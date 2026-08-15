"""
Tests for the durable email outbox (FD-020).

send_email now ENQUEUES to EmailDeliveryLog instead of spawning a daemon
thread (which gunicorn's worker recycle could kill mid-send, silently losing
password resets and magic links). A scheduled drainer sends queued rows.
These cover: enqueue-without-send, drain-and-mark-sent, transient-retry up to
the cap, idempotency (sent rows never re-sent), and stale-claim recovery from
a crashed drainer.
"""
from datetime import datetime, timedelta
import smtplib

import pytest

from app import db
import mail
from models import EmailDeliveryLog


@pytest.fixture(autouse=True)
def _clear_outbox(app):
    # send_email commits via an isolated session, so rows survive the
    # db_session rollback; clear them so they don't leak between tests.
    yield
    EmailDeliveryLog.query.delete()
    db.session.commit()


class _Recorder:
    """Stand-in for mail.mail.send: records calls, optionally raises."""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def __call__(self, msg):
        self.calls.append(msg)
        if self.exc is not None:
            raise self.exc


def test_send_email_enqueues_without_sending(app, db_session, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(mail.mail, "send", rec)

    assert mail.send_email("Reset", ["u@example.com"], "click here", html_body="<a>x</a>") is True

    assert rec.calls == []  # nothing sent synchronously
    row = EmailDeliveryLog.query.filter_by(recipient="u@example.com").one()
    assert row.status == "pending"
    assert row.subject == "Reset"
    assert row.text_body == "click here"
    assert row.html_body == "<a>x</a>"
    assert row.attempts == 0


def test_drain_sends_pending_and_marks_sent(app, db_session, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(mail.mail, "send", rec)
    mail.send_email("Hi", ["t@example.com"], "body")

    summary = mail.drain_email_outbox()

    assert summary["sent"] == 1
    assert len(rec.calls) == 1
    row = EmailDeliveryLog.query.filter_by(recipient="t@example.com").one()
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.attempts == 1


def test_transient_failure_retries_then_fails_at_cap(app, db_session, monkeypatch):
    rec = _Recorder(exc=smtplib.SMTPException("boom"))
    monkeypatch.setattr(mail.mail, "send", rec)
    mail.send_email("Hi", ["f@example.com"], "body")

    # 1st drain: transient failure -> re-queued as pending, attempts=1.
    s1 = mail.drain_email_outbox()
    assert s1["retried"] == 1
    row = EmailDeliveryLog.query.filter_by(recipient="f@example.com").one()
    assert row.status == "pending" and row.attempts == 1 and "boom" in (row.last_error or "")

    # 2nd drain: attempts=2, still re-drainable.
    mail.drain_email_outbox()
    row = EmailDeliveryLog.query.filter_by(recipient="f@example.com").one()
    assert row.status == "pending" and row.attempts == 2

    # 3rd drain: hits the cap -> terminal failed.
    s3 = mail.drain_email_outbox()
    row = EmailDeliveryLog.query.filter_by(recipient="f@example.com").one()
    assert row.status == "failed" and row.attempts == mail.MAX_SEND_ATTEMPTS
    assert s3["failed"] == 1

    # 4th drain: failed rows are not re-selected.
    s4 = mail.drain_email_outbox()
    assert s4 == {"sent": 0, "retried": 0, "failed": 0, "reclaimed": 0}
    assert len(rec.calls) == mail.MAX_SEND_ATTEMPTS  # exactly 3 send attempts, no more


def test_sent_rows_are_never_resent(app, db_session, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(mail.mail, "send", rec)
    mail.send_email("Hi", ["s@example.com"], "body")

    mail.drain_email_outbox()
    assert len(rec.calls) == 1
    # A second drain must be a no-op for an already-sent row.
    mail.drain_email_outbox()
    assert len(rec.calls) == 1


def test_stale_sending_row_is_reclaimed_and_sent(app, db_session, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(mail.mail, "send", rec)
    # A drainer crashed after claiming this row (status 'sending', old timestamp).
    db.session.add(EmailDeliveryLog(
        recipient="c@example.com", subject="S", text_body="b",
        status="sending", attempts=0,
        updated_at=datetime.utcnow() - timedelta(minutes=30),
    ))
    db.session.commit()

    summary = mail.drain_email_outbox()

    assert summary["reclaimed"] == 1
    assert summary["sent"] == 1
    row = EmailDeliveryLog.query.filter_by(recipient="c@example.com").one()
    assert row.status == "sent"


def test_fresh_sending_row_is_not_reclaimed(app, db_session, monkeypatch):
    # A row another drainer just claimed (recent 'sending') must be left alone.
    rec = _Recorder()
    monkeypatch.setattr(mail.mail, "send", rec)
    db.session.add(EmailDeliveryLog(
        recipient="live@example.com", subject="S", text_body="b",
        status="sending", attempts=0, updated_at=datetime.utcnow(),
    ))
    db.session.commit()

    summary = mail.drain_email_outbox()

    assert summary["reclaimed"] == 0
    assert rec.calls == []
    row = EmailDeliveryLog.query.filter_by(recipient="live@example.com").one()
    assert row.status == "sending"  # untouched
