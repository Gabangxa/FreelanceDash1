"""
Tests for the two auth hardening changes:

  1. Password-reset tokens are hashed at rest (never plaintext), verified in
     constant time, and single-use (atomic consume closes the double-submit
     race) -- mirroring the magic-link token guarantees.
  2. The auth-endpoint sliding-window rate limiter (utils/rate_limit.py)
     counts per key and blocks once the limit is exceeded, and the login
     route enforces it per targeted email.
"""
import hashlib
from datetime import datetime, timedelta

from app import db
from models import User, AuthRateLimitEvent
from utils import rate_limit


KNOWN_EMAIL = "reset_user@example.com"
PASSWORD = "CorrectHorseBattery1!"
NEW_PASSWORD = "BrandNewSecret9!"


def _make_user(email=KNOWN_EMAIL, username="reset_user"):
    u = User(username=username, email=email)
    u.set_password(PASSWORD)
    db.session.add(u)
    db.session.commit()
    return u


# --------------------------------------------------------------------------- #
# Reset-token model behavior
# --------------------------------------------------------------------------- #

def test_reset_token_is_stored_hashed_not_plaintext(db_session):
    u = _make_user(username="rt_hash", email="rt_hash@example.com")
    raw = u.generate_reset_token()
    db.session.commit()

    # The raw token is never persisted; only its SHA-256 digest is.
    assert u.reset_token_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert u.reset_token_hash != raw
    assert not hasattr(u, "reset_token")  # plaintext column is gone


def test_reset_token_verify_and_find(db_session):
    u = _make_user(username="rt_find", email="rt_find@example.com")
    raw = u.generate_reset_token()
    db.session.commit()

    assert u.verify_reset_token(raw) is True
    assert u.verify_reset_token("wrong-token") is False
    assert User.find_by_reset_token(raw).id == u.id
    assert User.find_by_reset_token("wrong-token") is None


def test_reset_token_consume_sets_password_and_is_single_use(db_session):
    u = _make_user(username="rt_consume", email="rt_consume@example.com")
    raw = u.generate_reset_token()
    db.session.commit()

    consumed = User.consume_reset_token(raw, NEW_PASSWORD)
    assert consumed is not None and consumed.id == u.id
    assert consumed.check_password(NEW_PASSWORD) is True
    assert consumed.reset_token_hash is None  # burned

    # Replay must fail -- single use.
    assert User.consume_reset_token(raw, "AnotherPass1!") is None


def test_expired_reset_token_is_rejected(db_session):
    u = _make_user(username="rt_expired", email="rt_expired@example.com")
    raw = u.generate_reset_token(expires_in=1)
    u.reset_token_expiry = datetime.utcnow() - timedelta(seconds=5)
    db.session.commit()

    assert u.verify_reset_token(raw) is False
    assert User.consume_reset_token(raw, NEW_PASSWORD) is None


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #

def test_rate_limit_hit_blocks_after_limit(app, db_session):
    app.config["AUTH_RATE_LIMIT_ENABLED"] = True
    try:
        key = "unittest:key:1"
        # limit=3 -> first three allowed, fourth blocked.
        assert rate_limit.hit(key, limit=3, window_seconds=60) is True
        assert rate_limit.hit(key, limit=3, window_seconds=60) is True
        assert rate_limit.hit(key, limit=3, window_seconds=60) is True
        assert rate_limit.hit(key, limit=3, window_seconds=60) is False
    finally:
        app.config["AUTH_RATE_LIMIT_ENABLED"] = False
        db.session.query(AuthRateLimitEvent).delete()
        db.session.commit()


def test_rate_limit_disabled_never_blocks(app, db_session):
    # Default test config has it disabled -> always allowed regardless of count.
    for _ in range(10):
        assert rate_limit.hit("unittest:disabled", limit=1, window_seconds=60) is True


def test_rate_limit_global_sweep_reclaims_one_shot_keys(app, db_session, monkeypatch):
    """The opportunistic global sweep must delete rows older than the max
    policy window, including one-shot keys that are never hit again (the bug
    a per-key-only prune would leave to grow unbounded)."""
    app.config["AUTH_RATE_LIMIT_ENABLED"] = True
    monkeypatch.setattr(rate_limit, "_SWEEP_EVERY", 1)  # sweep on every call
    try:
        stale = AuthRateLimitEvent(
            rate_key="one-shot:old",
            created_at=datetime.utcnow()
            - timedelta(seconds=rate_limit._MAX_WINDOW_SECONDS + 60),
        )
        db.session.add(stale)
        db.session.commit()

        # Any hit triggers the sweep because _SWEEP_EVERY == 1.
        rate_limit.hit("some:other:key", limit=5, window_seconds=300)

        remaining = db.session.query(AuthRateLimitEvent).filter_by(
            rate_key="one-shot:old"
        ).count()
        assert remaining == 0
    finally:
        app.config["AUTH_RATE_LIMIT_ENABLED"] = False
        db.session.query(AuthRateLimitEvent).delete()
        db.session.commit()


def test_login_route_rate_limited_per_email(app, client, db_session):
    app.config["AUTH_RATE_LIMIT_ENABLED"] = True
    _make_user(username="rt_login", email="rt_login@example.com")
    try:
        # Per-email login limit is 5 / 5 min; the 6th attempt is blocked.
        last = None
        for _ in range(6):
            last = client.post(
                "/auth/login",
                data={"email": "rt_login@example.com", "password": "wrong-pass"},
                follow_redirects=True,
            )
        assert b"Too many attempts" in last.data
    finally:
        app.config["AUTH_RATE_LIMIT_ENABLED"] = False
        db.session.query(AuthRateLimitEvent).delete()
        db.session.commit()
