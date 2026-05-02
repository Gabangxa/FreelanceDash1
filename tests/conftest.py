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

import pytest

from app import app as flask_app, db


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
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


@pytest.fixture(autouse=True)
def _reset_nats_client_state():
    """Make sure no test leaves the nats_client module in a partially-
    connected state. The module is a no-op stub under FLASK_ENV=test
    (NATS_URL is unset), so reset is essentially free, but doing it
    eagerly between tests stops one test's monkeypatched NATS_URL from
    polluting the next."""
    import nats_client
    nats_client.reset_for_tests()
    yield
    nats_client.reset_for_tests()
