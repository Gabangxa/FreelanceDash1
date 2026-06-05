"""
Unit tests for ``JetStreamKVStorage.cache_add`` that do NOT require a live
NATS server.

The live contract tests (``test_storage_contract_nats.py``) only run when
``NATS_TEST_URL`` is set, so the JetStream-specific failure semantics would
otherwise go unverified in CI. Here we construct the backend with
``object.__new__`` (bypassing the connection-opening ``__init__``) and inject
a mock KV bucket so we can assert the critical fail-open behaviour:

  * a *definitive* "key already exists" (``KeyWrongLastSequenceError``) is the
    only thing that makes ``cache_add`` return ``False`` (lost claim);
  * a *transient* create error must NOT be swallowed into a ``False`` -- it
    must propagate so the webhook route's fail-open path processes the
    delivery instead of dropping it as a phantom duplicate.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from nats.js.errors import KeyWrongLastSequenceError
from webhooks.storage import JetStreamKVStorage


def _make_backend():
    backend = object.__new__(JetStreamKVStorage)
    backend._cache = MagicMock()
    return backend


def test_cache_add_returns_true_when_create_succeeds():
    backend = _make_backend()
    backend._cache.create.return_value = 1
    assert backend.cache_add("k", "v", 60) is True
    backend._cache.create.assert_called_once()


def test_cache_add_returns_false_on_definitive_conflict():
    backend = _make_backend()
    backend._cache.create.side_effect = KeyWrongLastSequenceError()
    # A live (unexpired) entry exists -> lost the claim.
    entry = MagicMock()
    entry.value = json.dumps({"v": "1", "exp": time.time() + 600}).encode()
    entry.revision = 7
    backend._cache.get.return_value = entry
    assert backend.cache_add("k", "v", 60) is False


def test_cache_add_propagates_transient_create_error():
    """A transient/ambiguous create failure must NOT become a False
    duplicate -- it must raise so the caller fails open and processes the
    webhook."""
    backend = _make_backend()
    backend._cache.create.side_effect = TimeoutError("kv timeout")
    with pytest.raises(TimeoutError):
        backend.cache_add("k", "v", 60)


def test_cache_add_reclaims_expired_entry():
    backend = _make_backend()
    backend._cache.create.side_effect = KeyWrongLastSequenceError()
    expired = MagicMock()
    expired.value = json.dumps({"v": "1", "exp": time.time() - 10}).encode()
    expired.revision = 3
    backend._cache.get.return_value = expired
    backend._cache.update.return_value = 4
    assert backend.cache_add("k", "v", 60) is True
    backend._cache.update.assert_called_once()


def test_cache_add_propagates_transient_update_error_on_reclaim():
    """If the expired-slot reclaim CAS hits a transient (non-conflict)
    error, it must propagate rather than masquerade as a lost claim."""
    backend = _make_backend()
    backend._cache.create.side_effect = KeyWrongLastSequenceError()
    expired = MagicMock()
    expired.value = json.dumps({"v": "1", "exp": time.time() - 10}).encode()
    expired.revision = 3
    backend._cache.get.return_value = expired
    backend._cache.update.side_effect = TimeoutError("kv timeout")
    with pytest.raises(TimeoutError):
        backend.cache_add("k", "v", 60)
