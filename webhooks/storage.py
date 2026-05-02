"""
Pluggable storage backend for webhook rate-limit counters, failed-attempt
tracking, and small caches (e.g. fetched IP allowlists).

Why this exists
---------------
The previous implementation kept counters in process-local dictionaries
(``rate_limit_storage`` / ``failed_attempts_storage``). Under gunicorn each
worker has its own copy, so the limit is effectively N * intended-limit and
counters reset on every restart. Both make the rate limit trivial to bypass.

Backend selection
-----------------
* If ``REDIS_URL`` is set we use Redis (sorted sets keyed by timestamp).
* Otherwise we fall back to a Postgres/SQLite-backed implementation that
  uses the ``WebhookRateLimitEvent`` / ``WebhookFailedAttempt`` /
  ``WebhookCacheEntry`` tables defined in ``models.py``.

The chosen backend is logged once at first use so operators always know
which backend is live.
"""
from __future__ import annotations

import abc
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class WebhookStorageBackend(abc.ABC):
    """Abstract storage backend for webhook security state.

    All counter methods operate on a sliding window of ``window_seconds``.
    Implementations must ensure that entries older than the window are
    pruned eagerly so memory/row counts stay bounded.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def incr_with_window(self, key: str, window_seconds: int) -> int:
        """Record one event under ``key`` at "now" and return the count of
        events still inside the trailing ``window_seconds`` window
        (including this one)."""

    @abc.abstractmethod
    def get_count(self, key: str, window_seconds: int) -> int:
        """Return the number of events under ``key`` that are still inside
        the trailing ``window_seconds`` window."""

    @abc.abstractmethod
    def record_failed_attempt(self, key: str, window_seconds: int) -> int:
        """Record one failed attempt and return the failed-attempt count
        in the trailing ``window_seconds`` window."""

    @abc.abstractmethod
    def cache_get(self, key: str) -> Optional[str]:
        """Return the cached string value, or ``None`` if missing/expired."""

    @abc.abstractmethod
    def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store ``value`` under ``key`` with the given TTL."""

    @abc.abstractmethod
    def clear_counters(self) -> None:
        """Clear all rate-limit and failed-attempt counters. Used by the
        admin "clear cache" endpoint."""

    @abc.abstractmethod
    def active_rate_limit_keys(self) -> int:
        """Return the number of distinct rate-limit keys with at least one
        live entry. Used by the admin status endpoint."""

    @abc.abstractmethod
    def total_failed_attempts(self, window_seconds: int) -> int:
        """Return the total number of failed attempts across all keys
        within the trailing window. Used by the admin status endpoint."""


# ---------------------------------------------------------------------------
# Redis implementation
# ---------------------------------------------------------------------------
class RedisWebhookStorage(WebhookStorageBackend):
    """Redis-backed storage using sorted sets keyed by timestamp.

    Each ``key`` maps to a ZSET whose members are ``"<ts>:<nonce>"`` with
    score = unix timestamp. ``incr`` does ``ZREMRANGEBYSCORE`` (drop old)
    + ``ZADD`` (add new) + ``ZCARD`` (count) atomically in a pipeline.

    Keys are TTL'd a little past the window so cold entries eventually
    expire even if no traffic comes in to trigger the in-line cleanup.
    """

    name = "redis"

    _RL_PREFIX = "webhook:rl:"
    _FA_PREFIX = "webhook:fa:"
    _CACHE_PREFIX = "webhook:cache:"

    def __init__(self, url: str):
        # Imported lazily so projects that never set REDIS_URL don't need
        # the redis client installed.
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        # Fail loudly at construction time so a misconfigured URL doesn't
        # silently degrade behaviour for the first few requests.
        self._redis.ping()

    # -- counters -----------------------------------------------------------
    def _zset_incr(self, full_key: str, window_seconds: int) -> int:
        now = time.time()
        cutoff = now - window_seconds
        # The nonce is required because two requests landing in the same
        # microsecond would otherwise collide on the ZSET member key and
        # only count once.
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(full_key, 0, cutoff)
        pipe.zadd(full_key, {member: now})
        pipe.zcard(full_key)
        pipe.expire(full_key, window_seconds + 60)
        results = pipe.execute()
        return int(results[2])

    def _zset_count(self, full_key: str, window_seconds: int) -> int:
        now = time.time()
        cutoff = now - window_seconds
        self._redis.zremrangebyscore(full_key, 0, cutoff)
        return int(self._redis.zcard(full_key))

    def incr_with_window(self, key: str, window_seconds: int) -> int:
        return self._zset_incr(self._RL_PREFIX + key, window_seconds)

    def get_count(self, key: str, window_seconds: int) -> int:
        return self._zset_count(self._RL_PREFIX + key, window_seconds)

    def record_failed_attempt(self, key: str, window_seconds: int) -> int:
        return self._zset_incr(self._FA_PREFIX + key, window_seconds)

    # -- cache --------------------------------------------------------------
    def cache_get(self, key: str) -> Optional[str]:
        return self._redis.get(self._CACHE_PREFIX + key)

    def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._redis.setex(self._CACHE_PREFIX + key, ttl_seconds, value)

    # -- admin --------------------------------------------------------------
    def clear_counters(self) -> None:
        for prefix in (self._RL_PREFIX, self._FA_PREFIX):
            for k in self._redis.scan_iter(match=prefix + "*"):
                self._redis.delete(k)

    def active_rate_limit_keys(self) -> int:
        return sum(1 for _ in self._redis.scan_iter(match=self._RL_PREFIX + "*"))

    def total_failed_attempts(self, window_seconds: int) -> int:
        total = 0
        now = time.time()
        cutoff = now - window_seconds
        for full_key in self._redis.scan_iter(match=self._FA_PREFIX + "*"):
            self._redis.zremrangebyscore(full_key, 0, cutoff)
            total += int(self._redis.zcard(full_key))
        return total


# ---------------------------------------------------------------------------
# DB implementation
# ---------------------------------------------------------------------------
class DBWebhookStorage(WebhookStorageBackend):
    """SQLAlchemy-backed storage using small append-only tables.

    Tables (defined in ``models.py``):
      * ``webhook_rate_limit_event(rate_key, created_at)``
      * ``webhook_failed_attempt(attempt_key, created_at)``
      * ``webhook_cache_entry(cache_key PK, value, expires_at)``

    Each ``incr`` path runs a ``DELETE WHERE created_at < cutoff`` for the
    same key, an ``INSERT``, then a ``COUNT(*)`` -- this keeps row counts
    bounded per key without needing a background sweeper.

    All operations commit independently so they don't entangle the caller's
    request transaction. On any error we rollback the local changes and
    raise -- the security decorator already converts exceptions into a 500.
    """

    name = "database"

    def _now(self) -> datetime:
        return datetime.utcnow()

    def _incr(self, model, key_attr: str, key: str, window_seconds: int) -> int:
        from app import db

        now = self._now()
        cutoff = now - timedelta(seconds=window_seconds)
        try:
            db.session.query(model).filter(
                getattr(model, key_attr) == key,
                model.created_at < cutoff,
            ).delete(synchronize_session=False)

            row = model()
            setattr(row, key_attr, key)
            row.created_at = now
            db.session.add(row)
            db.session.commit()

            count = db.session.query(model).filter(
                getattr(model, key_attr) == key,
                model.created_at >= cutoff,
            ).count()
            return int(count)
        except Exception:
            db.session.rollback()
            raise

    def _count(self, model, key_attr: str, key: str, window_seconds: int) -> int:
        from app import db

        cutoff = self._now() - timedelta(seconds=window_seconds)
        return int(
            db.session.query(model).filter(
                getattr(model, key_attr) == key,
                model.created_at >= cutoff,
            ).count()
        )

    def incr_with_window(self, key: str, window_seconds: int) -> int:
        from models import WebhookRateLimitEvent

        return self._incr(WebhookRateLimitEvent, "rate_key", key, window_seconds)

    def get_count(self, key: str, window_seconds: int) -> int:
        from models import WebhookRateLimitEvent

        return self._count(WebhookRateLimitEvent, "rate_key", key, window_seconds)

    def record_failed_attempt(self, key: str, window_seconds: int) -> int:
        from models import WebhookFailedAttempt

        return self._incr(WebhookFailedAttempt, "attempt_key", key, window_seconds)

    # -- cache --------------------------------------------------------------
    def cache_get(self, key: str) -> Optional[str]:
        from app import db
        from models import WebhookCacheEntry

        entry = db.session.get(WebhookCacheEntry, key)
        if entry is None:
            return None
        if entry.expires_at and entry.expires_at <= self._now():
            return None
        return entry.value

    def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        from app import db
        from models import WebhookCacheEntry

        try:
            expires = self._now() + timedelta(seconds=ttl_seconds)
            entry = db.session.get(WebhookCacheEntry, key)
            if entry is None:
                entry = WebhookCacheEntry()
                entry.cache_key = key
                entry.value = value
                entry.expires_at = expires
                db.session.add(entry)
            else:
                entry.value = value
                entry.expires_at = expires
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    # -- admin --------------------------------------------------------------
    def clear_counters(self) -> None:
        from app import db
        from models import WebhookFailedAttempt, WebhookRateLimitEvent

        try:
            db.session.query(WebhookRateLimitEvent).delete()
            db.session.query(WebhookFailedAttempt).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    def active_rate_limit_keys(self) -> int:
        from app import db
        from models import WebhookRateLimitEvent

        return int(
            db.session.query(WebhookRateLimitEvent.rate_key)
            .distinct()
            .count()
        )

    def total_failed_attempts(self, window_seconds: int) -> int:
        from app import db
        from models import WebhookFailedAttempt

        cutoff = self._now() - timedelta(seconds=window_seconds)
        return int(
            db.session.query(WebhookFailedAttempt)
            .filter(WebhookFailedAttempt.created_at >= cutoff)
            .count()
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_storage: Optional[WebhookStorageBackend] = None
_logged_choice: bool = False


def get_storage() -> WebhookStorageBackend:
    """Return the lazily-instantiated singleton storage backend.

    Selection rules:
      * ``REDIS_URL`` set                      -> Redis (raises on failure)
      * otherwise                              -> DB fallback

    The first successful call logs which backend was selected so it shows
    up exactly once per worker process at startup.
    """
    global _storage, _logged_choice
    if _storage is not None:
        return _storage

    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            _storage = RedisWebhookStorage(redis_url)
        except Exception as exc:
            # Refuse to silently degrade to in-memory or to the DB fallback
            # when REDIS_URL was explicitly configured -- the operator
            # almost certainly wants Redis specifically.
            logger.error(
                "REDIS_URL is set but Redis is unreachable: %s. "
                "Refusing to silently fall back.", exc,
            )
            raise RuntimeError(
                f"Webhook storage backend Redis is unreachable: {exc}"
            ) from exc
    else:
        _storage = DBWebhookStorage()

    if not _logged_choice:
        logger.info(
            "Webhook security storage backend initialised: %s "
            "(set REDIS_URL to use Redis instead of the DB fallback)",
            _storage.name,
        )
        _logged_choice = True
    return _storage


def reset_storage_for_tests() -> None:
    """Drop the cached singleton. Tests use this to swap implementations."""
    global _storage, _logged_choice
    _storage = None
    _logged_choice = False


def set_storage_for_tests(backend: WebhookStorageBackend) -> None:
    """Inject a specific backend, bypassing env-var-based selection."""
    global _storage, _logged_choice
    _storage = backend
    _logged_choice = True
