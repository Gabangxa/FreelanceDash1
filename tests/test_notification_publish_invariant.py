"""
Regression test for the "every Notification row publishes
notification.created" invariant (Task #24).

The publish used to live in two manual call sites in
``webhooks/services.py``. It now lives in a SQLAlchemy after_commit
listener registered in ``models.py``, which means *any* code path that
writes a Notification row must trigger the bus event automatically --
including paths the original two call sites never knew about.

These tests intercept ``nats_client.publish`` (the bottom of the
publish stack, below ``events.publish``) so we don't need a live NATS
server and we can assert on the exact subject + envelope contents.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import nats_client
from app import db
from models import User, Notification


@pytest.fixture
def user(db_session):
    u = User(username="notif_invariant_user", email="notif_invariant@test.local")
    u.set_password("doesnotmatter")
    db.session.add(u)
    db.session.commit()
    yield u
    # Cleanup so successive tests don't trip the unique constraint.
    db.session.delete(u)
    db.session.commit()


def _capture_publishes():
    """Return (captured_list, patcher) where every nats_client.publish
    call appends ``(subject, envelope_dict)`` to the list."""
    captured = []

    def _fake(subject, payload):
        captured.append((subject, json.loads(payload.decode("utf-8"))))
        return True

    return captured, patch.object(nats_client, "publish", side_effect=_fake)


def test_direct_db_session_add_publishes_notification_created(db_session, user):
    """The whole point of centralising: a brand-new caller that knows
    nothing about webhooks/services.py still publishes the event just
    by adding a Notification through the ORM and committing.
    """
    captured, patcher = _capture_publishes()
    with patcher:
        n = Notification()
        n.user_id = user.id
        n.title = "Hello"
        n.message = "World"
        n.notification_type = "system"
        n.priority = "normal"
        db.session.add(n)
        db.session.commit()
        notif_id = n.id

    assert len(captured) == 1, (
        f"expected exactly one publish per committed Notification, got {len(captured)}: {captured}"
    )
    subject, envelope = captured[0]
    assert subject == "app.notification.created"
    assert envelope["type"] == "notification.created"
    assert envelope["user_id"] == user.id
    assert envelope["payload"]["notification_id"] == notif_id
    assert envelope["payload"]["notification_type"] == "system"
    assert envelope["payload"]["priority"] == "normal"

    # The per-row publish result is exposed on session.info so the
    # cutover-aware caller can inline-deliver on publish failure.
    assert db.session.info["_published_notif"][notif_id] is True


def test_rolled_back_notification_does_not_publish(db_session, user):
    """An after_insert that fires inside a transaction that ultimately
    rolls back must NOT result in a published event -- otherwise the
    bus would advertise a row that doesn't exist in the DB."""
    captured, patcher = _capture_publishes()
    with patcher:
        n = Notification()
        n.user_id = user.id
        n.title = "Doomed"
        n.message = "Will be rolled back"
        n.notification_type = "system"
        n.priority = "normal"
        db.session.add(n)
        db.session.flush()  # forces after_insert to fire
        db.session.rollback()
        # Now do an unrelated commit on the same session.
        db.session.commit()

    assert captured == [], (
        f"rolled-back Notification must not publish; got {captured}"
    )


def test_multiple_notifications_in_one_commit_each_publish(db_session, user):
    """A bulk path (e.g. system broadcast) that adds N rows in one
    transaction must produce N publish calls -- one per row."""
    captured, patcher = _capture_publishes()
    with patcher:
        ids = []
        for i in range(3):
            n = Notification()
            n.user_id = user.id
            n.title = f"Bulk {i}"
            n.message = "..."
            n.notification_type = "system"
            n.priority = "normal"
            db.session.add(n)
        db.session.commit()
        ids = [
            row.id
            for row in Notification.query.filter_by(user_id=user.id).all()
            if row.title.startswith("Bulk ")
        ]

    assert len(captured) == 3, f"expected 3 publishes, got {len(captured)}"
    published_ids = {env["payload"]["notification_id"] for _, env in captured}
    assert published_ids == set(ids)


def test_publish_failure_is_recorded_for_cutover_decision(db_session, user):
    """``events.publish`` returning False (e.g. JetStream unavailable)
    must surface as ``False`` on session.info so the webhook service's
    cutover logic falls back to inline delivery for THIS row."""
    def _fake_failed_publish(subject, payload):
        return False

    with patch.object(nats_client, "publish", side_effect=_fake_failed_publish):
        n = Notification()
        n.user_id = user.id
        n.title = "Pub-fail"
        n.message = "..."
        n.notification_type = "system"
        n.priority = "normal"
        db.session.add(n)
        db.session.commit()
        notif_id = n.id

    assert db.session.info["_published_notif"][notif_id] is False
