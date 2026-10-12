"""
Duration-only time entries must be invoiceable (FD-005).

The project page's timer and manual forms post a duration but no end_time.
The invoice picker required ``end_time IS NOT NULL`` (meant to hide running
timers, but the timer runs in the browser and only posts on stop), so those
entries were never offered for invoicing. Billing only ever uses ``duration``.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import re
from types import SimpleNamespace
import uuid

from flask import g
import pytest

from app import db
from models import Client, Invoice, InvoiceItem, Project, TimeEntry, User

PASSWORD = "testpassword123"
RATE = Decimal("100.00")


def _make_user(label):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{label}_{suffix}", email=f"{label}_{suffix}@example.com")
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()
    acme = Client(name="Acme", user_id=user.id)
    db.session.add(acme)
    db.session.flush()
    project = Project(name="Site", status="active", start_date=datetime(2026, 1, 1),
                      user_id=user.id, client_id=acme.id)
    db.session.add(project)
    db.session.commit()
    return user, acme, project


@pytest.fixture()
def ctx(app, client):
    user, acme, project = _make_user("inv")
    other_user, other_client, other_project = _make_user("other")
    # The session-scoped app context shares ``g`` across requests, and
    # Flask-Login caches the resolved user on it; drop any user cached by an
    # earlier test so this test's login is the one that takes effect.
    g.pop("_login_user", None)
    try:
        resp = client.post("/auth/login", data={"email": user.email, "password": PASSWORD})
        assert resp.status_code == 302 and "/auth/login" not in resp.headers["Location"]
        yield SimpleNamespace(
            user=user, client_row=acme, project=project,
            other_client=other_client, other_project=other_project,
        )
    finally:
        # Session-scoped SQLite: committed rows would leak into later tests.
        # User deletion cascades to clients, projects, entries and invoices.
        g.pop("_login_user", None)
        db.session.rollback()
        for u in (user, other_user):
            db.session.delete(db.session.get(User, u.id))
        db.session.commit()


def _post_entry(client, project, **fields):
    data = {"project_id": project.id, **fields}
    resp = client.post("/time-entries/new", data=data)
    assert resp.status_code == 302
    return TimeEntry.query.filter_by(project_id=project.id).order_by(TimeEntry.id.desc()).first()


def _listed_ids(client, client_row, project):
    resp = client.get(
        f"/invoices/from-time-entries?client_id={client_row.id}&project_id={project.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    return {int(i) for i in re.findall(r'name="entry_ids"\s+value="(\d+)"', html)}


def _direct_entry(project, **fields):
    entry = TimeEntry(project_id=project.id, start_time=datetime(2026, 3, 2, 9),
                      **fields)
    db.session.add(entry)
    db.session.commit()
    return entry


def test_manual_form_entry_gets_end_time_and_is_invoiceable(client, ctx):
    entry = _post_entry(client, ctx.project, start_time="2026-03-02",
                        duration="90", billable="on")
    assert entry.start_time == datetime(2026, 3, 2)
    assert entry.end_time == datetime(2026, 3, 2, 1, 30)
    assert entry.duration == 90

    assert entry.id in _listed_ids(client, ctx.client_row, ctx.project)

    resp = client.post("/invoices/from-time-entries", data={
        "client_id": ctx.client_row.id,
        "project_id": ctx.project.id,
        "entry_ids": [entry.id],
        "rate": str(RATE),
        "currency": "USD",
        "status": "draft",
        "due_date": (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d"),
    })
    assert resp.status_code == 302

    invoice = Invoice.query.filter_by(client_id=ctx.client_row.id).one()
    item = InvoiceItem.query.filter_by(invoice_id=invoice.id).one()
    assert item.quantity == Decimal("1.5")
    assert item.amount == Decimal("1.5") * RATE
    db.session.refresh(entry)
    assert entry.invoiced_at is not None
    assert entry.invoice_id == invoice.id


def test_timer_form_entry_is_treated_as_just_finished(client, ctx):
    before = datetime.utcnow()
    entry = _post_entry(client, ctx.project, duration="90", billable="on")
    after = datetime.utcnow()

    assert before - timedelta(seconds=5) <= entry.end_time <= after + timedelta(seconds=5)
    assert entry.end_time <= after  # never in the future
    assert entry.start_time == entry.end_time - timedelta(minutes=90)
    assert entry.id in _listed_ids(client, ctx.client_row, ctx.project)


def test_legacy_entry_without_end_time_is_listed(client, ctx):
    entry = _direct_entry(ctx.project, end_time=None, duration=90, billable=True)
    assert entry.id in _listed_ids(client, ctx.client_row, ctx.project)


def test_legacy_entry_without_end_time_can_be_invoiced(client, ctx):
    entry = _direct_entry(ctx.project, end_time=None, duration=90, billable=True)
    resp = client.post("/invoices/from-time-entries", data={
        "client_id": ctx.client_row.id,
        "project_id": ctx.project.id,
        "entry_ids": [entry.id],
        "rate": str(RATE),
        "currency": "USD",
        "status": "draft",
        "due_date": (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d"),
    })
    assert resp.status_code == 302
    db.session.refresh(entry)
    assert entry.invoiced_at is not None


# Each exclusion test also lists one valid entry, so "not listed" can't pass
# just because the picker showed nothing (wrong user, broken query).

@pytest.mark.parametrize("duration", [0, None])
def test_billable_entry_without_duration_is_not_listed(client, ctx, duration):
    valid = _direct_entry(ctx.project, end_time=None, duration=30, billable=True)
    entry = _direct_entry(ctx.project, end_time=datetime(2026, 3, 2, 10),
                          duration=duration, billable=True)
    listed = _listed_ids(client, ctx.client_row, ctx.project)
    assert valid.id in listed
    assert entry.id not in listed


def test_zero_duration_entry_posted_directly_is_not_invoiced(client, ctx):
    # A tampered POST naming an entry the picker never offered.
    entry = _direct_entry(ctx.project, end_time=datetime(2026, 3, 2, 10),
                          duration=0, billable=True)
    client.post("/invoices/from-time-entries", data={
        "client_id": ctx.client_row.id,
        "project_id": ctx.project.id,
        "entry_ids": [entry.id],
        "rate": str(RATE),
        "currency": "USD",
        "status": "draft",
        "due_date": (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d"),
    })
    assert Invoice.query.filter_by(client_id=ctx.client_row.id).count() == 0
    db.session.refresh(entry)
    assert entry.invoiced_at is None


def test_non_billable_and_invoiced_entries_stay_excluded(client, ctx):
    valid = _direct_entry(ctx.project, end_time=None, duration=30, billable=True)
    non_billable = _direct_entry(ctx.project, end_time=None, duration=60, billable=False)
    invoiced = _direct_entry(ctx.project, end_time=None, duration=60, billable=True,
                             invoiced_at=datetime(2026, 3, 3))
    listed = _listed_ids(client, ctx.client_row, ctx.project)
    assert valid.id in listed
    assert non_billable.id not in listed
    assert invoiced.id not in listed


def test_other_users_duration_only_entry_is_not_listed(client, ctx):
    own = _direct_entry(ctx.project, end_time=None, duration=30, billable=True)
    foreign = _direct_entry(ctx.other_project, end_time=None, duration=90, billable=True)
    # Probe both the foreign project and our own: neither may leak it.
    assert foreign.id not in _listed_ids(client, ctx.other_client, ctx.other_project)
    listed = _listed_ids(client, ctx.client_row, ctx.project)
    assert own.id in listed
    assert foreign.id not in listed


def test_absurd_duration_is_rejected_not_a_server_error(client, ctx):
    resp = client.post("/time-entries/new", data={
        "project_id": ctx.project.id, "duration": str(10**12), "billable": "on"})
    assert resp.status_code == 302
    assert TimeEntry.query.filter_by(project_id=ctx.project.id).count() == 0


def test_edit_without_end_time_derives_it_from_duration(client, ctx):
    # A legacy row (no end_time) moved to a new start, so the expected end
    # can only come from start + duration, not from a stored value.
    entry = _direct_entry(ctx.project, end_time=None, duration=90, billable=True)
    resp = client.post(f"/time-entries/{entry.id}/edit", data={
        "project_id": ctx.project.id,
        "task_id": 0,
        "start_time": "2026-03-05 14:00",
        "end_time": "",
        "description": "edited",
        "billable": "y",
    })
    assert resp.status_code == 302
    db.session.refresh(entry)
    assert entry.duration == 90
    assert entry.end_time == datetime(2026, 3, 5, 15, 30)


def test_edit_that_would_overflow_is_rejected_not_a_server_error(client, ctx):
    entry = _direct_entry(ctx.project, end_time=None, duration=120, billable=True)
    resp = client.post(f"/time-entries/{entry.id}/edit", data={
        "project_id": ctx.project.id,
        "task_id": 0,
        "start_time": "9999-12-31 23:00",
        "end_time": "",
        "description": "edited",
        "billable": "y",
    })
    assert resp.status_code == 200
    db.session.refresh(entry)
    assert entry.start_time == datetime(2026, 3, 2, 9)
    assert entry.end_time is None
