"""
Tests for the new feature-gating API.

Covers:
* Free-tier defaults via ``has_feature`` and ``get_feature_limit``.
* Professional/business tier overrides.
* The unlimited -> ``None`` translation (vs legacy ``0`` sentinel).
* Backwards-compat shim ``has_subscription_feature`` (still works, with a
  ``DeprecationWarning``) for one boolean and one limit feature.
* Schema agreement between ``Subscription.get_features`` and the free-tier
  defaults dict (no key/type drift).
"""
import warnings
from datetime import datetime

import pytest

from app import db
from models import User
from polar.models import Subscription
from polar.features import (
    FEATURE_SCHEMA,
    KIND_BOOL,
    KIND_LIMIT,
    free_tier_features,
    features_for_tier,
)


_email_counter = 0


def _make_user(db_session):
    """Create a user with a unique email to avoid collisions across tests."""
    global _email_counter
    _email_counter += 1
    user = User(
        username=f"feature_user_{_email_counter}",
        email=f"feature_user_{_email_counter}@test.local",
    )
    user.set_password("doesnotmatter")
    db.session.add(user)
    db.session.commit()
    return user


def _attach_subscription(user, tier_name):
    """Attach an active Subscription of the given tier to ``user``."""
    sub = Subscription(
        user_id=user.id,
        polar_subscription_id=f"polar_sub_{user.id}_{tier_name}",
        tier_id=tier_name,
        tier_name=tier_name,
        status="active",
        amount=10.0,
    )
    db.session.add(sub)
    db.session.commit()
    return sub


# ---------------------------------------------------------------------------
# Free tier
# ---------------------------------------------------------------------------

def test_free_tier_user_gets_default_limits(db_session):
    user = _make_user(db_session)
    assert user.get_feature_limit("clients_limit") == 3
    assert user.get_feature_limit("projects_limit") == 5
    # team_members on free tier is a real zero, not "unlimited".
    assert user.get_feature_limit("team_members") == 0


def test_free_tier_user_lacks_premium_booleans(db_session):
    user = _make_user(db_session)
    assert user.has_feature("custom_branding") is False
    assert user.has_feature("advanced_reporting") is False
    assert user.has_feature("api_access") is False
    assert user.has_feature("priority_support") is False


# ---------------------------------------------------------------------------
# Professional tier
# ---------------------------------------------------------------------------

def test_professional_tier_unlimited_translates_to_none(db_session):
    user = _make_user(db_session)
    _attach_subscription(user, "professional")

    # The whole point of the rewrite: unlimited surfaces as None, NOT 0.
    assert user.get_feature_limit("clients_limit") is None
    assert user.get_feature_limit("projects_limit") is None


def test_professional_tier_unlocks_branding_and_reporting(db_session):
    user = _make_user(db_session)
    _attach_subscription(user, "professional")

    assert user.has_feature("custom_branding") is True
    assert user.has_feature("advanced_reporting") is True
    # Pro tier doesn't include API access / priority support / team members.
    assert user.has_feature("api_access") is False
    assert user.has_feature("priority_support") is False


# ---------------------------------------------------------------------------
# Business tier
# ---------------------------------------------------------------------------

def test_business_tier_unlocks_everything(db_session):
    user = _make_user(db_session)
    _attach_subscription(user, "business")

    assert user.get_feature_limit("clients_limit") is None
    assert user.get_feature_limit("projects_limit") is None
    assert user.get_feature_limit("team_members") == 3
    assert user.has_feature("custom_branding") is True
    assert user.has_feature("advanced_reporting") is True
    assert user.has_feature("api_access") is True
    assert user.has_feature("priority_support") is True


# ---------------------------------------------------------------------------
# Method contract violations
# ---------------------------------------------------------------------------

def test_get_feature_limit_rejects_boolean_features(db_session):
    user = _make_user(db_session)
    with pytest.raises(ValueError):
        user.get_feature_limit("custom_branding")


def test_has_feature_returns_false_for_non_boolean_features(db_session):
    user = _make_user(db_session)
    # Limits aren't booleans -- has_feature must not silently coerce.
    assert user.has_feature("clients_limit") is False


# ---------------------------------------------------------------------------
# Schema integrity (single source of truth)
# ---------------------------------------------------------------------------

def test_subscription_get_features_uses_shared_schema(db_session):
    """All keys in Subscription.get_features come from the shared schema,
    and free-tier defaults match exactly between the dict and the schema."""
    user = _make_user(db_session)
    sub = _attach_subscription(user, "professional")

    schema_keys = set(FEATURE_SCHEMA.keys())
    sub_keys = set(sub.get_features().keys())
    free_keys = set(free_tier_features().keys())

    assert schema_keys == sub_keys == free_keys, (
        "Subscription.get_features and free_tier_features must expose the "
        "exact same set of keys as FEATURE_SCHEMA."
    )


def test_features_for_tier_unknown_tier_falls_back_to_free():
    assert features_for_tier(None) == free_tier_features()
    assert features_for_tier("") == free_tier_features()
    assert features_for_tier("nonexistent_tier") == free_tier_features()


# ---------------------------------------------------------------------------
# Deprecation shim
# ---------------------------------------------------------------------------

def test_deprecated_shim_still_works_for_boolean(db_session):
    user = _make_user(db_session)
    _attach_subscription(user, "professional")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = user.has_subscription_feature("custom_branding")

    assert result is True
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        "has_subscription_feature must emit a DeprecationWarning."
    )


def test_deprecated_shim_preserves_legacy_zero_unlimited(db_session):
    """The shim must stay bug-compatible with the old method so unmigrated
    callers (which expected 0 for unlimited) keep working."""
    user = _make_user(db_session)
    _attach_subscription(user, "professional")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result = user.has_subscription_feature("clients_limit")

    # Pro tier = unlimited. Old method returned 0; shim must too.
    assert result == 0


def test_deprecated_shim_returns_real_cap_for_free_tier(db_session):
    user = _make_user(db_session)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        result = user.has_subscription_feature("clients_limit")
    assert result == 3


def test_deprecated_shim_unknown_limit_returns_legacy_zero(db_session):
    """The original method returned 0 for unknown *_limit names. Preserve
    that so any typoed legacy caller comparing with ``>=`` doesn't blow up."""
    user = _make_user(db_session)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert user.has_subscription_feature("nonexistent_limit") == 0
        assert user.has_subscription_feature("totally_unknown_feature") is False
