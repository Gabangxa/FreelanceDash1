"""
Regression tests for the free-tier cap redirect (FD-004).

clients/routes.py and projects/routes.py redirected a capped free user to
``url_for('polar.index')``, but the blueprint is registered as
``subscriptions`` -- so the cap path raised a BuildError, which escaped the
local handlers and rendered a 500, destroying the user's form input at the
exact upgrade moment. These assert the cap now redirects cleanly to the
subscriptions page.
"""
from datetime import datetime
import uuid

import pytest

from app import db
from models import Client, Project, User


def _make_free_user(suffix):
    user = User(username=f"cap_{suffix}", email=f"cap_{suffix}@example.com")
    user.set_password("testpassword123")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email, password="testpassword123"):
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password, "remember_me": False},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302)


def test_client_cap_redirects_to_subscriptions(client, app, db_session):
    suffix = uuid.uuid4().hex[:8]
    user = _make_free_user(suffix)
    # Free tier allows 3 clients; fill the quota.
    for i in range(3):
        db.session.add(Client(name=f"c{i}", user_id=user.id))
    db.session.commit()

    _login(client, user.email)
    resp = client.post(
        "/clients/new",
        data={"name": "one too many"},
        follow_redirects=False,
    )

    assert resp.status_code == 302, resp.data[:400]
    assert resp.headers["Location"].endswith("/subscriptions/")
    # The 4th client was not created.
    assert Client.query.filter_by(user_id=user.id).count() == 3


def test_project_cap_redirects_to_subscriptions(client, app, db_session):
    suffix = uuid.uuid4().hex[:8]
    user = _make_free_user(suffix)
    client_row = Client(name="c", user_id=user.id)
    db.session.add(client_row)
    db.session.flush()
    # Free tier allows 5 projects; fill the quota.
    for i in range(5):
        db.session.add(Project(
            name=f"p{i}",
            start_date=datetime.utcnow(),
            user_id=user.id,
            client_id=client_row.id,
            status="active",
        ))
    db.session.commit()

    _login(client, user.email)
    # The project cap is checked before form validation, so a GET already
    # trips it.
    resp = client.get("/projects/new", follow_redirects=False)

    assert resp.status_code == 302, resp.data[:400]
    assert resp.headers["Location"].endswith("/subscriptions/")
    assert Project.query.filter_by(user_id=user.id).count() == 5
