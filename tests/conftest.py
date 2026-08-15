"""
Shared pytest fixtures.

Tests run against an isolated in-memory SQLite database so they have no
dependency on a running Postgres and leave no residue between runs.
``FLASK_ENV=test`` is set before the app module is imported so the
production guards in app.py don't trip.
"""
import os

# Must be set BEFORE the first `import app` so production guards stand down.
# Force-set (not setdefault) so the test suite is deterministic regardless of
# the developer's shell environment -- otherwise a stray DATABASE_URL pointing
# at the dev Postgres would silently mutate the real DB.
os.environ["FLASK_ENV"] = "test"
os.environ["FLASK_SECRET_KEY"] = "test-secret-key-do-not-use-in-prod"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# The Google OAuth blueprint only registers when these are present at app
# import time (app.py); without them the /google_login/* routes 404 and the
# callback tests fail on any machine that lacks the real secrets. Dummy
# values register the routes; tests that exercise the "not configured" path
# delete these via monkeypatch, which works because the enabled/disabled
# check reads the environment per-request, not at import.
os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-google-client-id"
os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "test-google-client-secret"

import pytest

from app import app as flask_app, db


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        # Off by default: the whole suite shares one client IP, so the auth
        # rate limiter would otherwise trip across unrelated tests. The
        # dedicated rate-limit test flips this on for its own scope.
        AUTH_RATE_LIMIT_ENABLED=False,
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_session(app):
    """Yield a clean session and roll back after each test."""
    with app.app_context():
        yield db.session
        db.session.rollback()
