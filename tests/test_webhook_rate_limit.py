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
    start_background_sweeper,
    stop_background_sweeper,
)
from tests import storage_contract


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
# Shared backend contract -- run the same suite that test_storage_contract_nats
# runs against JetStream so the DB and NATS backends provably satisfy the
# same WebhookStorageBackend semantics. New backends only need to wire
# this in once.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("contract_fn", storage_contract.ALL_CONTRACTS)
def test_db_backend_satisfies_storage_contract(app, contract_fn):
    """Run every contract function from ``tests/storage_contract.py``
    against the DB-backed storage backend."""
    with app.app_context():
        contract_fn(get_storage())


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
        # Cache populated with the just-fetched list plus the
        # fetched-at / origin metadata used by the admin status panel.
        cached = get_storage().cache_get("ip_ranges:github")
        assert cached is not None
        payload = json.loads(cached)
        assert payload["ranges"] == fresh
        assert payload["origin"] == ip_ranges.ORIGIN_UPSTREAM
        assert payload["fetched_at"] is not None


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


# ---------------------------------------------------------------------------
# prune_expired(): the global sweep that bounds the tables under a long
# tail of one-shot source IPs (the inline per-key prune in _incr only
# fires when the same key is hit again).
# ---------------------------------------------------------------------------
def test_prune_expired_drops_stale_rate_limit_rows_for_one_shot_keys(app):
    """Drive-by IPs that hit the webhook endpoint once and never come
    back must be reaped by the global sweep, not left in the table
    forever."""
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        # Seed 50 distinct keys, each with a row from "two hours ago".
        # Without the global sweep these would never be pruned because
        # no future incr() call ever uses the same key again.
        old_ts = datetime.utcnow() - timedelta(seconds=7200)
        for i in range(50):
            db.session.add(
                WebhookRateLimitEvent(
                    rate_key=f"github:10.0.0.{i}", created_at=old_ts
                )
            )
        # Plus a few rows that are still inside the 1h window: those
        # must NOT be pruned.
        fresh_ts = datetime.utcnow() - timedelta(seconds=60)
        for i in range(5):
            db.session.add(
                WebhookRateLimitEvent(
                    rate_key=f"github:fresh.{i}", created_at=fresh_ts
                )
            )
        db.session.commit()
        assert db.session.query(WebhookRateLimitEvent).count() == 55

        deleted = storage.prune_expired(
            rate_limit_window_seconds=3600,
            failed_attempt_window_seconds=3600,
        )
        assert deleted["rate_limit"] == 50
        # Only the fresh rows remain.
        remaining = db.session.query(WebhookRateLimitEvent).count()
        assert remaining == 5


def test_prune_expired_drops_stale_failed_attempts(app):
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        old_ts = datetime.utcnow() - timedelta(seconds=7200)
        for i in range(20):
            db.session.add(
                WebhookFailedAttempt(
                    attempt_key=f"stripe:11.0.0.{i}", created_at=old_ts
                )
            )
        db.session.add(
            WebhookFailedAttempt(
                attempt_key="stripe:active",
                created_at=datetime.utcnow(),
            )
        )
        db.session.commit()

        deleted = storage.prune_expired(
            rate_limit_window_seconds=3600,
            failed_attempt_window_seconds=3600,
        )
        assert deleted["failed_attempt"] == 20
        assert db.session.query(WebhookFailedAttempt).count() == 1


def test_prune_expired_drops_expired_cache_entries(app):
    from datetime import datetime, timedelta

    storage = get_storage()
    with app.app_context():
        # Expired entry.
        expired = WebhookCacheEntry()
        expired.cache_key = "stale-key"
        expired.value = "old-value"
        expired.expires_at = datetime.utcnow() - timedelta(seconds=10)
        db.session.add(expired)
        # Fresh entry.
        fresh = WebhookCacheEntry()
        fresh.cache_key = "fresh-key"
        fresh.value = "new-value"
        fresh.expires_at = datetime.utcnow() + timedelta(seconds=600)
        db.session.add(fresh)
        db.session.commit()

        deleted = storage.prune_expired()
        assert deleted["cache"] == 1
        # Fresh entry survives; stale entry is gone.
        remaining_keys = {
            row.cache_key
            for row in db.session.query(WebhookCacheEntry).all()
        }
        assert remaining_keys == {"fresh-key"}


def test_prune_expired_is_safe_when_tables_empty(app):
    storage = get_storage()
    with app.app_context():
        deleted = storage.prune_expired()
        assert deleted == {
            "rate_limit": 0,
            "failed_attempt": 0,
            "cache": 0,
        }


def test_table_count_stays_bounded_under_one_shot_ip_load(app, monkeypatch):
    """End-to-end version of the task's stated invariant: a synthetic
    flood of distinct one-shot source IPs must NOT leave the table
    growing without bound. The opportunistic sweep in ``_incr`` (every
    Nth call) is what bounds it -- this test forces a small N so the
    sweep fires inside the test and does its job."""
    from datetime import datetime, timedelta

    # Crank the opportunistic-sweep cadence way down so we don't have to
    # generate hundreds of requests inside the test.
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEP_EVERY", "10")
    reset_storage_for_tests()
    set_storage_for_tests(DBWebhookStorage())
    storage = get_storage()

    with app.app_context():
        # Pre-seed 200 stale rows from "drive-by" IPs that will never be
        # hit again. The inline per-key prune cannot reach these.
        old_ts = datetime.utcnow() - timedelta(seconds=7200)
        for i in range(200):
            db.session.add(
                WebhookRateLimitEvent(
                    rate_key=f"github:driveby.{i}", created_at=old_ts
                )
            )
        db.session.commit()
        assert db.session.query(WebhookRateLimitEvent).count() == 200

        # Now drive enough live traffic to trigger at least one
        # opportunistic sweep (every 10 incr calls per the env above).
        for i in range(15):
            storage.incr_with_window(f"github:live.{i}", 60)

        # All 200 stale rows must be gone, and only the 15 live ones
        # should remain. The table is bounded.
        remaining = db.session.query(WebhookRateLimitEvent).count()
        assert remaining == 15, (
            f"Expected stale drive-by rows to be reaped by the "
            f"opportunistic sweep, but {remaining} rows remain"
        )


def test_opportunistic_sweep_does_not_fire_too_often(app, monkeypatch):
    """The opportunistic sweep must NOT run on every _incr call -- that
    would tank throughput on a busy webhook endpoint. It should only
    fire every Nth call as configured."""
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEP_EVERY", "100")
    reset_storage_for_tests()
    set_storage_for_tests(DBWebhookStorage())
    storage = get_storage()

    sweep_calls = {"n": 0}
    real_prune = storage.prune_expired

    def _counting_prune(*args, **kwargs):
        sweep_calls["n"] += 1
        return real_prune(*args, **kwargs)

    with app.app_context():
        with patch.object(storage, "prune_expired", side_effect=_counting_prune):
            for i in range(50):
                storage.incr_with_window(f"k{i}", 60)
        # 50 incr calls with sweep-every=100 -> zero opportunistic sweeps.
        assert sweep_calls["n"] == 0


def test_opportunistic_sweep_failure_does_not_break_incr(app, monkeypatch, caplog):
    """If the sweep raises, the caller's increment must still succeed
    (and return the correct count) -- the sweep is best-effort."""
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEP_EVERY", "1")
    reset_storage_for_tests()
    set_storage_for_tests(DBWebhookStorage())
    storage = get_storage()

    with app.app_context():
        with patch.object(
            storage,
            "prune_expired",
            side_effect=RuntimeError("simulated sweep failure"),
        ):
            # First incr triggers a sweep (sweep-every=1) which raises.
            # The incr itself must still return 1, not raise.
            count = storage.incr_with_window("github:1.2.3.4", 60)
            assert count == 1
            # The row is in the table.
            assert (
                db.session.query(WebhookRateLimitEvent)
                .filter_by(rate_key="github:1.2.3.4")
                .count()
                == 1
            )


# ---------------------------------------------------------------------------
# Background sweeper thread
# ---------------------------------------------------------------------------
def test_start_background_sweeper_skipped_under_test_env(app, monkeypatch):
    """The sweeper must not spawn a thread when FLASK_ENV=test --
    otherwise the test suite would race a live sweeper against fixtures
    that wipe / re-seed the tables."""
    monkeypatch.setenv("FLASK_ENV", "test")
    thread = start_background_sweeper(app, interval_seconds=1)
    try:
        assert thread is None
    finally:
        stop_background_sweeper()


def test_start_background_sweeper_skipped_when_disabled_env(app, monkeypatch):
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEPER_ENABLED", "0")
    thread = start_background_sweeper(app, interval_seconds=1)
    try:
        assert thread is None
    finally:
        stop_background_sweeper()


def test_background_sweeper_runs_prune_expired_periodically(app, monkeypatch):
    """When the sweeper IS allowed to run, it must periodically call
    prune_expired() inside an app_context, which deletes drive-by rows
    that no longer have any matching incr() traffic to clean them up."""
    import time as _time
    from datetime import datetime, timedelta

    # Allow the sweeper to actually start under the test runner.
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEPER_ENABLED", "1")

    with app.app_context():
        old_ts = datetime.utcnow() - timedelta(seconds=7200)
        for i in range(10):
            db.session.add(
                WebhookRateLimitEvent(
                    rate_key=f"github:cold.{i}", created_at=old_ts
                )
            )
        db.session.commit()
        assert db.session.query(WebhookRateLimitEvent).count() == 10

    # Use a sub-second interval so the test doesn't have to sleep long.
    thread = start_background_sweeper(
        app,
        interval_seconds=1,
        rate_limit_window_seconds=3600,
        failed_attempt_window_seconds=3600,
    )
    try:
        assert thread is not None
        assert thread.is_alive()

        # Wait for at least one sweep iteration. The first sweep fires
        # after the first interval (the loop sleeps before the first
        # tick to avoid a fast-restart hammer). 3 seconds is plenty
        # of slack for the 1s interval.
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            with app.app_context():
                if db.session.query(WebhookRateLimitEvent).count() == 0:
                    break
            _time.sleep(0.1)

        with app.app_context():
            remaining = db.session.query(WebhookRateLimitEvent).count()
        assert remaining == 0, (
            f"Background sweeper should have reaped all 10 stale rows, "
            f"but {remaining} remain after waiting"
        )
    finally:
        stop_background_sweeper()


def test_background_sweeper_is_idempotent(app, monkeypatch):
    """Calling start_background_sweeper twice must not spawn two
    threads -- gunicorn workers each start the app once but blueprints
    can re-import storage."""
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("WEBHOOK_STORAGE_SWEEPER_ENABLED", "1")

    first = start_background_sweeper(app, interval_seconds=60)
    try:
        second = start_background_sweeper(app, interval_seconds=60)
        assert first is not None
        assert second is None  # already running
    finally:
        stop_background_sweeper()


# ---------------------------------------------------------------------------
# IP allowlist status (admin status panel feed)
# ---------------------------------------------------------------------------
def test_get_status_reports_fallback_when_cache_empty(app):
    """With no cache entry yet, get_status must report the static
    fallback list with origin=fallback and cached=False so the admin
    panel doesn't show a confusingly-empty allowlist before the
    boot-time refresh has run."""
    with app.app_context():
        status = ip_ranges.get_status("github")
    assert status["source"] == "github"
    assert status["origin"] == ip_ranges.ORIGIN_FALLBACK
    assert status["cached"] is False
    assert status["fetched_at"] is None
    assert status["range_count"] == len(ip_ranges.FALLBACK_RANGES["github"])


def test_get_status_reports_upstream_after_successful_refresh(app):
    fresh = ["192.0.2.0/24", "203.0.113.5/32"]
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": fresh}),
    ):
        assert ip_ranges.refresh_now("github") is True
        status = ip_ranges.get_status("github")
    assert status["source"] == "github"
    assert status["origin"] == ip_ranges.ORIGIN_UPSTREAM
    assert status["cached"] is True
    assert status["range_count"] == len(fresh)
    # ISO-8601 UTC timestamp ending in Z (see _utc_now_iso).
    assert status["fetched_at"] is not None
    assert status["fetched_at"].endswith("Z")


def test_get_status_reports_fallback_origin_after_outage(app):
    """After ``get_ranges`` has fallen back to the static list during
    an upstream outage the cached entry must carry origin=fallback so
    the admin panel can flag the source as unhealthy."""
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        side_effect=ConnectionError("upstream down"),
    ):
        ip_ranges.get_ranges("github")
        status = ip_ranges.get_status("github")
    assert status["origin"] == ip_ranges.ORIGIN_FALLBACK
    assert status["cached"] is True
    assert status["range_count"] == len(ip_ranges.FALLBACK_RANGES["github"])
    assert status["fetched_at"] is not None


def test_get_status_for_unknown_source_is_empty(app):
    with app.app_context():
        status = ip_ranges.get_status("definitely-not-a-source")
    assert status["range_count"] == 0
    assert status["origin"] == ip_ranges.ORIGIN_UNKNOWN
    assert status["cached"] is False


def test_all_statuses_returns_one_entry_per_known_source(app):
    with app.app_context():
        statuses = ip_ranges.all_statuses()
    sources = {s["source"] for s in statuses}
    assert sources == set(ip_ranges.FALLBACK_RANGES.keys())


def test_get_ranges_tolerates_legacy_bare_list_cache(app):
    """A cache entry primed with the pre-metadata bare-list shape must
    still serve the ranges (so a rolling deploy doesn't invalidate
    every cached entry the moment the new code ships)."""
    with app.app_context():
        get_storage().cache_set(
            "ip_ranges:github",
            json.dumps(["10.0.0.0/8", "172.16.0.0/12"]),
            3600,
        )
        # No HTTP allowed: the legacy list must be honoured directly.
        with patch.object(
            ip_ranges.requests, "get", side_effect=AssertionError("no http")
        ):
            assert ip_ranges.get_ranges("github") == [
                "10.0.0.0/8", "172.16.0.0/12"
            ]
        # ...and reported with origin=unknown so the admin panel can
        # surface that the metadata is missing.
        status = ip_ranges.get_status("github")
        assert status["origin"] == ip_ranges.ORIGIN_UNKNOWN
        assert status["range_count"] == 2


# ---------------------------------------------------------------------------
# /webhooks/security/status endpoint surfaces backend + IP allowlist health
# ---------------------------------------------------------------------------
def test_security_status_endpoint_includes_backend_and_allowlist(app, client):
    """The admin status endpoint must expose the active storage backend
    name and a per-source allowlist health snapshot so operators don't
    have to grep server logs to know whether the dynamic refresh is
    healthy."""
    app.config["WEBHOOK_ADMIN_TOKEN"] = "test-admin-token"
    fresh = ["192.0.2.0/24"]
    with app.app_context(), patch.object(
        ip_ranges.requests,
        "get",
        return_value=_fake_response({"hooks": fresh}),
    ):
        ip_ranges.refresh_now("github")

    resp = client.get(
        "/webhooks/security/status",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 200
    body = resp.get_json()

    assert body["storage_backend"] == get_storage().name
    assert isinstance(body["ip_allowlist"], list)
    by_source = {entry["source"]: entry for entry in body["ip_allowlist"]}
    assert "github" in by_source and "stripe" in by_source

    gh = by_source["github"]
    assert gh["origin"] == ip_ranges.ORIGIN_UPSTREAM
    assert gh["cached"] is True
    assert gh["range_count"] == len(fresh)
    assert gh["fetched_at"] is not None

    # Stripe was never refreshed so it must still report the static
    # fallback rather than crashing or being absent.
    stripe_entry = by_source["stripe"]
    assert stripe_entry["origin"] == ip_ranges.ORIGIN_FALLBACK
    assert stripe_entry["cached"] is False
    assert stripe_entry["range_count"] == len(
        ip_ranges.FALLBACK_RANGES["stripe"]
    )
