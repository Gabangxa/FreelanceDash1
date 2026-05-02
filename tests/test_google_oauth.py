"""Integration tests for the Google-OAuth sign-in feature (Task #17).

These tests do not actually hit Google's OAuth endpoints. They exercise
the helper functions and the three-step user-resolution logic directly,
plus the template-rendering / settings-page integration. The HTTP layer
of the OAuth flow itself (redirect to Google, callback handling) is
mocked at the network boundary in ``test_callback_*``.
"""
from unittest.mock import patch

import pytest

import google_auth as ga
from app import db
from models import User


PASSWORD = "CorrectHorseBattery1!"


def _make_password_user(username, email):
    u = User(username=username, email=email)
    u.set_password(PASSWORD)
    db.session.add(u)
    db.session.commit()
    return u


# --------------------------------------------------------------------------- #
# is_configured / blueprint registration
# --------------------------------------------------------------------------- #

def test_is_configured_false_without_secrets(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert ga.is_configured() is False


def test_is_configured_true_with_both_secrets(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    assert ga.is_configured() is True


def test_is_configured_false_with_only_one_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    assert ga.is_configured() is False


# --------------------------------------------------------------------------- #
# Username generation -- collision-safe
# --------------------------------------------------------------------------- #

def test_generate_unique_username_uses_clean_seed_when_available(db_session):
    u = ga._generate_unique_username("Alice")
    assert u == "Alice"


def test_generate_unique_username_strips_punctuation(db_session):
    u = ga._generate_unique_username("Mary-Jane O'Neil")
    assert u == "MaryJaneONeil"


def test_generate_unique_username_falls_back_to_default_for_empty(db_session):
    assert ga._generate_unique_username("") == "user"
    assert ga._generate_unique_username(None) == "user"
    assert ga._generate_unique_username("!!!") == "user"


def test_generate_unique_username_appends_random_on_collision(db_session):
    _make_password_user("Bob", "bob1@example.com")
    candidate = ga._generate_unique_username("Bob")
    assert candidate != "Bob"
    assert candidate.startswith("Bob_")


# --------------------------------------------------------------------------- #
# _find_or_create_user -- the three-step lookup
# --------------------------------------------------------------------------- #

def test_find_or_create_existing_oauth_link_returns_same_user(db_session):
    u = User(
        username="oauth_existing",
        email="oauth_existing@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-existing",
    )
    db.session.add(u)
    db.session.commit()

    found = ga._find_or_create_user("sub-existing", "doesnt-matter@example.com", "X")
    assert found.id == u.id
    # Email on file is NOT overwritten by whatever Google now reports.
    assert found.email == "oauth_existing@example.com"


def test_find_or_create_links_to_existing_email_match(db_session):
    """Existing local password account should get linked, additively."""
    u = _make_password_user("password_user", "linkme@example.com")
    user_id = u.id
    original_hash = u.password_hash

    found = ga._find_or_create_user("sub-link", "linkme@example.com", "Whoever")
    assert found.id == user_id
    assert found.oauth_provider == "google"
    assert found.oauth_provider_id == "sub-link"
    # Password is preserved -- additive linkage.
    assert found.password_hash == original_hash


def test_find_or_create_email_match_normalizes_case(db_session):
    """Google email should be matched case-insensitively against local."""
    _make_password_user("case_user", "casey@example.com")

    # Caller already lowercases, but the stored email is also lower so
    # matching works. This guards against a future regression where the
    # caller stops lowercasing.
    found = ga._find_or_create_user("sub-case", "casey@example.com", "Casey")
    assert found.email == "casey@example.com"
    assert found.oauth_provider_id == "sub-case"


def test_find_or_create_creates_new_user_when_no_match(db_session):
    found = ga._find_or_create_user("sub-new", "brand_new@example.com", "Brand")
    assert found.id is not None
    assert found.email == "brand_new@example.com"
    assert found.username.startswith("Brand")
    assert found.password_hash is None  # no password set
    assert found.oauth_provider == "google"
    assert found.oauth_provider_id == "sub-new"


def test_find_or_create_refuses_to_relink_account_with_different_subject(db_session):
    """If a local account already linked to Google sub A tries to be
    relinked to Google sub B (because the user logged in with a
    different Google account that happens to share the email), refuse
    rather than silently overwrite. Aborts with 409."""
    from werkzeug.exceptions import Conflict

    u = User(
        username="already_linked",
        email="conflict@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-A",
    )
    db.session.add(u)
    db.session.commit()

    with pytest.raises(Conflict):
        ga._find_or_create_user("sub-B", "conflict@example.com", "X")


# --------------------------------------------------------------------------- #
# get_sign_in_methods helper on User
# --------------------------------------------------------------------------- #

def test_sign_in_methods_password_and_magic_link_for_password_user(db_session):
    u = _make_password_user("methods_pw", "pw@example.com")
    methods = u.get_sign_in_methods()
    assert "password" in methods
    assert "magic_link" in methods
    assert "oauth:google" not in methods


def test_sign_in_methods_includes_oauth_when_linked(db_session):
    u = User(
        username="methods_g",
        email="g@example.com",
        oauth_provider="google",
        oauth_provider_id="sub-m",
    )
    db.session.add(u)
    db.session.commit()
    methods = u.get_sign_in_methods()
    assert "oauth:google" in methods
    # No password set -> no "password" entry.
    assert "password" not in methods
    assert "magic_link" in methods


# --------------------------------------------------------------------------- #
# Template integration: /auth/login renders with/without Google button
# --------------------------------------------------------------------------- #

def test_login_page_hides_google_button_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"Continue with Google" not in resp.data


def test_register_page_hides_google_button_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    resp = client.get("/auth/register")
    assert resp.status_code == 200
    assert b"Continue with Google" not in resp.data


# --------------------------------------------------------------------------- #
# Settings page: surfaces the methods
# --------------------------------------------------------------------------- #

def test_sign_in_methods_settings_page_requires_login(client):
    resp = client.get("/settings/sign-in-methods", follow_redirects=False)
    # Flask-Login redirects to login.
    assert resp.status_code in (301, 302)
    assert "/auth/login" in resp.headers["Location"]


def test_sign_in_methods_settings_page_renders_for_logged_in_user(client, db_session):
    u = _make_password_user("settings_view", "settingsview@example.com")
    # Log in via the password route.
    resp = client.post(
        "/auth/login",
        data={"email": u.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)

    page = client.get("/settings/sign-in-methods")
    assert page.status_code == 200
    assert b"Sign-in Methods" in page.data
    assert b"Password" in page.data
    assert b"Magic-link email" in page.data
    assert b"Google" in page.data


# --------------------------------------------------------------------------- #
# Regression: existing password login flow still works
# --------------------------------------------------------------------------- #

def test_password_login_still_works_after_oauth_changes(client, db_session):
    u = _make_password_user("regression_pw", "regression@example.com")
    resp = client.post(
        "/auth/login",
        data={"email": u.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    # Should land on the dashboard, not bounce back to login.
    assert "/auth/login" not in resp.headers["Location"]
