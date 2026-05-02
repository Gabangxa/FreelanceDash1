"""
Regression test for C1: WebhookEvent.metadata used to silently never
persist because ``metadata`` is reserved on SQLAlchemy DeclarativeBase.
After the rename to ``event_metadata`` the round-trip must work.
"""
import json
from datetime import datetime

from app import db
from models import WebhookEvent


def test_event_metadata_round_trips_through_db(db_session):
    payload_meta = {
        "client_ip": "203.0.113.5",
        "payload_size": 1234,
        "validation_time": 0.012,
        "security_version": "2.0",
    }

    event = WebhookEvent(
        source="github",
        event_type="push",
        payload='{"ref":"refs/heads/main"}',
        event_metadata=json.dumps(payload_meta),
        created_at=datetime.utcnow(),
    )
    db.session.add(event)
    db.session.commit()

    event_id = event.id
    db.session.expire_all()

    fetched = db.session.get(WebhookEvent, event_id)
    assert fetched is not None
    assert fetched.event_metadata is not None, (
        "event_metadata was None after a round-trip -- the column may not "
        "exist or the rename from `metadata` was not applied."
    )
    assert json.loads(fetched.event_metadata) == payload_meta


def test_webhook_event_does_not_have_legacy_metadata_column():
    """The legacy `metadata` attribute on the instance must not be a Column.

    If somebody re-introduces a `metadata` column, SQLAlchemy will raise an
    InvalidRequestError at class-definition time. This test guards against a
    silent regression where someone re-adds it as an instance attribute.
    """
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(WebhookEvent)
    column_names = {c.key for c in mapper.columns}
    assert "event_metadata" in column_names
    assert "metadata" not in column_names
