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
* If ``NATS_URL`` is set we use JetStream KV buckets.
* Else if ``REDIS_URL`` is set we use Redis (sorted sets keyed by
  timestamp).
* Otherwise we fall back to a Postgres/SQLite-backed implementation that
  uses the ``WebhookRateLimitEvent`` / ``WebhookFailedAttempt`` /
  ``WebhookCacheEntry`` tables defined in ``models.py``.

When ``NATS_URL`` or ``REDIS_URL`` is explicitly set the corresponding
backend is required -- ``get_storage()`` refuses to silently degrade to
DB if the configured backend is unreachable, because that would mask a
misconfiguration and let the app keep serving webhooks under wrong
assumptions about counter consistency.

The chosen backend is logged once at first use so operators always know
which backend is live.
"""
from __future__ import annotations

import abc
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


# Default windows used by the background sweeper / opportunistic prune
# when the caller doesn't supply explicit values. Mirrors
# ``WebhookSecurity.RATE_LIMITS`` (max window) and
# ``WebhookSecurity.FAILED_ATTEMPT_WINDOW_SECONDS``. Hard-coded here so
# the storage layer doesn't have to import the security module (which
# would be a circular import).
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 3600
DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS = 3600


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

    @abc.abstractmethod
    def prune_expired(
        self,
        rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
    ) -> dict:
        """Sweep all rate-limit / failed-attempt / cache state and remove
        anything whose lifetime has elapsed.

        This is the global cleanup the per-key inline prune in ``incr``
        cannot do: a "drive-by" key that never repeats (e.g. one-shot
        botnet IPs) leaves rows behind forever otherwise.

        Returns a dict with deletion counts for observability:
        ``{"rate_limit": N, "failed_attempt": M, "cache": K}``."""


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

    def prune_expired(
        self,
        rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
    ) -> dict:
        """Best-effort sweep across all live ZSETs.

        Redis already TTLs each key past its window (see ``_zset_incr``),
        so cold keys disappear on their own and there is nothing to do
        for the cache (``setex`` carries the TTL). We still scan and run
        ``ZREMRANGEBYSCORE`` so this method is callable for parity with
        the DB backend (and so a long-lived ZSET that keeps getting
        re-bumped also drops its stale members proactively)."""
        now = time.time()
        rl_cutoff = now - rate_limit_window_seconds
        fa_cutoff = now - failed_attempt_window_seconds
        rl_removed = 0
        fa_removed = 0
        for full_key in self._redis.scan_iter(match=self._RL_PREFIX + "*"):
            rl_removed += int(
                self._redis.zremrangebyscore(full_key, 0, rl_cutoff)
            )
        for full_key in self._redis.scan_iter(match=self._FA_PREFIX + "*"):
            fa_removed += int(
                self._redis.zremrangebyscore(full_key, 0, fa_cutoff)
            )
        return {
            "rate_limit": rl_removed,
            "failed_attempt": fa_removed,
            "cache": 0,
        }


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

    # Opportunistic sweep: every Nth ``_incr`` call we also do a global
    # prune of all expired rows (not just the ones for this key). This
    # bounds the table under high traffic without needing a background
    # thread to be alive. Tunable via ``WEBHOOK_STORAGE_SWEEP_EVERY``
    # (default 256) so test code / heavy traffic can crank it up or down.
    _DEFAULT_SWEEP_EVERY = 256

    def __init__(self) -> None:
        self._incr_counter = 0
        try:
            self._sweep_every = int(
                os.environ.get(
                    "WEBHOOK_STORAGE_SWEEP_EVERY",
                    self._DEFAULT_SWEEP_EVERY,
                )
            )
        except (TypeError, ValueError):
            self._sweep_every = self._DEFAULT_SWEEP_EVERY
        if self._sweep_every < 1:
            self._sweep_every = self._DEFAULT_SWEEP_EVERY

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
        except SQLAlchemyError:
            db.session.rollback()
            raise

        # Opportunistic global sweep. Done after the commit above so a
        # failure in the sweep can't roll back the caller's increment.
        # Any error is swallowed and logged -- this is best-effort
        # housekeeping, never load-bearing.
        self._incr_counter += 1
        if self._incr_counter % self._sweep_every == 0:
            try:
                self.prune_expired(
                    rate_limit_window_seconds=window_seconds,
                    failed_attempt_window_seconds=DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
                )
            except Exception:  # noqa: BLE001 - opportunistic, never fatal
                logger.exception(
                    "Opportunistic prune_expired sweep failed (continuing)"
                )

        return int(count)

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
        except SQLAlchemyError:
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
        except SQLAlchemyError:
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

    def prune_expired(
        self,
        rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
    ) -> dict:
        """Delete expired rows across the three webhook-storage tables.

        * ``webhook_rate_limit_event``: rows with ``created_at`` older
          than ``rate_limit_window_seconds`` ago.
        * ``webhook_failed_attempt``: rows with ``created_at`` older than
          ``failed_attempt_window_seconds`` ago.
        * ``webhook_cache_entry``: rows whose ``expires_at`` has passed.

        Unlike the per-key inline prune in ``_incr``, this does NOT
        require the same key to be hit again -- it sweeps across every
        key, which is exactly what bounds the table when the inbound
        traffic is a long tail of one-shot source IPs.
        """
        from app import db
        from models import (
            WebhookCacheEntry,
            WebhookFailedAttempt,
            WebhookRateLimitEvent,
        )

        now = self._now()
        rl_cutoff = now - timedelta(seconds=rate_limit_window_seconds)
        fa_cutoff = now - timedelta(seconds=failed_attempt_window_seconds)
        try:
            rl_deleted = (
                db.session.query(WebhookRateLimitEvent)
                .filter(WebhookRateLimitEvent.created_at < rl_cutoff)
                .delete(synchronize_session=False)
            )
            fa_deleted = (
                db.session.query(WebhookFailedAttempt)
                .filter(WebhookFailedAttempt.created_at < fa_cutoff)
                .delete(synchronize_session=False)
            )
            cache_deleted = (
                db.session.query(WebhookCacheEntry)
                .filter(
                    WebhookCacheEntry.expires_at.isnot(None),
                    WebhookCacheEntry.expires_at < now,
                )
                .delete(synchronize_session=False)
            )
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            raise

        return {
            "rate_limit": int(rl_deleted or 0),
            "failed_attempt": int(fa_deleted or 0),
            "cache": int(cache_deleted or 0),
        }


# ---------------------------------------------------------------------------
# JetStream KV implementation
# ---------------------------------------------------------------------------
class JetStreamKVStorage(WebhookStorageBackend):
    """NATS JetStream KV-backed storage.

    Three buckets, one per concern:

      * ``webhook_rate_limit``       (rolling window counter)
      * ``webhook_failed_attempt``   (rolling window counter)
      * ``webhook_cache``            (key -> serialised JSON value+expiry)

    JetStream KV doesn't have sorted sets like Redis, so we serialise a
    JSON list of timestamps under each rate-limit / failed-attempt key
    and use optimistic-concurrency updates (``last_revision``) to avoid
    losing increments under contention. The bucket-level TTL means cold
    keys evaporate on their own; the sliding-window prune in
    ``_zset_incr`` drops anything older than ``window_seconds`` on every
    increment.

    For cache values we don't get per-key TTLs from KV reliably, so we
    encode the expiry inline as ``{"v": value, "exp": unix_ts}`` and
    treat any read past ``exp`` as a miss. The bucket TTL bounds memory.
    """

    name = "nats"

    _RL_BUCKET = "webhook_rate_limit"
    _FA_BUCKET = "webhook_failed_attempt"
    _CACHE_BUCKET = "webhook_cache"

    # CAS retry budget. JetStream KV update() raises on a revision
    # mismatch -- we retry a small bounded number of times so a high-rate
    # increment race converges, then fall back to a non-CAS put on the
    # final attempt so no event is silently dropped.
    _CAS_RETRIES = 5

    def __init__(self) -> None:
        # Lazy-import nats_client so the storage module stays importable
        # in environments that never set NATS_URL.
        import nats_client

        # Bucket TTL is window+60s for counters so cold rows evaporate
        # naturally; cache bucket gets 24h to bound memory while letting
        # individual entries set shorter logical expiries.
        self._rl = nats_client.kv(
            self._RL_BUCKET,
            ttl_seconds=DEFAULT_RATE_LIMIT_WINDOW_SECONDS + 60,
        )
        self._fa = nats_client.kv(
            self._FA_BUCKET,
            ttl_seconds=DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS + 60,
        )
        self._cache = nats_client.kv(
            self._CACHE_BUCKET, ttl_seconds=86400
        )
        # If any bucket failed to open the operator wanted NATS but it
        # isn't reachable -- refuse to silently downgrade. Same policy
        # as the Redis branch in get_storage().
        if self._rl is None or self._fa is None or self._cache is None:
            raise RuntimeError(
                "NATS_URL is set but JetStream KV buckets could not be "
                "opened. Refusing to silently fall back to the DB store."
            )

    # -- counter primitive -------------------------------------------------
    def _zset_incr(self, bucket, key: str, window_seconds: int) -> int:
        """Atomic-ish increment: read, prune, append, CAS-write. Retries a
        bounded number of times on revision mismatch."""
        now = time.time()
        cutoff = now - window_seconds

        for _attempt in range(self._CAS_RETRIES):
            entry = bucket.get(key)
            if entry is None:
                # First write under this key.
                payload = json.dumps([now], separators=(",", ":")).encode()
                try:
                    bucket.put(key, payload)
                    return 1
                except Exception:  # noqa: BLE001 - racing creator wins; retry
                    logger.debug("kv put race on %s, retrying", key, exc_info=True)
                    continue

            try:
                timestamps = json.loads(entry.value)
                if not isinstance(timestamps, list):
                    timestamps = []
            except (json.JSONDecodeError, TypeError, ValueError):
                # Corrupt entry -- treat as empty and overwrite.
                logger.warning("kv corrupt counter entry on %s; resetting", key)
                timestamps = []

            timestamps = [t for t in timestamps if isinstance(t, (int, float)) and t >= cutoff]
            timestamps.append(now)
            payload = json.dumps(timestamps, separators=(",", ":")).encode()
            try:
                bucket.update(key, payload, last=entry.revision)
                return len(timestamps)
            except Exception:  # noqa: BLE001 - revision mismatch / transient; retry
                logger.debug("kv CAS retry on %s", key, exc_info=True)
                continue

        # Final fallback after exhausting CAS retries. We deliberately
        # do NOT overwrite with ``[now]`` because that would reset the
        # counter under sustained contention -- a crude attacker could
        # exploit it to bypass rate limits. Instead, re-read the
        # latest committed value, prune to the window, append our
        # event, and blind-put. Concurrent writers may still clobber
        # this put, but at least the historical timestamps inside
        # ``window_seconds`` are preserved and the rate limit can't
        # be reset to 1.
        entry = bucket.get(key)
        if entry is None:
            timestamps: list = []
        else:
            try:
                raw = json.loads(entry.value)
                timestamps = [
                    t for t in raw
                    if isinstance(t, (int, float)) and t >= cutoff
                ] if isinstance(raw, list) else []
            except (json.JSONDecodeError, TypeError, ValueError):
                timestamps = []
        timestamps.append(now)
        payload = json.dumps(timestamps, separators=(",", ":")).encode()
        try:
            bucket.put(key, payload)
        except Exception:  # noqa: BLE001 - last-ditch; counter accuracy is best-effort here
            logger.exception("kv final-fallback put failed for %s", key)
        return len(timestamps)

    def _zset_count(self, bucket, key: str, window_seconds: int) -> int:
        entry = bucket.get(key)
        if entry is None:
            return 0
        try:
            timestamps = json.loads(entry.value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0
        cutoff = time.time() - window_seconds
        return sum(
            1
            for t in timestamps
            if isinstance(t, (int, float)) and t >= cutoff
        )

    # -- counter API -------------------------------------------------------
    def incr_with_window(self, key: str, window_seconds: int) -> int:
        return self._zset_incr(self._rl, key, window_seconds)

    def get_count(self, key: str, window_seconds: int) -> int:
        return self._zset_count(self._rl, key, window_seconds)

    def record_failed_attempt(self, key: str, window_seconds: int) -> int:
        return self._zset_incr(self._fa, key, window_seconds)

    # -- cache API ---------------------------------------------------------
    def cache_get(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        try:
            wrapper = json.loads(entry.value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(wrapper, dict):
            return None
        exp = wrapper.get("exp")
        if exp is not None and isinstance(exp, (int, float)) and exp <= time.time():
            return None
        value = wrapper.get("v")
        return value if isinstance(value, str) else None

    def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        wrapper = {"v": value, "exp": time.time() + ttl_seconds}
        payload = json.dumps(wrapper, separators=(",", ":")).encode()
        try:
            self._cache.put(key, payload)
        except Exception as exc:  # noqa: BLE001 - non-fatal cache write
            logger.warning("kv cache_set failed for %s: %s", key, exc)

    # -- admin API ---------------------------------------------------------
    def clear_counters(self) -> None:
        try:
            self._rl.purge()
            self._fa.purge()
        except Exception:  # noqa: BLE001 - best-effort admin op
            logger.exception("kv clear_counters partial failure")

    def active_rate_limit_keys(self) -> int:
        try:
            return len(self._rl.keys())
        except Exception:  # noqa: BLE001 - bucket might be empty
            logger.debug("kv active_rate_limit_keys() raised (treating as 0)", exc_info=True)
            return 0

    def total_failed_attempts(self, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        total = 0
        try:
            keys = self._fa.keys()
        except Exception:  # noqa: BLE001
            return 0
        for k in keys:
            entry = self._fa.get(k)
            if entry is None:
                continue
            try:
                timestamps = json.loads(entry.value)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            total += sum(
                1
                for t in timestamps
                if isinstance(t, (int, float)) and t >= cutoff
            )
        return total

    def prune_expired(
        self,
        rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
        failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
    ) -> dict:
        """Bucket TTL handles cold-key eviction; per-entry prune happens
        inline in ``_zset_incr``. This method is kept for parity with the
        DB backend so the sweeper can still call it without branching."""
        return {"rate_limit": 0, "failed_attempt": 0, "cache": 0}


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_storage: Optional[WebhookStorageBackend] = None
_logged_choice: bool = False


def get_storage() -> WebhookStorageBackend:
    """Return the lazily-instantiated singleton storage backend.

    Selection rules:
      * ``NATS_URL`` set                       -> JetStream KV (raises on failure)
      * else ``REDIS_URL`` set                 -> Redis (raises on failure)
      * otherwise                              -> DB fallback

    The first successful call logs which backend was selected so it shows
    up exactly once per worker process at startup.
    """
    global _storage, _logged_choice
    if _storage is not None:
        return _storage

    nats_url = os.environ.get("NATS_URL")
    redis_url = os.environ.get("REDIS_URL")

    if nats_url:
        try:
            _storage = JetStreamKVStorage()
        except Exception as exc:  # noqa: BLE001 - nats client raises varied concrete types; we re-raise unconditionally
            # Refuse to silently degrade when the operator explicitly
            # asked for NATS -- same policy as the Redis branch below.
            logger.error(
                "NATS_URL is set but JetStream is unreachable: %s. "
                "Refusing to silently fall back.", exc,
            )
            raise RuntimeError(
                f"Webhook storage backend NATS is unreachable: {exc}"
            ) from exc
    elif redis_url:
        try:
            _storage = RedisWebhookStorage(redis_url)
        except Exception as exc:  # noqa: BLE001 - redis client raises varied concrete types; we re-raise unconditionally
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
            "(NATS_URL > REDIS_URL > DB fallback)",
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


# ---------------------------------------------------------------------------
# Background sweeper
# ---------------------------------------------------------------------------
# A long-tail of one-shot source IPs is the worst case for the inline
# per-key prune in ``_incr``: rows for keys that never repeat sit in the
# table forever. The opportunistic sweep in ``_incr`` covers high-traffic
# apps; this background thread covers low-traffic apps where ``_incr``
# may not be called often enough to trigger the opportunistic sweep
# within the configured window. Together they bound the tables.
_sweeper_thread: Optional[threading.Thread] = None
_sweeper_stop: Optional[threading.Event] = None


def start_background_sweeper(
    app,
    storage: Optional[WebhookStorageBackend] = None,
    interval_seconds: int = 600,
    rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    failed_attempt_window_seconds: int = DEFAULT_FAILED_ATTEMPT_WINDOW_SECONDS,
) -> Optional[threading.Thread]:
    """Start a daemon thread that periodically calls ``prune_expired``.

    Idempotent: a second call is a no-op if a sweeper is already running.
    Returns the thread (or ``None`` if disabled / already running).

    Disable knobs (any of these short-circuits and returns ``None``):
      * ``WEBHOOK_STORAGE_SWEEPER_ENABLED`` set to ``0`` / ``false`` /
        ``no`` -- explicit opt-out (e.g. when running cron externally).
      * ``FLASK_ENV=test`` -- never start the thread under the test
        suite; tests drive ``prune_expired`` directly so they don't have
        to coordinate with a live thread.
    """
    global _sweeper_thread, _sweeper_stop

    if _sweeper_thread is not None and _sweeper_thread.is_alive():
        return None

    if os.environ.get("FLASK_ENV", "").lower() == "test":
        return None
    if os.environ.get("WEBHOOK_STORAGE_SWEEPER_ENABLED", "1").lower() in (
        "0",
        "false",
        "no",
    ):
        logger.info(
            "Webhook storage background sweeper disabled via "
            "WEBHOOK_STORAGE_SWEEPER_ENABLED"
        )
        return None

    backend = storage if storage is not None else get_storage()

    stop = threading.Event()

    def _run() -> None:
        # Sleep first so a fast-restart loop doesn't hammer the DB; the
        # opportunistic per-incr sweep still runs immediately for
        # high-traffic processes.
        while not stop.wait(interval_seconds):
            try:
                with app.app_context():
                    deleted = backend.prune_expired(
                        rate_limit_window_seconds=rate_limit_window_seconds,
                        failed_attempt_window_seconds=failed_attempt_window_seconds,
                    )
                    if any(deleted.values()):
                        logger.info(
                            "Webhook storage sweep removed %s",
                            deleted,
                        )
                    else:
                        logger.debug(
                            "Webhook storage sweep removed nothing"
                        )
            except Exception:  # noqa: BLE001 - sweeper must never die
                logger.exception(
                    "Webhook storage background sweep raised; "
                    "thread will retry on next interval"
                )

    thread = threading.Thread(
        target=_run,
        name="webhook-storage-sweeper",
        daemon=True,
    )
    thread.start()
    _sweeper_thread = thread
    _sweeper_stop = stop
    logger.info(
        "Webhook storage background sweeper started "
        "(interval=%ss, rl_window=%ss, fa_window=%ss)",
        interval_seconds,
        rate_limit_window_seconds,
        failed_attempt_window_seconds,
    )
    return thread


def stop_background_sweeper(timeout: float = 5.0) -> None:
    """Signal the background sweeper to exit and join it.

    Used by tests / graceful shutdown. Safe to call when no sweeper is
    running.
    """
    global _sweeper_thread, _sweeper_stop
    if _sweeper_stop is not None:
        _sweeper_stop.set()
    if _sweeper_thread is not None:
        _sweeper_thread.join(timeout=timeout)
    _sweeper_thread = None
    _sweeper_stop = None
