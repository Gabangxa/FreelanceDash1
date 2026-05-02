"""Google OAuth sign-in blueprint (Task #17).

Adapted from the Replit ``flask_google_oauth`` blueprint, but customized
to fit our existing email/password + magic-link auth stack:

* Stable subject identifier. We key OAuth accounts off Google's ``sub``
  claim (stored in ``User.oauth_provider_id``), not the email -- emails
  can be changed inside the Google account, and we don't want one user
  to be able to grab another user's local row by changing their Gmail
  to match.

* Three-step lookup on callback:
    1. Match an existing user by (oauth_provider='google', oauth_provider_id=sub).
    2. Otherwise, match by lowercased + stripped email and *link* the
       Google identity onto that existing local account (additive --
       password login keeps working).
    3. Otherwise, create a brand-new account with a collision-safe
       generated username derived from the Google ``given_name``.

* Safe redirects. The ``next`` query parameter is preserved across the
  OAuth round-trip via Flask session and validated through the same
  ``is_safe_url`` helper used by password + magic-link login.

* Defensive registration. The blueprint is only registered when the
  ``GOOGLE_OAUTH_CLIENT_ID`` and ``GOOGLE_OAUTH_CLIENT_SECRET`` secrets
  are present, so the app boots fine in environments that haven't yet
  set up Google OAuth (tests, local dev without secrets, etc).

* No /logout override. The upstream blueprint claimed ``/logout``,
  which would clash with our existing ``auth.logout`` route. We don't
  redefine it -- the existing logout already calls ``logout_user()``
  which handles OAuth-logged-in sessions identically.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets

import requests
from flask import Blueprint, abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user, login_user
from oauthlib.oauth2 import WebApplicationClient
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from utils.security import is_safe_url

# NOTE: ``from app import db`` and ``from models import User`` are
# intentionally deferred into the functions that need them. Doing them
# at module load makes this blueprint un-importable standalone (because
# ``app.py`` itself imports ``google_auth`` at registration time), and
# we want ``import google_auth`` to succeed in any order so that:
#   1) tooling / linters / IDEs that introspect the module don't crash;
#   2) tests can import this file before the Flask app context exists.

logger = logging.getLogger(__name__)

GOOGLE_DISCOVERY_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
PROVIDER_KEY = "google"
# Where we stash the post-login redirect across the OAuth round-trip.
SESSION_NEXT_KEY = "_oauth_next"
# CSRF protection for the OAuth state parameter.
SESSION_STATE_KEY = "_oauth_state"

google_auth = Blueprint("google_auth", __name__)


class OAuthAccountConflict(Exception):
    """Raised by ``_find_or_create_user`` when the email-matched local
    account is already linked to a *different* Google identity. The
    caller turns this into a user-facing flash + redirect rather than
    a bare 409 page."""


def is_configured() -> bool:
    """Return True iff the Google OAuth credentials are configured.

    Used by ``app.py`` to decide whether to register this blueprint, and
    by the templates/settings page to decide whether to surface the
    "Continue with Google" button.
    """
    return bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def _client() -> WebApplicationClient:
    """Build a fresh OAuth client per request.

    We don't cache this at module scope because (a) tests may toggle the
    secret at runtime and (b) it's a cheap object.
    """
    return WebApplicationClient(os.environ["GOOGLE_OAUTH_CLIENT_ID"])


def _external_callback_url() -> str:
    """The fully-qualified https:// callback URL for this Repl.

    Replit terminates TLS at the proxy and forwards http:// to the app,
    so ``request.base_url`` reports http even when the browser used
    https. We rewrite to https because Google requires the redirect URI
    to *exactly* match what's whitelisted in the OAuth client config.
    """
    return (
        request.url_root.replace("http://", "https://").rstrip("/")
        + url_for("google_auth.callback")
    )


def _generate_unique_username(seed: str | None) -> str:
    """Return a username that satisfies our existing username rules and
    is not currently taken in the ``user`` table.

    The registration form enforces ``^[a-zA-Z0-9_]+$`` so we mirror that
    here. We try the sanitized seed first (gives the user a clean name
    if their Google ``given_name`` happens to fit), then suffix with a
    short random hex string. The random suffix path is what guarantees
    we never collide on a popular first name like "John".
    """
    from models import User  # deferred -- see module-level NOTE

    base = re.sub(r"[^a-zA-Z0-9_]", "", (seed or "").strip()) or "user"
    # Cap base so the final username stays well under our 64-char limit.
    base = base[:40]

    # Try bare seed first for a clean username.
    if not User.query.filter_by(username=base).first():
        return base

    # Fall back to base + random suffix until we find a free slot. 8 hex
    # chars = 4 bytes of entropy = 4.3 billion possibilities; collisions
    # require many retries even at large user counts. Cap at 5 retries
    # to bound worst-case latency; if all 5 collide something is wrong.
    for _ in range(5):
        candidate = f"{base}_{secrets.token_hex(4)}"[:64]
        if not User.query.filter_by(username=candidate).first():
            return candidate
    raise RuntimeError(
        "Could not generate a unique username after 5 attempts; "
        "this should be statistically impossible."
    )


@google_auth.route("/google_login")
def login():
    """Step 1: redirect to Google's authorization endpoint."""
    if current_user.is_authenticated:
        return redirect(url_for("projects.dashboard"))

    # Stash the post-login redirect target across the OAuth round-trip.
    # We can't pass it through Google because they only echo back the
    # ``state`` parameter, and putting URLs into ``state`` invites abuse.
    raw_next = request.args.get("next") or ""
    session[SESSION_NEXT_KEY] = raw_next if is_safe_url(raw_next) else ""

    # CSRF for the OAuth flow itself: an attacker who can get the user
    # to load /google_login/callback with a code they control could
    # otherwise hijack the session. We bind ``state`` to the user's
    # session and verify on callback.
    state = secrets.token_urlsafe(32)
    session[SESSION_STATE_KEY] = state

    try:
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL, timeout=5).json()
    except (requests.RequestException, ValueError):
        logger.exception("Google OAuth discovery document fetch failed")
        abort(503)

    request_uri = _client().prepare_request_uri(
        google_provider_cfg["authorization_endpoint"],
        redirect_uri=_external_callback_url(),
        scope=["openid", "email", "profile"],
        state=state,
    )
    return redirect(request_uri)


@google_auth.route("/google_login/callback")
def callback():
    """Step 2: exchange the auth code for tokens, identify the user,
    create-or-link the local account, and log them in.
    """
    if current_user.is_authenticated:
        return redirect(url_for("projects.dashboard"))

    # Verify the OAuth state matches what we stashed in the session.
    # ``session.pop`` so a replayed callback can't reuse the same state.
    expected_state = session.pop(SESSION_STATE_KEY, None)
    if (
        not expected_state
        or not secrets.compare_digest(expected_state, request.args.get("state") or "")
    ):
        logger.warning("Google OAuth state mismatch on callback")
        abort(400)

    code = request.args.get("code")
    if not code:
        logger.warning("Google OAuth callback missing authorization code")
        abort(400)

    try:
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL, timeout=5).json()
    except (requests.RequestException, ValueError):
        logger.exception("Google OAuth discovery document fetch failed")
        abort(503)

    client = _client()
    callback_url = _external_callback_url()

    token_url, headers, body = client.prepare_token_request(
        google_provider_cfg["token_endpoint"],
        # Replace http:// with https:// so the value matches what we
        # registered with Google (Replit terminates TLS at the proxy).
        authorization_response=request.url.replace("http://", "https://"),
        redirect_url=callback_url,
        code=code,
    )
    try:
        token_response = requests.post(
            token_url,
            headers=headers,
            data=body,
            auth=(
                os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            ),
            timeout=10,
        )
        token_response.raise_for_status()
    except requests.RequestException:
        logger.exception("Google OAuth token exchange failed")
        abort(502)

    client.parse_request_body_response(json.dumps(token_response.json()))

    # Fetch userinfo (email, sub, given_name, email_verified, ...).
    uri, headers, body = client.add_token(google_provider_cfg["userinfo_endpoint"])
    try:
        userinfo_response = requests.get(uri, headers=headers, data=body, timeout=10)
        userinfo_response.raise_for_status()
    except requests.RequestException:
        logger.exception("Google OAuth userinfo fetch failed")
        abort(502)

    userinfo = userinfo_response.json()

    if not userinfo.get("email_verified"):
        logger.warning(
            "Google OAuth callback for unverified email: %s",
            userinfo.get("email"),
        )
        return ("User email not available or not verified by Google.", 400)

    google_sub = userinfo.get("sub")
    google_email = (userinfo.get("email") or "").lower().strip()
    google_given_name = userinfo.get("given_name") or userinfo.get("name") or ""

    if not google_sub or not google_email:
        logger.warning("Google OAuth userinfo missing sub or email")
        abort(400)

    try:
        user = _find_or_create_user(google_sub, google_email, google_given_name)
    except OAuthAccountConflict:
        # User-friendly conflict path: the local account this Google
        # email maps to is already linked to a *different* Google
        # identity. Tell the user what to do (sign in with the original
        # Google account, or use password / magic link) instead of
        # showing a bare 409 page.
        flash(
            "An account with this email is already linked to a different "
            "Google account. Please sign in using your original Google "
            "account, your password, or request a magic-link email.",
            "danger",
        )
        return redirect(url_for("auth.login"))
    except SQLAlchemyError:
        from app import db  # deferred -- see module-level NOTE
        db.session.rollback()
        logger.exception("Database error during Google OAuth callback")
        abort(500)

    login_user(user, remember=False)
    logger.info(
        "User logged in via Google: %s (ID: %s, sub=%s)",
        user.username, user.id, google_sub,
    )

    # Pop the stashed post-login target. Re-validate is_safe_url at use
    # time -- session contents are technically attacker-influenceable
    # via cookie tampering, and the dashboard is a safe default.
    target = session.pop(SESSION_NEXT_KEY, "") or ""
    if not is_safe_url(target):
        target = url_for("projects.dashboard")
    return redirect(target)


def _find_or_create_user(sub: str, email: str, given_name: str):
    """Three-step lookup: provider id, then email, then create.

    Each step is its own commit point so a partial failure can't leave
    the user in a half-linked state.
    """
    from app import db  # deferred -- see module-level NOTE
    from models import User  # deferred -- see module-level NOTE

    # 1) Already linked to a local account from a prior Google sign-in.
    user = User.query.filter_by(
        oauth_provider=PROVIDER_KEY, oauth_provider_id=sub,
    ).first()
    if user is not None:
        return user

    # 2) Email match -- link the Google identity additively. The user
    #    keeps any existing password / magic-link capability so they can
    #    still sign in either way. Compare case-insensitively at the DB
    #    level so legacy mixed-case rows still match (the caller already
    #    lower/strips the Google-supplied email).
    user = User.query.filter(func.lower(User.email) == email).first()
    if user is not None:
        if user.oauth_provider and (
            user.oauth_provider != PROVIDER_KEY or user.oauth_provider_id != sub
        ):
            # The local account is already linked to a different Google
            # identity (or a different provider). Refuse silently rather
            # than overwrite -- the more-secure default.
            logger.warning(
                "Refusing to relink Google identity %s onto user %s "
                "(already linked to provider=%s subject=%s)",
                sub, user.id, user.oauth_provider, user.oauth_provider_id,
            )
            raise OAuthAccountConflict()
        user.oauth_provider = PROVIDER_KEY
        user.oauth_provider_id = sub
        db.session.commit()
        logger.info(
            "Linked Google identity to existing user: %s (ID: %s)",
            user.username, user.id,
        )
        return user

    # 3) Brand-new account. Generate a collision-safe username, then
    #    persist with no password (sign-in via Google or magic link).
    username = _generate_unique_username(given_name)
    user = User(
        username=username,
        email=email,
        oauth_provider=PROVIDER_KEY,
        oauth_provider_id=sub,
    )
    db.session.add(user)
    db.session.commit()
    logger.info(
        "Created new user via Google OAuth: %s (ID: %s)",
        user.username, user.id,
    )
    return user
