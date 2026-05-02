"""
JetStream KV storage contract tests.

Skipped unless ``NATS_TEST_URL`` is set. The CI environment that runs
``pytest`` by default does not have a NATS server, so we don't want a
hard failure there -- but if you're developing the JetStream backend
locally with ``nats-server -js`` running, set
``NATS_TEST_URL=nats://127.0.0.1:4222`` and these will exercise the
real bucket round-trips.
"""
from __future__ import annotations

import os

import pytest

from tests import storage_contract


pytestmark = pytest.mark.skipif(
    not os.environ.get("NATS_TEST_URL"),
    reason="NATS_TEST_URL not set; skipping live JetStream contract tests",
)


@pytest.fixture()
def nats_backend(monkeypatch):
    """Connect to the test NATS server, yield a fresh JetStream KV
    backend, then purge all buckets so the next test starts clean."""
    import nats_client
    from webhooks.storage import JetStreamKVStorage

    monkeypatch.setenv("NATS_URL", os.environ["NATS_TEST_URL"])
    nats_client.reset_for_tests()
    nats_client.init()
    backend = JetStreamKVStorage()
    # Purge anything left over from previous tests.
    backend._rl.purge()
    backend._fa.purge()
    backend._cache.purge()
    yield backend
    backend._rl.purge()
    backend._fa.purge()
    backend._cache.purge()
    nats_client.shutdown()
    nats_client.reset_for_tests()


@pytest.mark.parametrize("contract_fn", storage_contract.ALL_CONTRACTS)
def test_jetstream_kv_satisfies_contract(nats_backend, contract_fn):
    """Run every contract function from ``tests/storage_contract.py``
    against the live JetStream KV backend."""
    contract_fn(nats_backend)
