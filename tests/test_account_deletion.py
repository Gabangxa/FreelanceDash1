"""
Regression tests for account deletion (settings/delete-account).

The original implementation issued ``Query.delete()`` after ``Query.join()``,
which SQLAlchemy rejects with ``InvalidRequestError`` -- so every deletion
attempt failed and was swallowed by the generic error handler. It also bulk-
deleted the User row, bypassing the ORM cascades and leaving Subscription /
SubscriptionLog rows (which have no cascade) dangling.

These tests build a fully-populated user -- one of every child row that
references the account, including both cascade-backed and non-cascade
relationships -- delete the account through the real route, and assert that
nothing owned by the user survives and nothing belonging to a *second* user
is touched.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import uuid

import pytest

from app import db
from models import (
    Client,
    Invoice,
    InvoiceItem,
    Notification,
    NotificationSettings,
    Project,
    Task,
    TimeEntry,
    User,
    UserSettings,
)
from polar.models import Subscription, SubscriptionLog


@pytest.fixture()
def make_populated_user(app):
    """Factory that builds fully-populated users and cleans them up.

    The ``app`` fixture's in-memory SQLite is session-scoped, and these
    fixtures ``commit`` (so ``db_session``'s rollback can't reclaim them).
    Any user still present at teardown is deleted here so committed rows
    don't leak into later, order-dependent tests (e.g. the admin-hours
    test sums TimeEntry.duration across the whole database).
    """
    created_ids = []

    def _factory(username, email, password="testpassword123"):
        user = _make_populated_user(username, email, password)
        created_ids.append(user.id)
        return user

    yield _factory

    for uid in created_ids:
        # Subscription/SubscriptionLog have no cascade, so clear them first
        # (mirroring the route under test) before the cascading User delete.
        SubscriptionLog.query.filter_by(user_id=uid).delete(synchronize_session=False)
        Subscription.query.filter_by(user_id=uid).delete(synchronize_session=False)
        user = db.session.get(User, uid)
        if user is not None:
            db.session.delete(user)
    db.session.commit()


def _make_populated_user(username, email, password="testpassword123"):
    """Create a user with one row of every account-owned model."""
    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    client = Client(name=f"{username} client", user_id=user.id)
    db.session.add(client)
    db.session.flush()

    project = Project(
        name=f"{username} project",
        start_date=datetime.utcnow(),
        user_id=user.id,
        client_id=client.id,
        status="active",
    )
    db.session.add(project)
    db.session.flush()

    task = Task(title=f"{username} task", project_id=project.id, status="todo")
    db.session.add(task)
    db.session.flush()

    now = datetime.utcnow()
    db.session.add(TimeEntry(
        project_id=project.id,
        task_id=task.id,
        start_time=now,
        end_time=now + timedelta(minutes=30),
        duration=30,
        description=f"{username} time",
        billable=True,
    ))

    invoice = Invoice(
        invoice_number=f"INV-{uuid.uuid4().hex[:8].upper()}",
        amount=Decimal("100.00"),
        currency="USD",
        status="draft",
        due_date=now + timedelta(days=14),
        client_id=client.id,
        project_id=project.id,
    )
    db.session.add(invoice)
    db.session.flush()

    db.session.add(InvoiceItem(
        description="consulting",
        quantity=Decimal("1.0000"),
        rate=Decimal("100.00"),
        amount=Decimal("100.00"),
        invoice_id=invoice.id,
    ))

    db.session.add(UserSettings(user_id=user.id))
    db.session.add(NotificationSettings(user_id=user.id))
    db.session.add(Notification(
        user_id=user.id,
        title="hello",
        message="body",
        notification_type="system",
    ))

    # Subscription + SubscriptionLog have NO delete cascade -- the exact rows
    # that would FK-violate if the route relied on the User cascade alone.
    subscription = Subscription(
        user_id=user.id,
        polar_subscription_id=f"sub_{uuid.uuid4().hex[:10]}",
        tier_id="professional",
        tier_name="professional",
        amount=Decimal("9.00"),
    )
    db.session.add(subscription)
    db.session.flush()
    db.session.add(SubscriptionLog(
        user_id=user.id,
        subscription_id=subscription.id,
        event_type="created",
    ))

    db.session.commit()
    return user


def _login(client, email, password="testpassword123"):
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302), f"Login failed: {resp.status_code}"


def _owned_row_counts(user_id):
    """Count every row that references this user, directly or transitively."""
    project_ids = [p.id for p in Project.query.filter_by(user_id=user_id).all()]
    invoice_ids = [
        inv.id for inv in Invoice.query.filter(Invoice.project_id.in_(project_ids or [-1])).all()
    ]
    return {
        "user": User.query.filter_by(id=user_id).count(),
        "clients": Client.query.filter_by(user_id=user_id).count(),
        "projects": Project.query.filter_by(user_id=user_id).count(),
        "tasks": Task.query.filter(Task.project_id.in_(project_ids or [-1])).count(),
        "time_entries": TimeEntry.query.filter(TimeEntry.project_id.in_(project_ids or [-1])).count(),
        "invoices": Invoice.query.filter(Invoice.project_id.in_(project_ids or [-1])).count(),
        "invoice_items": InvoiceItem.query.filter(InvoiceItem.invoice_id.in_(invoice_ids or [-1])).count(),
        "user_settings": UserSettings.query.filter_by(user_id=user_id).count(),
        "notifications": Notification.query.filter_by(user_id=user_id).count(),
        "notification_settings": NotificationSettings.query.filter_by(user_id=user_id).count(),
        "subscriptions": Subscription.query.filter_by(user_id=user_id).count(),
        "subscription_logs": SubscriptionLog.query.filter_by(user_id=user_id).count(),
    }


def test_delete_account_removes_all_owned_rows(client, app, db_session, make_populated_user):
    suffix = uuid.uuid4().hex[:8]
    user = make_populated_user(f"victim_{suffix}", f"victim_{suffix}@example.com")
    user_id = user.id
    user_email = user.email

    # Sanity: the account genuinely owns a row of every kind before deletion.
    before = _owned_row_counts(user_id)
    assert all(v >= 1 for v in before.values()), f"fixture incomplete: {before}"

    _login(client, user_email)
    resp = client.post(
        "/settings/delete-account",
        data={"confirmation": user_email, "password": "testpassword123", "understand": "y"},
        follow_redirects=False,
    )
    # Success path redirects to the landing page; a validation failure would
    # re-render the form with 200, and the old bug left a 200 too.
    assert resp.status_code == 302, resp.data[:400]

    after = _owned_row_counts(user_id)
    assert after == {k: 0 for k in after}, f"rows survived deletion: {after}"


def test_delete_account_wrong_password_keeps_data(client, app, db_session, make_populated_user):
    suffix = uuid.uuid4().hex[:8]
    user = make_populated_user(f"keep_{suffix}", f"keep_{suffix}@example.com")
    user_id = user.id

    _login(client, user.email)
    resp = client.post(
        "/settings/delete-account",
        data={"confirmation": user.email, "password": "wrong-password", "understand": "y"},
        follow_redirects=False,
    )
    # Form validation fails -> the page re-renders, nothing is deleted.
    assert resp.status_code == 200
    assert User.query.filter_by(id=user_id).count() == 1


def test_delete_account_succeeds_with_subscription_relationship_loaded(
    client, app, db_session, make_populated_user
):
    """Guards the StaleDataError foot-gun.

    Subscription has no delete cascade, so if the ``User.subscription``
    backref is loaded into the session before deletion, a bulk
    ``Query.delete()`` of the subscription followed by ``delete(user)``
    would try to NULL the already-gone row and raise StaleDataError
    (silently rolled back). Deleting the subscription through the session
    keeps the identity map consistent. This test forces the relationship
    to load, then deletes.
    """
    suffix = uuid.uuid4().hex[:8]
    user = make_populated_user(f"loaded_{suffix}", f"loaded_{suffix}@example.com")
    user_id = user.id
    user_email = user.email

    # Force the backref into the session's identity map (the trigger condition).
    loaded = db.session.get(User, user_id)
    assert loaded.subscription is not None

    _login(client, user_email)
    resp = client.post(
        "/settings/delete-account",
        data={"confirmation": user_email, "password": "testpassword123", "understand": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.data[:400]

    after = _owned_row_counts(user_id)
    assert after == {k: 0 for k in after}, f"rows survived deletion: {after}"


def test_delete_account_does_not_touch_other_users(client, app, db_session, make_populated_user):
    suffix = uuid.uuid4().hex[:8]
    victim = make_populated_user(f"gone_{suffix}", f"gone_{suffix}@example.com")
    bystander = make_populated_user(f"stay_{suffix}", f"stay_{suffix}@example.com")
    bystander_id = bystander.id

    _login(client, victim.email)
    resp = client.post(
        "/settings/delete-account",
        data={"confirmation": victim.email, "password": "testpassword123", "understand": "y"},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    survived = _owned_row_counts(bystander_id)
    assert all(v >= 1 for v in survived.values()), f"bystander data lost: {survived}"
