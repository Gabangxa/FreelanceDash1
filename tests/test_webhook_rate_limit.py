"""
Coverage for the persistent webhook rate-limit / failed-attempt storage
and the dynamic IP allowlist fetcher.

These tests exercise the DB-backed storage backend (which is what the
default test environment uses, since ``REDIS_URL`` is unset) and mock
the upstream HTTP calls for the IP-range fetcher so the suite stays
hermetic.
"""
import json
from unittest.mock import patch

import pytest

from app import db
from models import (
    WebhookCacheEntry,
    WebhookFailedAttempt,
    WebhookRateLimitEvent,
)
from webhooks import ip_ranges
from webhooks.storage import (
    DBWebhookStorage,
    get_storage,
    reset_storage_for_tests,
    set_storage_for_tests,
)


@pytest.fixture(autouse=True)
def _isolated_db_storage(app):
    """Each test gets a fresh DB-backed storage backend and an empty set
    of rate-limit / failed-attempt / cache rows so they don't bleed into
    each other."""
    reset_storage_for_tests()
    set_storage_for_tests(DBWebhookStorage())
    with app.app_context():
        db.session.query(WebhookRateLimitEvent).delete()
        db.session.query(WebhookFailedAttempt).delete()
        db.session.query(WebhookCacheEntry).delete()
        db.session.commit()
    yield
    reset_storage_for_tests()


# ---------------------------------------------------------------------------
# Rate-limit counter behaviour
# ---------------------------------------------------------------------------
def test_incr_with_window_returns_increasing_count(app):
    storage = get_storage()
    with app.app_context():
        assert storage.incr_with_window("github:1.2.3.4", 60) == 1
        assert storage.incr_with_window("github:1.2.3.4", 60) == 2
        assert storage.incr_with_window("github:1.2.3.4", 60) == 3


def test_incr_with_window_is_keyed_per_client(app):
    storage = get_storage()
    with app.app_context():
        assert storage.incr_with_window("github:1.1.1.1", 60) == 1
        assert storage.incr_with_window("github:2.2.2.2", 60) == 1
        assert storage.incr_with_window("github:1.1.1.1", 60) == 2


def test_incr_with_window_prunes_expired_entries(app):
    """Entries older than the window must be dropped on the next incr."""
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        # Seed 3 events from "an hour ago" plus one fresh one.
        old_ts = datetime.utcnow() - timedelta(seconds=3600)
        for _ in range(3):
            row = WebhookRateLimitEvent(
                rate_key="github:5.5.5.5", created_at=old_ts
            )
            db.session.add(row)
        db.session.commit()

        # Window of 60s -- the 3 old rows should be pruned, leaving only
        # the just-inserted row.
        assert storage.incr_with_window("github:5.5.5.5", 60) == 1


def test_get_count_does_not_create_rows(app):
    storage = get_storage()
    with app.app_context():
        assert storage.get_count("github:9.9.9.9", 60) == 0
        assert (
            db.session.query(WebhookRateLimitEvent)
            .filter_by(rate_key="github:9.9.9.9")
            .count()
            == 0
        )


# ---------------------------------------------------------------------------
# Failed-attempt tracking
# ---------------------------------------------------------------------------
def test_record_failed_attempt_increments(app):
    storage = get_storage()
    with app.app_context():
        for expected in (1, 2, 3):
            assert (
                storage.record_failed_attempt("stripe:9.9.9.9", 3600)
                == expected
            )


def test_record_failed_attempt_prunes_old(app):
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        old_ts = datetime.utcnow() - timedelta(seconds=7200)
        db.session.add(
            WebhookFailedAttempt(
                attempt_key="stripe:7.7.7.7", created_at=old_ts
            )
        )
        db.session.commit()
        # New entry inserted, the old row outside the 1h window pruned.
        assert (
            storage.record_failed_attempt("stripe:7.7.7.7", 3600) == 1
        )


def test_clear_counters_drops_everything(app):
    storage = get_storage()
    with app.app_context():
        storage.incr_with_window("github:1.1.1.1", 60)
        storage.record_failed_attempt("github:1.1.1.1", 3600)

        storage.clear_counters()

        assert db.session.query(WebhookRateLimitEvent).count() == 0
        assert db.session.query(WebhookFailedAttempt).count() == 0


def test_total_failed_attempts_sums_across_keys(app):
    storage = get_storage()
    with app.app_context():
        storage.record_failed_attempt("stripe:1.1.1.1", 3600)
        storage.record_failed_attempt("stripe:1.1.1.1", 3600)
        storage.record_failed_attempt("github:2.2.2.2", 3600)
        assert storage.total_failed_attempts(3600) == 3


def test_active_rate_limit_keys_counts_distinct_keys(app):
    storage = get_storage()
    with app.app_context():
        storage.incr_with_window("a:1", 60)
        storage.incr_with_window("a:1", 60)
        storage.incr_with_window("a:2", 60)
        storage.incr_with_window("a:3", 60)
        assert storage.active_rate_limit_keys() == 3


# ---------------------------------------------------------------------------
# Cache (used by IP-ranges fetcher)
# ---------------------------------------------------------------------------
def test_cache_get_returns_none_for_missing(app):
    storage = get_storage()
    with app.app_context():
        assert storage.cache_get("missing") is None


def test_cache_set_and_get_roundtrip(app):
    storage = get_storage()
    with app.app_context():
        storage.cache_set("mykey", "myvalue", 60)
        assert storage.cache_get("mykey") == "myvalue"


def test_cache_set_overwrites_existing(app):
    storage = get_storage()
    with app.app_context():
        storage.cache_set("k", "v1", 60)
        storage.cache_set("k", "v2", 60)
        assert storage.cache_get("k") == "v2"


def test_cache_get_returns_none_for_expired(app):
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        entry = WebhookCacheEntry()
        entry.cache_key = "old"
        entry.value = "stale"
        entry.expires_at = datetime.utcnow() - timedelta(seconds=10)
        db.session.add(entry)
        db.session.commit()
        assert storage.cache_get("old") is None


# ---------------------------------------------------------------------------
# Dynamic IP allowlist fetcher
# ---------------------------------------------------------------------------
def _fake_response(payload, status=200):
    class _Resp:
        def __init__(self):
            self.status_code = status
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    return _Resp()


def test_get_ranges_uses_freshly_fetched_github_list(app):
    fresh = ["192.0.2.0/24", "203.0.113.5/32"]
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": fresh}),
    ) as mock_get:
        out = ip_ranges.get_ranges("github")
        assert out == fresh
        mock_get.assert_called_once()


def test_get_ranges_normalises_stripe_bare_ips(app):
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"WEBHOOKS": ["1.2.3.4", "5.6.7.8/32"]}),
    ):
        out = ip_ranges.get_ranges("stripe")
        # Stripe lists bare IPs; the fetcher must normalise to /32 so
        # ipaddress.ip_network() accepts them downstream.
        assert out == ["1.2.3.4/32", "5.6.7.8/32"]


def test_get_ranges_caches_subsequent_calls(app):
    fresh = ["198.51.100.0/24"]
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": fresh}),
    ) as mock_get:
        # First call: hits upstream and primes the cache.
        first = ip_ranges.get_ranges("github")
        # Second call: must come from cache, no HTTP.
        second = ip_ranges.get_ranges("github")
        assert first == second == fresh
        assert mock_get.call_count == 1


def test_get_ranges_falls_back_to_static_on_fetch_error(app):
    # ConnectionError is the realistic transient-network case.
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        side_effect=ConnectionError("upstream down"),
    ):
        out = ip_ranges.get_ranges("github")
        assert out == list(ip_ranges.FALLBACK_RANGES["github"])


def test_fallback_is_cached_so_outage_doesnt_hammer_upstream(app):
    """When upstream is unreachable and the cache is empty, get_ranges()
    must cache the static fallback under a short backoff TTL so that
    subsequent webhook requests in the same window read from cache and
    do NOT re-hit upstream. Otherwise an outage at GitHub/Stripe would
    turn every inbound webhook into an outbound HTTP call."""
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        side_effect=ConnectionError("upstream down"),
    ) as mock_get:
        # First call: cache miss + upstream fails -> static fallback
        # returned and cached under FALLBACK_CACHE_TTL_SECONDS.
        first = ip_ranges.get_ranges("github")
        # Second and third calls within the backoff window must be served
        # entirely from cache -- no extra upstream hits.
        second = ip_ranges.get_ranges("github")
        third = ip_ranges.get_ranges("github")
        assert first == second == third == list(
            ip_ranges.FALLBACK_RANGES["github"]
        )
        assert mock_get.call_count == 1, (
            "Expected exactly one upstream HTTP call across three "
            f"get_ranges() invocations during outage, got {mock_get.call_count}"
        )


def test_get_ranges_falls_back_when_payload_is_empty(app):
    """An upstream response that is well-formed but has no IPs (e.g.
    GitHub returned ``hooks: []``) must not silently empty the allowlist
    -- treat it as a fetch failure and use the static fallback."""
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": []}),
    ):
        out = ip_ranges.get_ranges("github")
        assert out == list(ip_ranges.FALLBACK_RANGES["github"])


def test_get_ranges_returns_empty_for_unknown_source(app):
    with app.app_context():
        assert ip_ranges.get_ranges("unknown_source") == []


def test_corrupt_cache_triggers_refetch(app):
    """If the cached payload is malformed JSON, the fetcher must log,
    refetch from upstream, and overwrite the bad entry rather than
    falling back to the static list (which would skip the dynamic
    refresh entirely)."""
    with app.app_context():
        get_storage().cache_set(
            "ip_ranges:github", "{not-json}", 3600
        )
        with patch.object(
            ip_ranges.requests,
            "get",
            return_value=_fake_response({"hooks": ["10.0.0.0/8"]}),
        ) as mock_get:
            out = ip_ranges.get_ranges("github")
            assert out == ["10.0.0.0/8"]
            mock_get.assert_called_once()


def test_refresh_now_returns_false_on_failure(app):
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        side_effect=RuntimeError("boom"),
    ):
        assert ip_ranges.refresh_now("github") is False


def test_refresh_now_returns_true_and_caches_on_success(app):
    fresh = ["192.0.2.0/24"]
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": fresh}),
    ):
        assert ip_ranges.refresh_now("github") is True
        # Cache populated with the just-fetched list.
        cached = get_storage().cache_get("ip_ranges:github")
        assert cached is not None
        assert json.loads(cached) == fresh


# ---------------------------------------------------------------------------
# WebhookSecurity integration: confirm the call signatures still work and
# the underlying counters live in the shared backend.
# ---------------------------------------------------------------------------
def test_check_rate_limit_uses_shared_storage(app):
    """A handful of requests from the same IP for a low-limit source
    must trip the 429, and the count must be visible via the storage
    backend (proving it's not a process-local dict)."""
    from webhooks.security import WebhookSecurity, WebhookSecurityError

    # Override the source-specific limit so we can trip it cheaply
    # without making 1000+ calls.
    original = WebhookSecurity.RATE_LIMITS.get("github")
    WebhookSecurity.RATE_LIMITS["github"] = {"requests": 3, "window": 60}
    try:
        with app.test_request_context(
            "/webhooks/receive/github", environ_base={"REMOTE_ADDR": "8.8.8.8"}
        ):
            WebhookSecurity.check_rate_limit("github")
            WebhookSecurity.check_rate_limit("github")
            WebhookSecurity.check_rate_limit("github")
            with pytest.raises(WebhookSecurityError) as excinfo:
                WebhookSecurity.check_rate_limit("github")
            assert excinfo.value.status_code == 429

        with app.app_context():
            count = get_storage().get_count("github:8.8.8.8", 60)
            # 4 incr calls happened above (the 4th raised AFTER inserting,
            # because the storage layer counts the new event before the
            # cap check). Either 3 or 4 is acceptable; what matters is
            # that the counter persisted in the shared store, not in a
            # process-local dict.
            assert count >= 3
    finally:
        if original is not None:
            WebhookSecurity.RATE_LIMITS["github"] = original


def test_track_failed_attempt_writes_to_shared_storage(app):
    from webhooks.security import WebhookSecurity

    with app.test_request_context(
        "/webhooks/receive/github", environ_base={"REMOTE_ADDR": "4.4.4.4"}
    ):
        WebhookSecurity.track_failed_attempt("github")
        WebhookSecurity.track_failed_attempt("github")

    with app.app_context():
        rows = (
            db.session.query(WebhookFailedAttempt)
            .filter_by(attempt_key="github:4.4.4.4")
            .count()
        )
        assert rows == 2


# ---------------------------------------------------------------------------
# Storage backend selection: fail fast on Redis misconfig, never silently
# degrade to in-memory or DB when the operator explicitly asked for Redis.
# ---------------------------------------------------------------------------
def test_get_storage_raises_when_redis_url_set_but_unreachable(monkeypatch):
    """If REDIS_URL is set and Redis can't be reached, get_storage() must
    raise rather than quietly fall back to the DB backend -- otherwise a
    misconfigured prod deploy would silently lose the cross-worker
    counter consistency the operator was relying on."""
    from webhooks import storage as storage_mod

    storage_mod.reset_storage_for_tests()
    # Point at a guaranteed-unreachable Redis. No daemon listens here in
    # the test environment, so .ping() will raise during construction.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    try:
        with pytest.raises(RuntimeError, match="Redis is unreachable"):
            storage_mod.get_storage()
    finally:
        storage_mod.reset_storage_for_tests()


def test_get_storage_uses_db_when_redis_url_unset(monkeypatch):
    """With no REDIS_URL the singleton must come up as the DB fallback,
    not raise."""
    from webhooks import storage as storage_mod

    storage_mod.reset_storage_for_tests()
    monkeypatch.delenv("REDIS_URL", raising=False)
    try:
        backend = storage_mod.get_storage()
        assert backend.name == "database"
    finally:
        storage_mod.reset_storage_for_tests()
