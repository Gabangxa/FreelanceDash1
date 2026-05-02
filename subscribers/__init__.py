"""
NATS JetStream subscribers — the consumer side of the bus.

Phase 1. Each subscriber declares a subject, a JetStream durable name,
and a synchronous ``handle(envelope)`` method. The runner in
``worker.py`` connects once, registers all entries in :data:`REGISTRY`
as JetStream pull/push consumers, and dispatches incoming messages
through a thread executor so the asyncio loop never blocks on
SQLAlchemy.

Why subscribers run in their own process
----------------------------------------
The web tier runs on Replit Autoscale, which scales gunicorn workers
down to zero between requests. A NATS subscriber needs a long-lived TCP
connection to the broker -- if the worker dies, messages queue up on
JetStream until someone restarts it, which only happens when web
traffic arrives. That defeats the purpose of moving work off the
request path. So subscribers live on a Reserved VM (always-on, flat
monthly rate) instead.

See ``docs/nats.md`` for the deployment runbook.
"""
from __future__ import annotations

from subscribers.base import Subscriber  # noqa: F401 - re-export
from subscribers.notifications import NotificationDeliverySubscriber

# Single source of truth for "what subscribers does this app run?".
# The worker iterates this list at startup. Adding a new subscriber is
# as simple as appending here -- no other plumbing required.
REGISTRY: list[type[Subscriber]] = [
    NotificationDeliverySubscriber,
]
