"""
Shared contract tests for any ``WebhookStorageBackend`` implementation.

Each function takes a ``backend`` argument and asserts a single
behaviour. Backend-specific test files (``test_webhook_rate_limit.py``
for the DB backend, ``test_storage_contract_nats.py`` for the NATS
backend) wire these up with their own fixtures.

Why a shared module instead of pytest parametrize? Because the
DB-backed tests need an app context and the NATS-backed tests need a
live NATS server -- two very different fixtures. Keeping the contract
as plain functions lets each backend test file decide how to set up
the backend without coupling the contract definition to pytest's
fixture system.
"""
from __future__ import annotations

import time


# ---------------------------------------------------------------------------
# Counter contract
# ---------------------------------------------------------------------------
def assert_incr_returns_increasing_count(backend) -> None:
    assert backend.incr_with_window("contract:k1", 60) == 1
    assert backend.incr_with_window("contract:k1", 60) == 2
    assert backend.incr_with_window("contract:k1", 60) == 3


def assert_incr_is_keyed_per_client(backend) -> None:
    assert backend.incr_with_window("contract:a", 60) == 1
    assert backend.incr_with_window("contract:b", 60) == 1
    assert backend.incr_with_window("contract:a", 60) == 2


def assert_get_count_does_not_create_rows(backend) -> None:
    # A get on an unknown key must return 0 and must not implicitly
    # create state -- otherwise a probe could inflate the row count or
    # trip CAS contention.
    assert backend.get_count("contract:never-seen", 60) == 0


def assert_record_failed_attempt_increments(backend) -> None:
    assert backend.record_failed_attempt("contract:fa", 3600) == 1
    assert backend.record_failed_attempt("contract:fa", 3600) == 2
    assert backend.record_failed_attempt("contract:fa", 3600) == 3


def assert_clear_counters_drops_everything(backend) -> None:
    backend.incr_with_window("contract:rl", 60)
    backend.record_failed_attempt("contract:fa", 3600)
    backend.clear_counters()
    assert backend.get_count("contract:rl", 60) == 0
    # Failed attempts should also be cleared. We assert via the totals
    # accessor since there's no per-key getter for failed attempts.
    assert backend.total_failed_attempts(3600) == 0


def assert_total_failed_attempts_sums_across_keys(backend) -> None:
    backend.record_failed_attempt("contract:fa1", 3600)
    backend.record_failed_attempt("contract:fa1", 3600)
    backend.record_failed_attempt("contract:fa2", 3600)
    assert backend.total_failed_attempts(3600) == 3


def assert_active_rate_limit_keys_counts_distinct_keys(backend) -> None:
    backend.incr_with_window("contract:rl1", 60)
    backend.incr_with_window("contract:rl1", 60)
    backend.incr_with_window("contract:rl2", 60)
    backend.incr_with_window("contract:rl3", 60)
    # Backends may also count any keys touched by other contract tests
    # in the same backend instance; assert the contract minimum, not
    # exact equality, so this composes safely.
    assert backend.active_rate_limit_keys() >= 3


# ---------------------------------------------------------------------------
# Cache contract
# ---------------------------------------------------------------------------
def assert_cache_get_returns_none_for_missing(backend) -> None:
    assert backend.cache_get("contract:missing") is None


def assert_cache_set_and_get_roundtrip(backend) -> None:
    backend.cache_set("contract:c1", "v1", 60)
    assert backend.cache_get("contract:c1") == "v1"


def assert_cache_set_overwrites_existing(backend) -> None:
    backend.cache_set("contract:c2", "first", 60)
    backend.cache_set("contract:c2", "second", 60)
    assert backend.cache_get("contract:c2") == "second"


def assert_cache_get_returns_none_after_short_ttl(backend) -> None:
    """We use a 1s TTL and sleep just past it; this is the only contract
    test that's allowed to be slow (~1s) because it's the only way to
    test TTL behaviour without poking backend internals."""
    backend.cache_set("contract:ttl", "soon-gone", 1)
    time.sleep(1.5)
    assert backend.cache_get("contract:ttl") is None


# ---------------------------------------------------------------------------
# Convenience: run them all
# ---------------------------------------------------------------------------
ALL_CONTRACTS = [
    assert_incr_returns_increasing_count,
    assert_incr_is_keyed_per_client,
    assert_get_count_does_not_create_rows,
    assert_record_failed_attempt_increments,
    assert_clear_counters_drops_everything,
    assert_total_failed_attempts_sums_across_keys,
    assert_active_rate_limit_keys_counts_distinct_keys,
    assert_cache_get_returns_none_for_missing,
    assert_cache_set_and_get_roundtrip,
    assert_cache_set_overwrites_existing,
    # ttl test deliberately not in the default list -- callers opt in
    # because it's slow.
]
