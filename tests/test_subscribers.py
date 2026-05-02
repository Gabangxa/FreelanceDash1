"""
Unit tests for the NATS subscriber framework.

We test the handler logic against synthetic envelope dicts -- no live
NATS server required. The worker dispatch loop in ``worker.py`` is
covered by a smoke test (importable, callable, exits with the right
code when NATS_URL is unset).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from subscribers import REGISTRY
from subscribers.notifications import NotificationDeliverySubscriber


# ---------------------------------------------------------------------------
# Registry / contract
# ---------------------------------------------------------------------------
def test_registry_is_non_empty_and_well_formed():
    """Every entry must declare a subject and durable name -- the
    worker will refuse to bind otherwise."""
    assert len(REGISTRY) > 0
    for sub_cls in REGISTRY:
        sub = sub_cls()
        assert sub.subject, f"{sub_cls.__name__} missing subject"
        assert sub.durable_name, f"{sub_cls.__name__} missing durable_name"
        assert sub.subject.startswith("app."), (
            f"{sub_cls.__name__} subject {sub.subject!r} must start with "
            "'app.' so it's covered by the APP_EVENTS stream"
        )


def test_durable_names_are_unique():
    """Two subscribers with the same durable name would race on the
    same JetStream consumer cursor and silently steal each other's
    messages."""
    names = [s().durable_name for s in REGISTRY]
    assert len(names) == len(set(names)), f"duplicate durable names in REGISTRY: {names}"


# ---------------------------------------------------------------------------
# NotificationDeliverySubscriber -- flag-off path (default)
# ---------------------------------------------------------------------------
def test_notification_subscriber_acks_and_skips_when_flag_unset(monkeypatch):
    """Pre-cutover: web tier still delivers inline. The subscriber
    must ack-and-discard so messages don't pile up, but must NOT call
    deliver_notification."""
    monkeypatch.delenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", raising=False)

    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification"
    ) as deliver:
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"notification_id": 7}}
        )

    deliver.assert_not_called()


# ---------------------------------------------------------------------------
# NotificationDeliverySubscriber -- flag-on path (cutover-on)
# ---------------------------------------------------------------------------
def test_notification_subscriber_delivers_when_flag_on(monkeypatch):
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "true")

    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={"in_app": {"status": "success"}},
    ) as deliver:
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"notification_id": 42}}
        )

    deliver.assert_called_once_with(42)


def test_notification_subscriber_acks_on_permanent_missing_row(monkeypatch):
    """``deliver_notification`` returning ``{'error': 'Notification not found'}``
    (e.g. row deleted between publish and consume) must NOT raise --
    a missing row is permanent, redelivery won't help."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")

    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={"error": "Notification not found"},
    ):
        # Should return normally, not raise.
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"notification_id": 999}}
        )


def test_notification_subscriber_acks_on_permanent_missing_user(monkeypatch):
    """User-deleted is also permanent; redelivery won't help."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={"error": "User not found"},
    ):
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"notification_id": 999}}
        )


def test_notification_subscriber_naks_on_transient_smtp_failure(monkeypatch):
    """``deliver_notification`` catches SMTP exceptions and returns
    ``{'email': {'status': 'error', ...}}`` instead of raising. The
    subscriber MUST treat this as transient (raise → nak → retry),
    otherwise mail outages silently drop notifications."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={
            "email": {"status": "error", "error": "smtp wedged"},
            "in_app": {"status": "success"},
        },
    ):
        with pytest.raises(RuntimeError, match="email=error"):
            NotificationDeliverySubscriber().handle(
                {"id": "abc", "payload": {"notification_id": 5}}
            )


def test_notification_subscriber_naks_on_per_channel_failed(monkeypatch):
    """``status: 'failed'`` (sender returned False) is also transient."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={"email": {"status": "failed"}},
    ):
        with pytest.raises(RuntimeError, match="email=failed"):
            NotificationDeliverySubscriber().handle(
                {"id": "abc", "payload": {"notification_id": 5}}
            )


def test_notification_subscriber_naks_on_unknown_top_level_error(monkeypatch):
    """Unknown top-level ``error`` strings (not 'Notification/User not
    found') are treated as transient -- safer to retry than to silently
    swallow a new failure mode the service grows later."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        return_value={"error": "Database connection lost"},
    ):
        with pytest.raises(RuntimeError, match="unexpected top-level error"):
            NotificationDeliverySubscriber().handle(
                {"id": "abc", "payload": {"notification_id": 5}}
            )


def test_notification_subscriber_propagates_hard_error(monkeypatch):
    """A genuine exception from ``deliver_notification`` must propagate
    so the dispatcher can nak() and JetStream retries."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")

    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification",
        side_effect=ConnectionError("smtp wedged"),
    ):
        with pytest.raises(ConnectionError):
            NotificationDeliverySubscriber().handle(
                {"id": "abc", "payload": {"notification_id": 5}}
            )


# ---------------------------------------------------------------------------
# Malformed envelope -- ack and discard, never raise
# ---------------------------------------------------------------------------
def test_notification_subscriber_handles_missing_payload(monkeypatch):
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification"
    ) as deliver:
        NotificationDeliverySubscriber().handle({"id": "abc"})  # no payload key
    deliver.assert_not_called()


def test_notification_subscriber_handles_missing_notification_id(monkeypatch):
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification"
    ) as deliver:
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"foo": "bar"}}  # missing notification_id
        )
    deliver.assert_not_called()


def test_notification_subscriber_handles_non_int_notification_id(monkeypatch):
    """Type-confused payloads (string, None, dict) must not be coerced
    -- the publisher's schema says ``notification_id`` is an int."""
    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "1")
    with patch(
        "notifications.services.NotificationDeliveryService.deliver_notification"
    ) as deliver:
        NotificationDeliverySubscriber().handle(
            {"id": "abc", "payload": {"notification_id": "7"}}
        )
    deliver.assert_not_called()


# ---------------------------------------------------------------------------
# Worker entry point -- smoke test
# ---------------------------------------------------------------------------
def test_worker_module_imports_cleanly():
    """The worker must be importable without side effects (no NATS
    connection at import time, no asyncio loop spun up)."""
    import importlib

    # Force a fresh import to catch any module-level state leaks.
    if "worker" in list(__import__("sys").modules):
        del __import__("sys").modules["worker"]
    worker = importlib.import_module("worker")
    assert callable(worker.main)


def test_subscriber_owns_delivery_requires_jetstream_healthy(monkeypatch):
    """The cutover interlock: even with the env flag set, the web tier
    must NOT skip inline delivery when JetStream publish is
    unhealthy -- otherwise we'd publish into a broken bus and the
    worker would never see the message."""
    from webhooks.services import WebhookProcessor
    import nats_client

    monkeypatch.setenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "true")

    monkeypatch.setattr(nats_client, "_jetstream_publish_enabled", False)
    assert WebhookProcessor._subscriber_owns_delivery() is False, (
        "must inline-deliver when JS unhealthy, even with flag on"
    )

    monkeypatch.setattr(nats_client, "_jetstream_publish_enabled", True)
    assert WebhookProcessor._subscriber_owns_delivery() is True

    monkeypatch.delenv("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", raising=False)
    assert WebhookProcessor._subscriber_owns_delivery() is False, (
        "flag off must always inline-deliver regardless of JS health"
    )


def test_runtime_jetstream_failure_downgrades_publish_enabled(monkeypatch):
    """If js.publish() raises mid-runtime, _jetstream_publish_enabled
    must flip to False so subsequent _subscriber_owns_delivery() calls
    take the inline-fallback path. Without this, every subsequent
    notification would also be silently dropped."""
    import asyncio
    import nats_client

    fake_js = type("FakeJS", (), {})()

    async def _boom(subject, payload):
        raise RuntimeError("simulated JS publish failure")

    fake_js.publish = _boom

    monkeypatch.setattr(nats_client, "_nc", object())
    monkeypatch.setattr(nats_client, "_js", fake_js)
    monkeypatch.setattr(nats_client, "_jetstream_publish_enabled", True)

    with pytest.raises(RuntimeError, match="simulated JS publish failure"):
        asyncio.run(nats_client._publish_async("app.notification.created", b"{}"))

    assert nats_client._jetstream_publish_enabled is False, (
        "JS publish failure must downgrade the flag so subsequent calls "
        "skip JS and the cutover interlock falls back to inline delivery"
    )


def test_worker_exits_with_code_2_when_nats_url_unset(monkeypatch):
    """Refuse to start with NATS_URL unset -- there's nothing to
    consume from. Exit non-zero so the supervisor surfaces the
    misconfiguration instead of silently looping."""
    monkeypatch.delenv("NATS_URL", raising=False)

    import worker

    rc = worker.main()
    assert rc == 2
