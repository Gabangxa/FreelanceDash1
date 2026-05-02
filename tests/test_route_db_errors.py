"""
Regression tests for Task #3: ensure mid-route SQLAlchemyError on commit
is caught by the create_client and create_project routes, producing a
graceful response (no 500) with the row never being persisted.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from app import db
from models import User, Client, Project, Invoice


def _login(client, email, password="testpassword123"):
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302), f"Login returned {resp.status_code}"


def _make_user(username, email, password="testpassword123"):
    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def test_create_client_handles_sqlalchemy_error_on_commit(client, db_session):
    """POST /clients/new must not 500 when db.session.commit raises."""
    user = _make_user("dberr_client_user", "dberr_client@test.local")
    _login(client, "dberr_client@test.local")

    with patch.object(db.session, "commit", side_effect=SQLAlchemyError("boom")):
        resp = client.post(
            "/clients/new",
            data={
                "name": "Test Client DBErr",
                "email": "tc-dberr@example.com",
                "company": "TC Inc",
                "address": "123 Main St",
                "submit": "Save Client",
            },
            follow_redirects=False,
        )

    assert resp.status_code != 500, (
        f"Route returned 500 instead of handling SQLAlchemyError gracefully: "
        f"{resp.data[:300]!r}"
    )

    # Verify no client row leaked into the DB after the failed commit.
    leaked = Client.query.filter_by(
        name="Test Client DBErr", user_id=user.id
    ).first()
    assert leaked is None, (
        "Client should not have been persisted after commit failure"
    )


def test_create_project_handles_sqlalchemy_error_on_commit(client, db_session):
    """POST /projects/new must not 500 when db.session.commit raises."""
    user = _make_user("dberr_proj_user", "dberr_proj@test.local")

    # Need a client row so the ProjectForm.client_id select has a valid choice.
    parent_client = Client(name="Proj Client DBErr", user_id=user.id)
    db.session.add(parent_client)
    db.session.commit()
    client_id = parent_client.id

    _login(client, "dberr_proj@test.local")

    with patch.object(db.session, "commit", side_effect=SQLAlchemyError("boom")):
        resp = client.post(
            "/projects/new",
            data={
                "name": "Test Project DBErr",
                "description": "desc",
                "start_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "client_id": str(client_id),
                "submit": "Save Project",
            },
            follow_redirects=False,
        )

    assert resp.status_code != 500, (
        f"Route returned 500 instead of handling SQLAlchemyError gracefully: "
        f"{resp.data[:300]!r}"
    )

    # Verify no project row leaked into the DB after the failed commit.
    leaked = Project.query.filter_by(
        name="Test Project DBErr", user_id=user.id
    ).first()
    assert leaked is None, (
        "Project should not have been persisted after commit failure"
    )


def test_create_invoice_handles_sqlalchemy_error_on_commit(client, db_session):
    """POST /invoices/new must not 500 when db.session.commit raises."""
    user = _make_user("dberr_inv_user", "dberr_inv@test.local")

    # Need a client + project so the invoice form's selects have valid choices.
    parent_client = Client(name="Inv Client DBErr", user_id=user.id)
    db.session.add(parent_client)
    db.session.flush()
    parent_project = Project(
        name="Inv Project DBErr",
        start_date=datetime.utcnow(),
        user_id=user.id,
        client_id=parent_client.id,
    )
    db.session.add(parent_project)
    db.session.commit()
    client_id = parent_client.id
    project_id = parent_project.id

    _login(client, "dberr_inv@test.local")

    due_date = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")

    with patch.object(db.session, "commit", side_effect=SQLAlchemyError("boom")):
        resp = client.post(
            "/invoices/new",
            data={
                "client_id": str(client_id),
                "project_id": str(project_id),
                "currency": "USD",
                "status": "draft",
                "due_date": due_date,
                "notes": "test invoice",
                "items-0-description": "Consulting",
                "items-0-quantity": "2",
                "items-0-rate": "100",
                "submit": "Save Invoice",
            },
            follow_redirects=False,
        )

    assert resp.status_code != 500, (
        f"Route returned 500 instead of handling SQLAlchemyError gracefully: "
        f"{resp.data[:300]!r}"
    )

    # Verify no invoice row leaked into the DB after the failed commit.
    leaked = Invoice.query.filter_by(client_id=client_id).first()
    assert leaked is None, (
        "Invoice should not have been persisted after commit failure"
    )
