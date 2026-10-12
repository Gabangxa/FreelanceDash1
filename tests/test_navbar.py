"""
Navbar active state, the Time nav entry, and the projects-list filter (FD-026).

The navbar decided each item's active state with a substring check on
``request.endpoint``. The dashboard is ``projects.dashboard`` and the time
pages live in the projects blueprint, so two items lit up at once. On an
unmatched URL the endpoint is None, so rendering the 404 page raised
TypeError -- and so did the 500 page that extends the same base template,
leaving logged-in users with a bare server error for any mistyped URL.

The projects list had Bootstrap pill "tabs" pointing at tab panes that never
existed, so the Active / Completed filters did nothing.
"""
from datetime import datetime
from html.parser import HTMLParser
from types import SimpleNamespace
import uuid

from flask import render_template_string
import pytest

from app import db
from models import Client, Project, Task, TimeEntry, User

PASSWORD = "testpassword123"
PRIMARY_NAV_LABELS = {"Dashboard", "Projects", "Clients", "Invoices", "Time"}


class _PageParser(HTMLParser):
    """Collect the main navbar's links and every element's attributes."""

    def __init__(self):
        super().__init__()
        self._nav_depth = 0
        self._open_nav_link = None
        self.nav_links = []
        self.elements = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        classes = (attrs.get("class") or "").split()
        if tag == "nav" and (self._nav_depth or attrs.get("aria-label") == "Main"):
            self._nav_depth += 1
        elif tag == "a" and self._nav_depth and "nav-link" in classes:
            self._open_nav_link = {
                "label": "",
                "href": attrs.get("href"),
                "classes": classes,
                "aria_current": attrs.get("aria-current"),
                "bs_toggle": attrs.get("data-bs-toggle"),
            }
            self.nav_links.append(self._open_nav_link)

    def handle_endtag(self, tag):
        if tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
        elif tag == "a":
            self._open_nav_link = None

    def handle_data(self, data):
        if self._open_nav_link is not None:
            self._open_nav_link["label"] += data.strip()


def _parse(response):
    parser = _PageParser()
    parser.feed(response.get_data(as_text=True))
    return parser


def _active_nav_labels(response):
    links = _parse(response).nav_links
    # Guard against a vacuous pass: "nothing active" must mean the navbar
    # rendered with nothing marked, not that the parser never found it.
    assert PRIMARY_NAV_LABELS <= {link["label"] for link in links}
    active = [link for link in links if "active" in link["classes"]]
    # The visual and the accessible "current page" markers must agree.
    assert all(link["aria_current"] == "page" for link in active)
    assert all(link["aria_current"] is None for link in links if link not in active)
    return [link["label"] for link in active]


@pytest.fixture()
def logged_in(app, client):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"nav_{suffix}", email=f"nav_{suffix}@example.com")
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()

    acme = Client(name="Acme", user_id=user.id)
    db.session.add(acme)
    db.session.flush()
    projects = [
        Project(name=name, status=status, start_date=datetime(2026, 1, 1),
                user_id=user.id, client_id=acme.id)
        for name, status in [("Live work", "active"), ("Shipped work", "completed")]
    ]
    db.session.add_all(projects)
    db.session.flush()
    task = Task(title="Draft wireframes", project_id=projects[0].id)
    time_entry = TimeEntry(start_time=datetime(2026, 1, 2, 9), end_time=datetime(2026, 1, 2, 10),
                           duration=60, project_id=projects[0].id)
    db.session.add_all([task, time_entry])
    db.session.commit()

    try:
        resp = client.post("/auth/login", data={"email": user.email, "password": PASSWORD})
        assert resp.status_code == 302 and "/auth/login" not in resp.headers["Location"]
        yield SimpleNamespace(projects=projects, task=task, time_entry=time_entry)
    finally:
        # The app fixture's SQLite is session-scoped; committed rows would
        # leak into later tests. User deletion cascades to everything above.
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


@pytest.mark.parametrize("path, expected", [
    ("/dashboard", ["Dashboard"]),
    ("/projects", ["Projects"]),
    ("/tasks/new", ["Projects"]),
    ("/clients/", ["Clients"]),
    ("/invoices/", ["Invoices"]),
    ("/invoices/from-time-entries", ["Invoices"]),
    ("/time-entries/batch", ["Time"]),
    ("/time-entries/statistics", ["Time"]),
    ("/settings/company", []),
    ("/notifications/", []),
])
def test_exactly_the_right_nav_item_is_active(client, logged_in, path, expected):
    resp = client.get(path)
    assert resp.status_code == 200
    assert _active_nav_labels(resp) == expected


@pytest.mark.parametrize("path_for, expected", [
    (lambda data: f"/projects/{data.projects[0].id}", ["Projects"]),
    (lambda data: f"/tasks/{data.task.id}", ["Projects"]),
    (lambda data: f"/time-entries/{data.time_entry.id}/edit", ["Time"]),
])
def test_detail_pages_activate_their_section(client, logged_in, path_for, expected):
    resp = client.get(path_for(logged_in))
    assert resp.status_code == 200
    assert _active_nav_labels(resp) == expected


def test_unknown_url_while_logged_in_renders_the_404_page(client, logged_in):
    resp = client.get("/no-such-page-anywhere")
    assert resp.status_code == 404
    assert "Page Not Found" in resp.get_data(as_text=True)
    assert _active_nav_labels(resp) == []


def test_templates_render_without_a_request_context(app):
    # CLI commands and background jobs render with only an app context.
    with app.app_context():
        assert render_template_string("{{ active_nav }}") == "None"


def test_time_nav_item_links_straight_to_time_entry(client, logged_in):
    links = _parse(client.get("/dashboard")).nav_links
    time_link = next(link for link in links if link["label"] == "Time")
    assert time_link["href"] == "/time-entries/batch"
    # A Bootstrap toggle would swallow the click and open a menu instead.
    assert time_link["bs_toggle"] is None


def test_time_statistics_stays_reachable_from_time_entry(client, logged_in):
    elements = _parse(client.get("/time-entries/batch")).elements
    assert any(tag == "a" and attrs.get("href") == "/time-entries/statistics"
               for tag, attrs in elements)


def test_project_rows_carry_the_status_the_filter_matches_on(client, logged_in):
    elements = _parse(client.get("/projects")).elements

    row_statuses = sorted(
        attrs["data-status"] for tag, attrs in elements
        if "project-item" in (attrs.get("class") or "").split()
    )
    assert row_statuses == ["active", "completed"]

    initial_pressed = {attrs["data-filter"]: attrs.get("aria-pressed")
                       for tag, attrs in elements
                       if tag == "button" and "data-filter" in attrs}
    assert initial_pressed == {"all": "true", "active": "false", "completed": "false"}
