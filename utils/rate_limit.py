"""Postgres-backed sliding-window rate limiting for the auth endpoints.

Why this module exists
----------------------
The auth routes (login, register, password-reset request, magic-link
request) had no rate limiting, leaving them open to password brute-force
and to email-bombing / account-enumeration via the reset + magic-link
forms.

It deliberately reuses the same insert-prune-count pattern as the webhook
DB rate limiter (``webhooks/storage.py``) rather than adding Flask-Limiter,
because:

* Postgres is guaranteed on this deploy target (Replit ``postgresql-16``);
  Redis is not. An in-memory limiter would be per-worker *and* per-instance,
  so gunicorn's 4 workers plus autoscale would loosen the real limit well
  beyond what's configured.
* The counter lives in the shared database, so limits are correct across
  every worker and instance.
* Auth endpoints are low-QPS, so one insert + one count per attempt is
  negligible load.

Fail-open contract
------------------
``hit`` returns ``True`` (allowed) on any database error or when disabled.
A rate-limit backend hiccup must never lock legitimate users out of login;
availability wins over perfect enforcement here.
"""
import itertools
import logging
from datetime import datetime, timedelta

from flask import current_app, request
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# Largest window any caller uses (see auth/routes.py policy tuples). The
# opportunistic global sweep deletes rows older than THIS so it can never
# drop a row that's still counting toward a longer-window policy. Keep it
# >= the biggest window passed to hit().
_MAX_WINDOW_SECONDS = 3600

# Run a table-wide sweep roughly every Nth call. Mirrors the opportunistic
# prune in webhooks/storage.py's DatabaseStorage._incr: the per-key prune
# alone can't reclaim one-shot keys (each distinct IP/email hit once), so
# without this the table would grow without bound.
_SWEEP_EVERY = 256
_call_counter = itertools.count(1)


def _enabled():
    """Rate limiting is on unless explicitly disabled (e.g. in the test
    suite, where every request shares one client IP)."""
    try:
        return bool(current_app.config.get('AUTH_RATE_LIMIT_ENABLED', True))
    except RuntimeError:
        # No application context -- treat as enabled to be safe.
        return True


def client_ip():
    """Best-effort client IP. ProxyFix is applied in ``app.py`` so
    ``remote_addr`` already reflects the real client behind Replit's proxy.
    Falls back to a constant so a missing address can't crash the limiter."""
    return (request.remote_addr or 'unknown').strip()


def hit(key, limit, window_seconds):
    """Record one attempt for ``key`` and report whether it is still allowed.

    Returns ``True`` if the number of attempts for ``key`` within the trailing
    ``window_seconds`` is at or below ``limit`` (i.e. allow the request), and
    ``False`` once the limit is exceeded. Fails open (returns ``True``) when
    disabled or on any DB error.
    """
    if not _enabled():
        return True

    from app import db
    from models import AuthRateLimitEvent

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=window_seconds)
    try:
        # Prune this key's own expired rows, record this attempt, then count
        # the window. (This only reclaims keys that get hit again; one-shot
        # keys are swept table-wide by _maybe_global_sweep below.)
        db.session.query(AuthRateLimitEvent).filter(
            AuthRateLimitEvent.rate_key == key,
            AuthRateLimitEvent.created_at < cutoff,
        ).delete(synchronize_session=False)

        db.session.add(AuthRateLimitEvent(rate_key=key, created_at=now))
        db.session.commit()

        count = db.session.query(AuthRateLimitEvent).filter(
            AuthRateLimitEvent.rate_key == key,
            AuthRateLimitEvent.created_at >= cutoff,
        ).count()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception(
            "auth rate-limit backend error; failing open for key=%s", key
        )
        return True

    _maybe_global_sweep()
    return count <= limit


def _maybe_global_sweep():
    """Every ``_SWEEP_EVERY`` calls, delete all rows older than the largest
    policy window so one-shot keys can't accumulate forever. Best-effort:
    done after the caller's own increment has committed, and any error is
    swallowed -- this is housekeeping, never load-bearing."""
    if next(_call_counter) % _SWEEP_EVERY != 0:
        return
    from app import db
    from models import AuthRateLimitEvent

    cutoff = datetime.utcnow() - timedelta(seconds=_MAX_WINDOW_SECONDS)
    try:
        db.session.query(AuthRateLimitEvent).filter(
            AuthRateLimitEvent.created_at < cutoff,
        ).delete(synchronize_session=False)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("auth rate-limit global sweep failed (continuing)")
