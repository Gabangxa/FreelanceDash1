"""
Tests for the no-op stub semantics of nats_client + events.publish.

The whole point of the NATS_URL gate is that the entire feature must
be invisible in environments that don't set it (dev, CI, the existing
test suite). These tests pin that contract.

We also verify the envelope shape so a future subscriber service can
rely on it without reading source.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import events
import nats_client


@pytest.fixture(autouse=True)
def _reset_nats_state(monkeypatch):
    """Ensure each test starts with a clean module state and NATS_URL
    cleared. Tests that want NATS_URL set re-set it themselves."""
    monkeypatch.delenv("NATS_URL", raising=False)
    monkeypatch.delenv("NATS_CREDS_PATH", raising=False)
    nats_client.reset_for_tests()
    yield
    nats_client.reset_for_tests()


# ---------------------------------------------------------------------------
# is_enabled / init / state -- the gate
# ---------------------------------------------------------------------------
def test_is_enabled_false_when_url_unset():
    assert nats_client.is_enabled() is False


def test_is_enabled_true_when_url_set(monkeypatch):
    monkeypatch.setenv("NATS_URL", "nats://example:4222")
    assert nats_client.is_enabled() is True


def test_init_is_noop_when_disabled():
    """init() must be safe to call when NATS_URL is unset; no thread, no
    connection, no log spam."""
    nats_client.init()
    snap = nats_client.state()
    assert snap["enabled"] is False
    assert snap["state"] == "disabled"
    assert snap["url"] is None


def test_state_returns_stable_shape():
    """The admin panel template renders this dict directly; the keys
    must always be present so the template doesn't blow up on
    AttributeError."""
    snap = nats_client.state()
    for key in ("enabled", "state", "url", "last_error", "last_event_at"):
        assert key in snap


# ---------------------------------------------------------------------------
# publish -- no-op semantics
# ---------------------------------------------------------------------------
def test_publish_returns_false_when_disabled():
    """The hot path: a request handler calls publish() and gets False
    back, not an exception, when NATS isn't configured."""
    assert nats_client.publish("any.subject", b"{}") is False


def test_publish_returns_false_when_url_set_but_not_connected(monkeypatch):
    """NATS_URL is set but init() was never called (or failed). publish()
    must still no-op rather than crash the request."""
    monkeypatch.setenv("NATS_URL", "nats://unreachable:4222")
    # Deliberately don't call init() -- we want to assert that the
    # disconnected branch returns False without side effects.
    assert nats_client.publish("any.subject", b"{}") is False


# ---------------------------------------------------------------------------
# kv() -- accessor returns None when disconnected so the storage layer
# can fall back instead of raising.
# ---------------------------------------------------------------------------
def test_kv_returns_none_when_disabled():
    assert nats_client.kv("any-bucket") is None


# ---------------------------------------------------------------------------
# events.publish wrapper
# ---------------------------------------------------------------------------
def test_events_publish_returns_false_when_nats_disabled():
    """The contract callers rely on: events.publish never raises and
    quietly returns False when NATS isn't running."""
    assert events.publish("webhook.received", user_id=1, payload={"id": 99}) is False


def test_events_publish_does_not_raise_for_unserialisable_payload():
    """A programming error (non-JSON payload) must be logged, not
    raised, so a bug in one publisher can't take down a request."""
    class _NotJsonable:
        pass

    # Even with NATS disabled, the early envelope-build path should
    # still swallow the TypeError gracefully.
    assert events.publish(
        "webhook.received",
        user_id=1,
        payload={"obj": _NotJsonable()},
    ) is False


def test_events_publish_builds_correct_envelope():
    """Pin the envelope shape: future subscribers depend on this. We
    intercept at nats_client.publish to capture the bytes that would be
    sent without needing a real NATS server."""
    captured = {}

    def _fake_publish(subject, payload):
        captured["subject"] = subject
        captured["payload"] = payload
        return True

    with patch.object(nats_client, "publish", side_effect=_fake_publish):
        ok = events.publish(
            "webhook.received",
            user_id=42,
            payload={"webhook_id": 7, "source": "github"},
        )
    assert ok is True
    assert captured["subject"] == "app.webhook.received"

    envelope = json.loads(captured["payload"].decode("utf-8"))
    assert envelope["v"] == events.ENVELOPE_VERSION
    assert envelope["type"] == "webhook.received"
    assert envelope["user_id"] == 42
    assert envelope["payload"] == {"webhook_id": 7, "source": "github"}
    # id and timestamp must be present and non-empty
    assert envelope["id"] and isinstance(envelope["id"], str)
    assert envelope["timestamp"].endswith("Z")


def test_events_publish_handles_none_user_id_and_payload():
    """System-wide events (no per-user attribution) must publish cleanly
    with both fields omitted."""
    captured = {}

    def _fake_publish(subject, payload):
        captured["payload"] = payload
        return True

    with patch.object(nats_client, "publish", side_effect=_fake_publish):
        events.publish("webhook.received")

    envelope = json.loads(captured["payload"].decode("utf-8"))
    assert envelope["user_id"] is None
    assert envelope["payload"] == {}


# ---------------------------------------------------------------------------
# record_publish_success -- the bookkeeping the admin panel reads
# ---------------------------------------------------------------------------
def test_record_publish_success_updates_last_event_at():
    snap_before = nats_client.state()
    assert snap_before["last_event_at"] is None
    nats_client.record_publish_success()
    snap_after = nats_client.state()
    assert snap_after["last_event_at"] is not None
    assert snap_after["last_event_at"].endswith("Z")
