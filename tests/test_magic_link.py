"""
Integration tests for the magic-link sign-in feature (task #16).

Covers the architect's requested verification:
  - fresh token success
  - reuse / replay failure (single-use)
  - expiry failure
  - unknown-email neutral response (no account-existence leak)
  - GET-on-confirm-URL does NOT burn the token (mailbox scanner safety)
  - POST-on-confirm-URL atomically consumes the token and logs the user in
  - regression: existing password login flow still works
"""
from datetime import datetime, timedelta

from app import db
from models import User


# Use example.com (valid TLD with MX) -- WTForms' Email validator runs
# deliverability checks that reject reserved TLDs like .invalid / .test.
KNOWN_EMAIL = "ml_user@example.com"
UNKNOWN_EMAIL = "nobody-here@example.com"
PASSWORD = "CorrectHorseBattery1!"


def _make_user(email=KNOWN_EMAIL, username="ml_user"):
    """Create + commit a fresh user. Caller owns cleanup via db_session rollback."""
    u = User(username=username, email=email)
    u.set_password(PASSWORD)
    db.session.add(u)
    db.session.commit()
    return u


# --------------------------------------------------------------------------- #
# Token-level behavior (model layer)
# --------------------------------------------------------------------------- #

def test_fresh_token_verifies_and_can_be_consumed(db_session):
    u = _make_user(username="ml_fresh", email="ml_fresh@example.com")
    user_id = u.id
    token = u.generate_magic_link_token()
    db.session.commit()

    consumed = User.consume_magic_link_token(user_id, token)
    assert consumed is not None
    assert consumed.id == user_id

    db.session.expire_all()
    u_after = db.session.get(User, user_id)
    assert u_after.magic_link_token_hash is None
    assert u_after.magic_link_token_expiry is None


def test_replay_after_consume_is_rejected(db_session):
    """A magic link must only work once -- replays must be rejected."""
    u = _make_user(username="ml_replay", email="ml_replay@example.com")
    user_id = u.id
    token = u.generate_magic_link_token()
    db.session.commit()

    first = User.consume_magic_link_token(user_id, token)
    assert first is not None

    second = User.consume_magic_link_token(user_id, token)
    assert second is None, "single-use token must not be redeemable twice"


def test_expired_token_is_rejected(db_session):
    u = _make_user(username="ml_expired", email="ml_expired@example.com")
    user_id = u.id
    token = u.generate_magic_link_token()
    u.magic_link_token_expiry = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()

    assert User.consume_magic_link_token(user_id, token) is None


def test_wrong_token_does_not_burn_real_token(db_session):
    """A bad guess must not invalidate the legitimate outstanding token."""
    u = _make_user(username="ml_badguess", email="ml_badguess@example.com")
    user_id = u.id
    real_token = u.generate_magic_link_token()
    db.session.commit()

    assert User.consume_magic_link_token(user_id, "not-the-real-token") is None

    db.session.expire_all()
    consumed = User.consume_magic_link_token(user_id, real_token)
    assert consumed is not None
    assert consumed.id == user_id


# --------------------------------------------------------------------------- #
# HTTP-level behavior (request + confirm routes)
# --------------------------------------------------------------------------- #

def test_request_known_email_returns_neutral_confirmation(client, db_session, monkeypatch):
    """A request for a known email must return the neutral confirmation
    flash and trigger an email send (we stub the sender)."""
    _make_user()

    sent = []
    monkeypatch.setattr(
        "auth.routes.send_magic_link_email",
        lambda user, magic_url: sent.append((user.id, magic_url)) or True,
    )

    r = client.post(
        "/auth/magic_link_request",
        data={"email": KNOWN_EMAIL, "submit": "Email me a sign-in link"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Neutral flash message defined in auth/routes.py magic_link_request().
    assert "If your email address exists in our database" in body, (
        f"expected neutral flash, got body snippet: {body[:500]}"
    )
    assert len(sent) == 1, "send_magic_link_email should have been called once"


def test_request_unknown_email_returns_same_neutral_response(client, db_session, monkeypatch):
    """Unknown emails must NOT leak account existence -- same UX as known."""
    sent = []
    monkeypatch.setattr(
        "auth.routes.send_magic_link_email",
        lambda user, magic_url: sent.append(user.id) or True,
    )

    r = client.post(
        "/auth/magic_link_request",
        data={"email": UNKNOWN_EMAIL, "submit": "Email me a sign-in link"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "If your email address exists in our database" in body
    assert sent == [], "must NOT attempt to send email for unknown account"


def test_get_on_confirm_url_does_not_burn_token(client, db_session):
    """Mailbox scanner / link-preview prefetches must not burn the token."""
    u = _make_user(username="ml_get", email="ml_get@example.com")
    user_id = u.id
    token = u.generate_magic_link_token()
    db.session.commit()

    r = client.get(f"/auth/magic_link/{user_id}/{token}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Sign me in" in body, (
        "confirm page should render its consent button (token must verify on GET)"
    )

    db.session.expire_all()
    u_after = db.session.get(User, user_id)
    assert u_after.magic_link_token_hash is not None, (
        "GET must NOT consume the token -- prefetchers would burn it otherwise"
    )


def test_post_confirm_signs_user_in(client, db_session):
    u = _make_user(username="ml_post", email="ml_post@example.com")
    user_id = u.id
    token = u.generate_magic_link_token()
    db.session.commit()

    # CSRF disabled in test config (WTF_CSRF_ENABLED=False).
    r = client.post(
        f"/auth/magic_link/{user_id}/{token}",
        data={},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (
        f"POST should redirect on success, got {r.status_code}: "
        f"{r.get_data(as_text=True)[:200]}"
    )
    loc = r.headers["Location"]
    assert "/auth/login" not in loc, f"successful POST must not bounce to login: {loc}"
    assert "magic_link_request" not in loc, (
        f"successful POST must not bounce to request page: {loc}"
    )

    # Token burned.
    db.session.expire_all()
    assert db.session.get(User, user_id).magic_link_token_hash is None


def test_post_confirm_with_bad_token_does_not_log_in(client, db_session):
    """POST with a bogus token must not authenticate the session."""
    u = _make_user(username="ml_badpost", email="ml_badpost@example.com")
    user_id = u.id

    r = client.post(
        f"/auth/magic_link/{user_id}/this-is-not-a-valid-token",
        data={},
        follow_redirects=False,
    )
    if r.status_code in (302, 303):
        loc = r.headers.get("Location", "")
        assert "/dashboard" not in loc
        assert "/projects" not in loc


# --------------------------------------------------------------------------- #
# Regression: password login must still work unchanged
# --------------------------------------------------------------------------- #

def test_password_login_still_works(client, db_session):
    _make_user(username="ml_pwlogin", email="ml_pwlogin@example.com")

    r = client.post(
        "/auth/login",
        data={
            "email": "ml_pwlogin@example.com",
            "password": PASSWORD,
            "remember_me": False,
            "submit": "Sign In",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), (
        f"password login should redirect on success, got {r.status_code}: "
        f"{r.get_data(as_text=True)[:200]}"
    )
    loc = r.headers["Location"]
    assert "/auth/login" not in loc, "successful login must not bounce back to /auth/login"
