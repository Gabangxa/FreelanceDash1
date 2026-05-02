"""
Base class for NATS JetStream subscribers.

Subscribers are ordinary classes with a synchronous ``handle()``
method. The async dispatch loop in ``worker.py`` wraps each invocation
in:

  1. JSON-decode the envelope (malformed → ``term()`` immediately, no
     redelivery — bad data won't get better with retries).
  2. ``loop.run_in_executor(None, ...)`` so blocking SQLAlchemy / SMTP
     calls don't stall the event loop.
  3. ``with flask_app.app_context()`` so the handler can use
     ``db.session`` exactly like a request handler.
  4. On success → ``msg.ack()``. On exception → ``msg.nak()`` so
     JetStream redelivers (up to ``max_deliver``).

The handler MUST be idempotent. JetStream guarantees at-least-once
delivery; transient failures and worker restarts will cause the same
envelope to land twice. See ``NotificationDeliverySubscriber`` for the
canonical idempotency pattern (check-then-act against the DB).
"""
from __future__ import annotations

import abc
from typing import ClassVar, Optional


class Subscriber(abc.ABC):
    """Concrete subscribers set the class attributes and implement
    :meth:`handle`. Instances are constructed once at worker startup
    and reused for every message on the subject."""

    #: NATS subject (or wildcard) this subscriber consumes. Must match
    #: a subject that's covered by the ``APP_EVENTS`` stream
    #: (``app.>``) or the consumer creation will fail.
    subject: ClassVar[str] = ""

    #: JetStream durable consumer name. Stable across restarts so the
    #: subscriber resumes where it left off rather than re-reading
    #: every message in the stream.
    durable_name: ClassVar[str] = ""

    #: Optional queue group for horizontal scaling. When two workers
    #: with the same ``queue_group`` consume the same subject,
    #: JetStream load-balances messages between them. Leave ``None``
    #: until you actually need a second worker.
    queue_group: ClassVar[Optional[str]] = None

    #: Max delivery attempts before JetStream gives up and the message
    #: lands on the per-stream "max deliver" dead-letter handling.
    #: Five is conservative; transient errors (DB blip, SMTP timeout)
    #: get retries, but a permanently-broken message can't loop
    #: forever and starve the consumer.
    max_deliver: ClassVar[int] = 5

    #: How long the subscriber gets to ack before JetStream considers
    #: the message un-acked and redelivers. Should be larger than the
    #: P99 wall-clock time of :meth:`handle`.
    ack_wait_seconds: ClassVar[int] = 60

    @abc.abstractmethod
    def handle(self, envelope: dict) -> None:
        """Process one decoded envelope. Raise on failure to trigger
        a JetStream redelivery; return normally to ack."""
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<{type(self).__name__} subject={self.subject!r} "
            f"durable={self.durable_name!r}>"
        )
