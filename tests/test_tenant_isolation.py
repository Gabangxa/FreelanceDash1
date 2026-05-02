"""
Cross-tenant IDOR regression tests.

These assert that user A asking for one of user B's row IDs gets a 404
(or a no-content response in the case of the JSON helpers) -- never the
data. They are deliberately mechanical: each protected route is poked
with a foreign-tenant id and the response is checked.

Belt-and-suspenders fixes in clients/routes.py, invoices/routes.py and
projects/routes.py (May 2026 audit) added defensive ``user_id`` filters
to queries that previously trusted a single parent-row check. These
tests guard against regression.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app import db
from models import (
    Client,
    Invoice,
    InvoiceItem,
    Project,
    Task,
    TimeEntry,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Tenant:
    """Simple value-object holding the row ids belonging to one tenant."""

    def __init__(self, user, client, project, task, invoice, time_entry):
        self.user = user
        self.client = client
        self.project = project
        self.task = task
        self.invoice = invoice
        self.time_entry = time_entry


def _make_tenant(username, email, password="testpassword123"):
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
        status='active',
    )
    db.session.add(project)
    db.session.flush()

    task = Task(
        title=f"{username} task",
        project_id=project.id,
        status='todo',
    )
    db.session.add(task)
    db.session.flush()

    now = datetime.utcnow()
    time_entry = TimeEntry(
        project_id=project.id,
        task_id=task.id,
        start_time=now,
        end_time=now + timedelta(minutes=30),
        duration=30,
        description=f"{username} time",
        billable=True,
    )
    db.session.add(time_entry)

    # Use uuid in the invoice number so per-test fixtures don't collide
    # on the UNIQUE(invoice_number) constraint.
    import uuid as _uuid
    invoice = Invoice(
        invoice_number=f"INV-{_uuid.uuid4().hex[:8].upper()}",
        amount=Decimal('100.00'),
        currency='USD',
        status='draft',
        due_date=now + timedelta(days=14),
        client_id=client.id,
        project_id=project.id,
    )
    db.session.add(invoice)
    db.session.flush()

    db.session.add(InvoiceItem(
        description='consulting',
        quantity=Decimal('1.0000'),
        rate=Decimal('100.00'),
        amount=Decimal('100.00'),
        invoice_id=invoice.id,
    ))
    db.session.commit()

    return _Tenant(user, client, project, task, invoice, time_entry)


@pytest.fixture()
def two_tenants(app, db_session):
    """Create two unrelated tenants (alice + bob) and return both.

    Each test gets fresh users with uuid-suffixed identifiers so the
    in-memory SQLite (which is session-scoped via the ``app`` fixture)
    doesn't trip on the UNIQUE constraints from a previous test's rows.
    """
    import uuid
    suffix = uuid.uuid4().hex[:8]
    # The login form's Email() validator rejects ``.local`` TLDs, so use
    # ``.com`` for these fixtures even though everything is in-memory.
    alice = _make_tenant(f'alice_{suffix}', f'alice_{suffix}@example.com')
    bob = _make_tenant(f'bob_{suffix}', f'bob_{suffix}@example.com')
    yield alice, bob


def _login(client, email, password="testpassword123"):
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302), f"Login failed: {resp.status_code}"


# ---------------------------------------------------------------------------
# Cross-tenant access tests
# ---------------------------------------------------------------------------
# Each test logs in as Alice and asks for one of Bob's resources. The
# expected response is 404 (we use ``first_or_404`` everywhere), or a
# 403 / empty list for the JSON dropdown helpers.

def test_view_other_tenant_client_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/clients/{bob.client.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_edit_other_tenant_client_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/clients/{bob.client.id}/edit", follow_redirects=False)
    assert resp.status_code == 404


def test_delete_other_tenant_client_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.post(f"/clients/{bob.client.id}/delete", follow_redirects=False)
    assert resp.status_code == 404


def test_view_other_tenant_project_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/projects/{bob.project.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_view_other_tenant_task_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/tasks/{bob.task.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_edit_other_tenant_task_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/tasks/{bob.task.id}/edit", follow_redirects=False)
    assert resp.status_code == 404


def test_delete_other_tenant_task_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.post(f"/tasks/{bob.task.id}/delete", follow_redirects=False)
    assert resp.status_code == 404


def test_view_other_tenant_invoice_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/invoices/{bob.invoice.id}", follow_redirects=False)
    assert resp.status_code == 404


def test_pdf_other_tenant_invoice_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/invoices/{bob.invoice.id}/pdf", follow_redirects=False)
    assert resp.status_code == 404


def test_delete_other_tenant_invoice_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.post(f"/invoices/{bob.invoice.id}/delete", follow_redirects=False)
    assert resp.status_code == 404


def test_get_projects_for_other_tenants_client_refused(client, two_tenants):
    """The JSON dropdown helper must refuse cross-tenant client ids."""
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(f"/invoices/get-projects/{bob.client.id}", follow_redirects=False)
    # The route returns 403 with [] when the client is not Alice's. The
    # critical assertion is "no leaked project data" -- shape varies a
    # little, so check both status and that bob's project name isn't in
    # the body.
    assert resp.status_code in (403, 404)
    assert bob.project.name.encode() not in resp.data


def test_get_project_tasks_for_other_tenants_project_returns_404(client, two_tenants):
    alice, bob = two_tenants
    _login(client, alice.user.email)
    resp = client.get(
        f"/projects/{bob.project.id}/tasks",
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert bob.task.title.encode() not in resp.data
