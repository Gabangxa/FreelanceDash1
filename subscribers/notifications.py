"""
Subscriber that delivers notifications via the bus instead of inline
in the request handler.

Cutover model
-------------
This subscriber is **always running** on the Reserved VM whenever the
worker process is up. Whether it actually delivers the notification or
no-ops is controlled by the ``NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS``
environment variable, which is also read by ``webhooks/services.py``
to decide whether to call ``deliver_notification`` inline.

This means there is exactly one delivery path at any time:

  * Flag unset (default)  → web tier delivers inline, subscriber acks
                            and discards.
  * Flag set              → web tier skips inline delivery, subscriber
                            owns it.

The flag must be set in **both** processes simultaneously (web + worker).
The recommended cutover is:

  1. Deploy both with the flag unset, run for 24h.
  2. Watch the worker logs to confirm it's consuming messages and the
     subscriber is healthy ("delivery skipped, flag off" lines).
  3. Set ``NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS=true`` and restart
     both. From this point only the worker delivers.
  4. To roll back: unset the flag and restart both. Inline delivery
     resumes; any messages that landed during the rollback window are
     re-acked-and-discarded by the subscriber.

Idempotency
-----------
JetStream is at-least-once. If the worker crashes between the email
send and the ack, the same envelope will redeliver. We rely on
``NotificationDeliveryService.deliver_notification`` and the underlying
email queue to handle re-sends gracefully (the email queue dedupes by
``notification_id`` + recipient + timestamp window). For in-app
delivery there's nothing to dedupe — re-running it just touches the
same row.
"""
from __future__ import annotations

import logging
import os

from subscribers.base import Subscriber

logger = logging.getLogger(__name__)


def _flag_enabled() -> bool:
    """Read the cutover flag at message-handling time (not import time).
    This lets an operator flip the flag and restart only the worker
    without code changes; the next message read picks up the new value.
    """
    return os.environ.get("NATS_SUBSCRIBER_DELIVERS_NOTIFICATIONS", "").lower() in (
        "1", "true", "yes",
    )


class NotificationDeliverySubscriber(Subscriber):
    """Consume ``app.notification.created`` events and deliver the
    notification (email + in-app) when the cutover flag is set."""

    subject = "app.notification.created"
    durable_name = "notification-delivery"
    max_deliver = 5
    ack_wait_seconds = 60

    def handle(self, envelope: dict) -> None:
        payload = envelope.get("payload") or {}
        notification_id = payload.get("notification_id")
        if not isinstance(notification_id, int):
            # Malformed payload — log and ack (returning normally).
            # This is intentional: a missing id is a publisher bug, not
            # a transient failure, so retrying won't help and we don't
            # want it to clog the consumer.
            logger.error(
                "notification.delivery: envelope missing payload.notification_id; "
                "discarding (envelope id=%s)",
                envelope.get("id"),
            )
            return

        if not _flag_enabled():
            # Pre-cutover mode: web tier still delivers inline, we
            # just ack-and-discard so the messages don't pile up.
            logger.debug(
                "notification.delivery: flag off, ack-and-skipping notification_id=%s",
                notification_id,
            )
            return

        # Cutover-on path: actually deliver. Re-raises on failure so
        # the dispatcher can nak() and JetStream will retry.
        from notifications.services import NotificationDeliveryService

        result = NotificationDeliveryService.deliver_notification(notification_id)
        if not isinstance(result, dict):
            # Defensive: deliver_notification's contract is "always
            # returns a dict". If that ever changes, treat as
            # transient and retry.
            raise RuntimeError(
                f"deliver_notification returned non-dict {type(result).__name__}; "
                "treating as transient"
            )

        # Top-level "error" key = permanent failure (notification row
        # gone, user gone). Retrying won't help; ack and move on.
        # These are the only two strings the service produces today
        # (see notifications/services.py); any other top-level error
        # is unexpected and we treat as transient.
        top_error = result.get("error")
        if top_error in ("Notification not found", "User not found"):
            logger.warning(
                "notification.delivery: permanent failure for notification_id=%s: %s",
                notification_id, top_error,
            )
            return
        if top_error:
            raise RuntimeError(
                f"deliver_notification unexpected top-level error: {top_error!r}"
            )

        # Per-channel status check. The service catches SMTP/DB blips
        # and turns them into ``{'status': 'error' | 'failed'}`` dicts
        # rather than raising. From the bus subscriber's point of view
        # these ARE retryable -- the next redelivery may succeed when
        # the SMTP server is back. Without this check, transient mail
        # outages would be silently acked.
        transient_channels = []
        for channel, status_info in result.items():
            if not isinstance(status_info, dict):
                continue
            if status_info.get("status") in ("error", "failed"):
                transient_channels.append(
                    f"{channel}={status_info.get('status')}"
                    f"({status_info.get('error', 'no detail')})"
                )
        if transient_channels:
            raise RuntimeError(
                f"notification_id={notification_id} channel failures: "
                + "; ".join(transient_channels)
            )

        logger.info(
            "notification.delivery: delivered notification_id=%s via %s",
            notification_id, ", ".join(result.keys()),
        )
