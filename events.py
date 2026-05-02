"""
Application-level event publishing.

Thin wrapper over :mod:`nats_client` that builds a standard JSON envelope
and routes it to a subject. The envelope shape is the public contract for
any future subscriber service, so don't change it without bumping
``ENVELOPE_VERSION`` and updating ``docs/nats.md``.

Subject naming: ``app.<entity>.<verb>``. Examples:

  * ``app.webhook.received``
  * ``app.notification.created``

Phase 0 has no subscribers -- :func:`publish` is fire-and-log. Failures
are never raised back to the request handler. This matches the contract
in ``nats_client.py``.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

import nats_client

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
SUBJECT_PREFIX = "app"


def _build_envelope(
    event_type: str,
    user_id: Optional[int],
    payload: Optional[dict],
) -> bytes:
    """Build the JSON envelope. Kept private so the shape only lives in
    one place; tests assert against this directly."""
    envelope = {
        "v": ENVELOPE_VERSION,
        "id": str(uuid.uuid4()),
        "type": event_type,
        "user_id": user_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "payload": payload or {},
    }
    return json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")


def _subject_for(event_type: str) -> str:
    """Map ``webhook.received`` -> ``app.webhook.received``."""
    return f"{SUBJECT_PREFIX}.{event_type}"


def publish(
    event_type: str,
    *,
    user_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> bool:
    """Publish an application event. Returns ``True`` on success.

    Never raises. ``event_type`` should be a dotted ``entity.verb`` string
    (e.g. ``"webhook.received"``). ``payload`` should contain only IDs /
    timestamps / event metadata -- no PII (see ``docs/nats.md``).
    """
    try:
        envelope = _build_envelope(event_type, user_id, payload)
    except (TypeError, ValueError):
        # A non-serialisable payload is a programming error, but we still
        # don't want it to take down the request. Log loudly and bail.
        logger.exception(
            "events.publish dropped event %s: payload not JSON-serialisable",
            event_type,
        )
        return False

    return nats_client.publish(_subject_for(event_type), envelope)
