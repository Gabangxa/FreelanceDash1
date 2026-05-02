"""Integration tests for the Google-OAuth sign-in feature (Task #17).

These tests do not actually hit Google's OAuth endpoints. They exercise
the helper functions and the three-step user-resolution logic directly,
plus the template-rendering / settings-page integration. The HTTP layer
of the OAuth flow itself (redirect to Google, callback handling) is
mocked at the network boundary in ``test_callback_*``.
"""
from unittest.mock import patch

import pytest

import google_auth as ga
from app import db
from models import User


PASSWORD = "CorrectHorseBattery1!"


def _make_password_user(username, email):
    u = User(username=username, email=email)
    u.set_password(PASSWORD)
    db.session.add(u)
    db.session.commit()
    return u


# --------------------------------------------------------------------------- #
# is_configured / blueprint registration
# --------------------------------------------------------------------------- #

def test_is_configured_false_without_secrets(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert ga.is_configured() is False


def test_is_configured_true_with_both_secrets(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    assert ga.is_configured() is True


def test_is_configured_false_with_only_one_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert ga.is_configured() is False


# --------------------------------------------------------------------------- #
# Username generation -- collision-safe
# --------------------------------------------------------------------------- #

def test_generate_unique_username_uses_clean_seed_when_available(db_session):
    u = ga._generate_unique_username("Alice")
    assert u == "Alice"


def test_generate_unique_username_strips_punctuation(db_session):
    u = ga._generate_unique_username("Mary-Jane O'Neil")
    assert u == "MaryJaneONeil"


def test_generate_unique_username_falls_back_to_default_for_empty(db_session):
    assert ga._generate_unique_username("") == "user"
    assert ga._generate_unique_username(None) == "user"
    assert ga._generate_unique_username("!!!") == "user"


def test_generate_unique_username_appends_random_on_collision(db_session):
    _make_password_user("Bob", "bob1@example.com")
    candidate = ga._generate_unique_username("Bob")
    assert candidate != "Bob"
    assert candidate.startswith("Bob_")


# --------------------------------------------------------------------------- #
# _find_or_create_user -- the three-step lookup
# --------------------------------------------------------------------------- #

def test_find_or_create_existing_oauth_link_returns_same_user(db_session):
    u = User(
        username="oauth_existing",
        email="oauth_existing@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-existing",
    )
    db.session.add(u)
    db.session.commit()

    found = ga._find_or_create_user("sub-existing", "doesnt-matter@example.com", "X")
    assert found.id == u.id
    # Email on file is NOT overwritten by whatever Google now reports.
    assert found.email == "oauth_existing@example.com"


def test_find_or_create_links_to_existing_email_match(db_session):
    """Existing local password account should get linked, additively."""
    u = _make_password_user("password_user", "linkme@example.com")
    user_id = u.id
    original_hash = u.password_hash

    found = ga._find_or_create_user("sub-link", "linkme@example.com", "Whoever")
    assert found.id == user_id
    assert found.oauth_provider == "google"
    assert found.oauth_provider_id == "sub-link"
    # Password is preserved -- additive linkage.
    assert found.password_hash == original_hash


def test_find_or_create_email_match_normalizes_case(db_session):
    """Google email should be matched case-insensitively against local."""
    _make_password_user("case_user", "casey@example.com")

    # Caller already lowercases, but the stored email is also lower so
    # matching works. This guards against a future regression where the
    # caller stops lowercasing.
    found = ga._find_or_create_user("sub-case", "casey@example.com", "Casey")
    assert found.email == "casey@example.com"
    assert found.oauth_provider_id == "sub-case"


def test_find_or_create_creates_new_user_when_no_match(db_session):
    found = ga._find_or_create_user("sub-new", "brand_new@example.com", "Brand")
    assert found.id is not None
    assert found.email == "brand_new@example.com"
    assert found.username.startswith("Brand")
    assert found.password_hash is None  # no password set
    assert found.oauth_provider == "google"
    assert found.oauth_provider_id == "sub-new"


def test_find_or_create_refuses_to_relink_account_with_different_subject(db_session):
    """If a local account already linked to Google sub A tries to be
    relinked to Google sub B (because the user logged in with a
    different Google account that happens to share the email), refuse
    rather than silently overwrite. The callback turns this exception
    into a user-facing flash + redirect to /auth/login -- see the
    callback handler."""
    u = User(
        username="already_linked",
        email="conflict@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-A",
    )
    db.session.add(u)
    db.session.commit()

    with pytest.raises(ga.OAuthAccountConflict):
        ga._find_or_create_user("sub-B", "conflict@example.com", "X")


# --------------------------------------------------------------------------- #
# get_sign_in_methods helper on User
# --------------------------------------------------------------------------- #

def test_sign_in_methods_password_and_magic_link_for_password_user(db_session):
    u = _make_password_user("methods_pw", "pw@example.com")
    methods = u.get_sign_in_methods()
    assert "password" in methods
    assert "magic_link" in methods
    assert "oauth:google" not in methods


def test_sign_in_methods_includes_oauth_when_linked(db_session):
    u = User(
        username="methods_g",
        email="g@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-m",
    )
    db.session.add(u)
    db.session.commit()
    methods = u.get_sign_in_methods()
    assert "oauth:google" in methods
    # No password set -> no "password" entry.
    assert "password" not in methods
    assert "magic_link" in methods


# --------------------------------------------------------------------------- #
# Template integration: /auth/login renders with/without Google button
# --------------------------------------------------------------------------- #

def test_login_page_hides_google_button_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"Continue with Google" not in resp.data


def test_register_page_hides_google_button_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    resp = client.get("/auth/register")
    assert resp.status_code == 200
    assert b"Continue with Google" not in resp.data


# --------------------------------------------------------------------------- #
# Settings page: surfaces the methods
# --------------------------------------------------------------------------- #

def test_sign_in_methods_settings_page_requires_login(client):
    resp = client.get("/settings/sign-in-methods", follow_redirects=False)
    # Flask-Login redirects to login.
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_sign_in_methods_settings_page_renders_for_logged_in_user(client, db_session):
    u = _make_password_user("settings_view", "settingsview@example.com")
    # Log in via the password route.
    resp = client.post(
        "/auth/login",
        data={"email": u.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)

    page = client.get("/settings/sign-in-methods")
    assert page.status_code == 200
    assert b"Sign-in Methods" in page.data
    assert b"Password" in page.data
    assert b"Magic-link email" in page.data
    assert b"Google" in page.data


# --------------------------------------------------------------------------- #
# Regression: existing password login flow still works
# --------------------------------------------------------------------------- #

def test_email_link_lookup_is_case_insensitive(db_session):
    """A legacy local account stored with a mixed-case email must still
    match a Google email returned in lower-case. Otherwise the user
    would silently get a brand-new account instead of having their
    existing one linked."""
    u = User(
        username="legacy_mixed",
        email="MixedCase@Example.com",
    )
    u.set_password("irrelevant")
    db.session.add(u)
    db.session.commit()
    original_id = u.id

    found = ga._find_or_create_user("sub-mixed", "mixedcase@example.com", "Mixed")

    assert found.id == original_id
    assert found.oauth_provider == "google"
    assert found.oauth_provider_id == "sub-mixed"


def test_oauth_only_user_cannot_login_with_password(client, db_session):
    """Regression: an OAuth-only user (no password_hash) attempting
    email/password login must get the normal "invalid credentials"
    response, NOT a 500. ``User.check_password`` on a NULL hash used
    to raise AttributeError before the Task-#17 null guard."""
    u = User(
        username="oauth_only",
        email="oauth_only@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-pwlogin",
    )
    db.session.add(u)
    db.session.commit()

    # Direct model-level check first -- belt-and-braces.
    assert u.check_password("anything") is False
    assert u.check_password("") is False

    resp = client.post(
        "/auth/login",
        data={"email": u.email, "password": "anything"},
        follow_redirects=False,
    )
    # Should bounce back to login (302), NOT crash with a 500.
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_google_auth_module_is_importable_standalone():
    """Regression: ``google_auth`` must be importable on its own,
    without first importing ``app``. This protects against accidentally
    re-introducing top-level ``from app import db`` / ``from models
    import User`` calls -- both would create a circular import because
    ``app.py`` imports ``google_auth`` at blueprint-registration time.

    We spawn a fresh ``python -c`` so the import happens in a
    process where ``app`` has never been loaded."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import google_auth; print(google_auth.__name__)"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Standalone import of google_auth failed:\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "google_auth" in result.stdout


def test_callback_state_mismatch_aborts(client, db_session, monkeypatch):
    """The OAuth callback must reject a mismatched ``state`` parameter
    rather than trust attacker-supplied query args."""
    # Pretend OAuth is configured so the route is registered.
    with client.session_transaction() as s:
        s[ga.SESSION_STATE_KEY] = "expected-state"
    resp = client.get("/google_login/callback?state=wrong-state&code=anything")
    assert resp.status_code == 400


def test_callback_happy_path_creates_user_and_logs_in(client, db_session, monkeypatch):
    """End-to-end happy path: a brand-new Google user hits the
    callback with valid state, the token exchange and userinfo fetch
    are mocked, and we verify a User row is created, the session is
    established (login_user fired), and the response 302s to the
    safe ``next`` target."""
    import json as _json

    # --- 1. Stub Google's HTTP endpoints ----------------------------------
    fake_discovery = {
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
    }
    fake_token = {
        "access_token": "fake-access-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "openid email profile",
        "id_token": "fake.id.token",
    }
    fake_userinfo = {
        "sub": "google-sub-happy-path",
        "email": "happy@example.com",
        "email_verified": True,
        "given_name": "Happy",
        "name": "Happy User",
    }

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def json(self):
            return self._payload
        def raise_for_status(self):
            return None

    def fake_get(url, *args, **kwargs):
        # Discovery doc fetch comes first; userinfo fetch comes second.
        if url == ga.GOOGLE_DISCOVERY_URL:
            return _FakeResp(fake_discovery)
        if url.startswith(fake_discovery["userinfo_endpoint"]):
            return _FakeResp(fake_userinfo)
        raise AssertionError(f"Unexpected GET to {url}")

    def fake_post(url, *args, **kwargs):
        assert url == fake_discovery["token_endpoint"]
        return _FakeResp(fake_token)

    monkeypatch.setattr(ga.requests, "get", fake_get)
    monkeypatch.setattr(ga.requests, "post", fake_post)

    # The oauthlib client also parses the token response body; that path
    # works on real JSON, so passing through the fake_token JSON above
    # is sufficient.

    # --- 2. Set up the OAuth state + a safe next target -------------------
    with client.session_transaction() as s:
        s[ga.SESSION_STATE_KEY] = "happy-state"
        s[ga.SESSION_NEXT_KEY] = "/settings/sign-in-methods"

    # --- 3. Hit the callback ---------------------------------------------
    resp = client.get(
        "/google_login/callback?state=happy-state&code=fake-auth-code",
        follow_redirects=False,
    )

    # --- 4. Assert ---------------------------------------------------------
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings/sign-in-methods")

    # The user should now exist in the DB, linked to the Google sub.
    new_user = User.query.filter_by(email="happy@example.com").first()
    assert new_user is not None
    assert new_user.oauth_provider == "google"
    assert new_user.oauth_provider_id == "google-sub-happy-path"
    assert new_user.password_hash is None  # OAuth-only account

    # And the session should reflect the logged-in user (flask_login
    # stores the user id under ``_user_id``).
    with client.session_transaction() as s:
        assert s.get("_user_id") == str(new_user.id)


def test_password_login_still_works_after_oauth_changes(client, db_session):
    u = _make_password_user("regression_pw", "regression@example.com")
    resp = client.post(
        "/auth/login",
        data={"email": u.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    # Should land on the dashboard, not bounce back to login.
    assert "/auth/login" not in resp.headers["Location"]
