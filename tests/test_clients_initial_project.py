"""
Tests for Task #4: nested WTForms sub-form for the optional initial
project on ``POST /clients/new``.

Covers:
* Creating a client with the include-project box unchecked -- no project
  row is created.
* Creating a client with the include-project box checked plus a name +
  start date -- both client and project rows are persisted.
* Creating a client with the include-project box checked but no project
  name -- the form re-renders (200) with the validation error and
  neither row is persisted.
* Editing an existing client -- the create.html template still renders
  without the project block and the update succeeds.
"""
from datetime import datetime

from app import db
from models import User, Client, Project


def _make_user(username, email, password="testpassword123"):
    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email, password="testpassword123"):
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 302, (
        f"Login should have redirected on success but got {resp.status_code}"
    )
    assert "/auth/login" not in (resp.headers.get("Location") or ""), (
        "Login redirected back to /auth/login -- credentials were wrong"
    )


def test_create_client_without_project(client, db_session):
    user = _make_user("init_proj_off", "init_proj_off@example.com")
    _login(client, "init_proj_off@example.com")

    resp = client.post(
        "/clients/new",
        data={
            "name": "Solo Client",
            "email": "solo@example.com",
            "company": "",
            "address": "",
            # project sub-form: include unchecked (omitted from form data)
            "project-name": "",
            "project-description": "",
            "project-start_date": "",
            "project-end_date": "",
            "submit": "Save Client",
        },
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303), (
        f"Expected redirect after success, got {resp.status_code}: "
        f"{resp.data[:300]!r}"
    )

    created = Client.query.filter_by(name="Solo Client", user_id=user.id).first()
    assert created is not None, "Client should have been persisted"
    assert created.email == "solo@example.com"

    # No project should have been created when include is unchecked.
    project_count = Project.query.filter_by(
        user_id=user.id, client_id=created.id
    ).count()
    assert project_count == 0, "No project row should have been created"


def test_create_client_with_project_happy_path(client, db_session):
    user = _make_user("init_proj_on", "init_proj_on@example.com")
    _login(client, "init_proj_on@example.com")

    today = datetime.utcnow().strftime("%Y-%m-%d")
    resp = client.post(
        "/clients/new",
        data={
            "name": "Combo Client",
            "email": "combo@example.com",
            "company": "Combo Co",
            "address": "1 Main St",
            "project-include": "y",
            "project-name": "Initial Project",
            "project-description": "Kickoff work",
            "project-start_date": today,
            "project-end_date": "",
            "submit": "Save Client",
        },
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303), (
        f"Expected redirect after success, got {resp.status_code}: "
        f"{resp.data[:300]!r}"
    )

    created = Client.query.filter_by(name="Combo Client", user_id=user.id).first()
    assert created is not None, "Client should have been persisted"

    project = Project.query.filter_by(
        user_id=user.id, client_id=created.id, name="Initial Project"
    ).first()
    assert project is not None, "Project should have been persisted"
    assert project.description == "Kickoff work"
    assert project.start_date is not None
    assert project.start_date.strftime("%Y-%m-%d") == today
    assert project.end_date is None
    assert project.status == "active"


def test_create_client_with_project_missing_name_rerenders_with_error(
    client, db_session
):
    user = _make_user("init_proj_bad", "init_proj_bad@example.com")
    _login(client, "init_proj_bad@example.com")

    today = datetime.utcnow().strftime("%Y-%m-%d")
    resp = client.post(
        "/clients/new",
        data={
            "name": "BadCombo Client",
            "email": "",
            "company": "",
            "address": "",
            "project-include": "y",
            # name intentionally missing
            "project-name": "",
            "project-description": "no name supplied",
            "project-start_date": today,
            "project-end_date": "",
            "submit": "Save Client",
        },
        follow_redirects=False,
    )

    # Validation failure should re-render the form (200), not redirect
    # and not 500.
    assert resp.status_code == 200, (
        f"Expected 200 re-render on validation error, got {resp.status_code}"
    )

    # Neither row should have been persisted.
    leaked_client = Client.query.filter_by(
        name="BadCombo Client", user_id=user.id
    ).first()
    assert leaked_client is None, "Client must not be saved when project sub-form fails"

    leaked_project = Project.query.filter_by(
        user_id=user.id, name=""
    ).first()
    assert leaked_project is None, "Project must not be saved when name missing"

    # Surface the validation error message in the rendered HTML.
    body = resp.data.decode("utf-8", errors="replace")
    assert "Project name is required" in body, (
        "Expected the project-name validation error to be rendered in the page"
    )


def test_edit_existing_client_still_works(client, db_session):
    user = _make_user("edit_client_user", "edit_client@example.com")
    _login(client, "edit_client@example.com")

    existing = Client(
        name="Editable Client",
        email="orig@example.com",
        company="Orig Co",
        address="orig addr",
        user_id=user.id,
    )
    db.session.add(existing)
    db.session.commit()
    cid = existing.id

    # GET should render fine without project sub-form blowing up on the
    # missing ``project`` attribute on the Client model.
    get_resp = client.get(f"/clients/{cid}/edit")
    assert get_resp.status_code == 200, (
        f"Edit form GET failed: {get_resp.status_code}"
    )
    body = get_resp.data.decode("utf-8", errors="replace")
    # The project sub-form block must NOT render in the edit view.
    assert "Initial Project (Optional)" not in body, (
        "Edit view should not show the initial-project block"
    )

    # POST update -- no project fields submitted (template doesn't render
    # them), include is therefore unchecked, validation must still pass.
    post_resp = client.post(
        f"/clients/{cid}/edit",
        data={
            "name": "Edited Client",
            "email": "edited@example.com",
            "company": "Edited Co",
            "address": "new addr",
            "submit": "Save Client",
        },
        follow_redirects=False,
    )
    assert post_resp.status_code in (302, 303), (
        f"Expected redirect after successful edit, got {post_resp.status_code}"
    )

    refreshed = db.session.get(Client, cid)
    assert refreshed.name == "Edited Client"
    assert refreshed.email == "edited@example.com"
    assert refreshed.company == "Edited Co"
    assert refreshed.address == "new addr"
