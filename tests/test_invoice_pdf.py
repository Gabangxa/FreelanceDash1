"""
Tests for the threaded invoice PDF route.

The expensive ReportLab render was moved off the gunicorn request thread
onto a module-level ``ThreadPoolExecutor`` (see ``invoices/__init__.py``
and ``invoices/pdf_generator.py``). This test focuses on the wiring,
not on PDF byte-for-byte fidelity:

* The executor is mocked so we don't actually run ReportLab in the
  test (other tests already exercise the layout indirectly via
  ``test_tenant_isolation.test_pdf_other_tenant_invoice_returns_404``,
  which hits the real route).
* The route under test (``/invoices/<id>/pdf``) is asserted to:
    1. submit ``generate_invoice_pdf`` to the executor with the right
       invoice id,
    2. respond ``200``,
    3. respond with ``Content-Type: application/pdf``.
"""
from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app import db
from models import Client, Invoice, InvoiceItem, Project, User


def _make_invoice_owner(suffix: str):
    """Spin up a fresh user + client + invoice. Mirrors the helper in
    ``tests/test_tenant_isolation.py`` but trimmed to just what the
    PDF route reads (no project / task / time entries)."""
    user = User(username=f'pdfowner_{suffix}',
                email=f'pdfowner_{suffix}@example.com')
    user.set_password('testpassword123')
    db.session.add(user)
    db.session.flush()

    client_row = Client(name=f'pdfclient_{suffix}', user_id=user.id)
    db.session.add(client_row)
    db.session.flush()

    project = Project(
        name=f'pdfproject_{suffix}',
        start_date=datetime.utcnow(),
        user_id=user.id,
        client_id=client_row.id,
        status='active',
    )
    db.session.add(project)
    db.session.flush()

    import uuid as _uuid
    invoice = Invoice(
        invoice_number=f'INV-{_uuid.uuid4().hex[:8].upper()}',
        amount=Decimal('250.00'),
        currency='USD',
        status='draft',
        due_date=datetime.utcnow() + timedelta(days=14),
        client_id=client_row.id,
        project_id=project.id,
    )
    db.session.add(invoice)
    db.session.flush()
    db.session.add(InvoiceItem(
        description='consulting',
        quantity=Decimal('2.5000'),
        rate=Decimal('100.00'),
        amount=Decimal('250.00'),
        invoice_id=invoice.id,
    ))
    db.session.commit()
    return user, invoice


def _login(client, email):
    resp = client.post(
        '/auth/login',
        data={'email': email, 'password': 'testpassword123', 'remember_me': False},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302), f'Login failed: {resp.status_code}'


@pytest.fixture()
def invoice_owner(app, db_session):
    import uuid
    user, invoice = _make_invoice_owner(uuid.uuid4().hex[:8])
    yield user, invoice


def test_pdf_route_uses_executor_and_returns_pdf(client, invoice_owner, monkeypatch):
    """Route should submit work to the executor and stream the bytes
    back as ``application/pdf`` with HTTP 200."""
    user, invoice = invoice_owner
    _login(client, user.email)

    # Build a fake executor whose ``submit`` returns a future-like
    # object whose ``.result(timeout=...)`` hands back canned PDF bytes.
    # We deliberately do NOT call the real ``generate_invoice_pdf`` --
    # the point of the test is that the route delegates correctly, not
    # that ReportLab still produces a valid PDF (covered separately).
    fake_pdf_bytes = b'%PDF-1.4 fake-pdf-bytes-for-test'

    fake_future = MagicMock()
    fake_future.result.return_value = fake_pdf_bytes

    fake_executor = MagicMock()
    fake_executor.submit.return_value = fake_future

    # Patch the accessor the route uses; that way we replace ``the``
    # executor for this one request without disturbing the module-level
    # singleton (which other tests / processes may rely on).
    import invoices.routes as routes_mod
    monkeypatch.setattr(routes_mod, 'get_pdf_executor', lambda: fake_executor)

    resp = client.get(f'/invoices/{invoice.id}/pdf', follow_redirects=False)

    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'application/pdf'
    assert resp.data == fake_pdf_bytes

    # The route must have actually delegated (not fallen back to
    # synchronous rendering on the request thread). Both the invoice
    # id AND the user id must be passed through so the worker can do
    # its defence-in-depth ownership re-check.
    fake_executor.submit.assert_called_once()
    submit_args = fake_executor.submit.call_args.args
    assert submit_args[0] is routes_mod.generate_invoice_pdf
    assert submit_args[1] == invoice.id
    assert submit_args[2] == user.id

    # And the route must have actually awaited the future with a
    # non-None timeout (so a runaway render can't hang a worker).
    fake_future.result.assert_called_once()
    timeout_kw = fake_future.result.call_args.kwargs.get('timeout')
    timeout_pos = (
        fake_future.result.call_args.args[0]
        if fake_future.result.call_args.args else None
    )
    assert (timeout_kw or timeout_pos) is not None


def test_pdf_route_returns_503_on_render_timeout(client, invoice_owner, monkeypatch):
    """When the future times out, the route must respond 503 (not hang
    the worker, not 500)."""
    user, invoice = invoice_owner
    _login(client, user.email)

    fake_future = MagicMock()
    fake_future.result.side_effect = FuturesTimeoutError()
    fake_executor = MagicMock()
    fake_executor.submit.return_value = fake_future

    import invoices.routes as routes_mod
    monkeypatch.setattr(routes_mod, 'get_pdf_executor', lambda: fake_executor)

    resp = client.get(f'/invoices/{invoice.id}/pdf', follow_redirects=False)
    assert resp.status_code == 503
    # Best-effort cancel must have been attempted.
    fake_future.cancel.assert_called_once()


def test_pdf_route_returns_503_when_worker_raises(client, invoice_owner, monkeypatch):
    """An unexpected exception from the worker must surface as 503,
    not a 500 with a stack trace leak."""
    user, invoice = invoice_owner
    _login(client, user.email)

    fake_future = MagicMock()
    fake_future.result.side_effect = RuntimeError("reportlab blew up")
    fake_executor = MagicMock()
    fake_executor.submit.return_value = fake_future

    import invoices.routes as routes_mod
    monkeypatch.setattr(routes_mod, 'get_pdf_executor', lambda: fake_executor)

    resp = client.get(f'/invoices/{invoice.id}/pdf', follow_redirects=False)
    assert resp.status_code == 503


def test_pdf_route_returns_503_when_submit_raises(client, invoice_owner, monkeypatch):
    """If the executor itself rejects the job (shutdown / runtime
    failure), the route must still respond 503 -- not 500."""
    user, invoice = invoice_owner
    _login(client, user.email)

    fake_executor = MagicMock()
    fake_executor.submit.side_effect = RuntimeError("executor is shut down")

    import invoices.routes as routes_mod
    monkeypatch.setattr(routes_mod, 'get_pdf_executor', lambda: fake_executor)

    resp = client.get(f'/invoices/{invoice.id}/pdf', follow_redirects=False)
    assert resp.status_code == 503
