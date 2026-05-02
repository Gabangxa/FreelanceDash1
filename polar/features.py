"""
Single source of truth for the subscription feature schema.

Both ``polar.models.Subscription.get_features`` and ``models.User``'s
feature-gating methods (``has_feature`` / ``get_feature_limit``) derive
their behavior from the constants in this module. Previously the free-tier
defaults were duplicated between ``models.py`` and ``polar/models.py``,
and the legacy ``has_subscription_feature`` returned a polymorphic
``bool | int`` value that callers had to defensively re-cast.

Conventions
-----------
* Every feature has exactly one ``kind`` -- one of:
    * ``KIND_BOOL``  -- a yes/no flag.
    * ``KIND_LIMIT`` -- a non-negative integer cap. ``None`` means
                        *unlimited* (replacing the legacy ``0`` sentinel,
                        which was indistinguishable from a real zero cap
                        like ``team_members=0`` on free tier).
    * ``KIND_LIST``  -- a list of allowed values (e.g. invoice templates).
* Per-tier overrides only need to list the fields that differ from the
  free-tier default. Everything else is inherited.
"""
from typing import Any, Dict, Optional

KIND_BOOL = "bool"
KIND_LIMIT = "limit"
KIND_LIST = "list"


# The schema. Each entry: kind + free-tier default.
# The keys here ARE the public feature names used everywhere in the app.
FEATURE_SCHEMA: Dict[str, Dict[str, Any]] = {
    # Numeric limits. None means unlimited. 0 means "literally zero".
    "clients_limit":      {"kind": KIND_LIMIT, "free_default": 3},
    "projects_limit":     {"kind": KIND_LIMIT, "free_default": 5},
    "team_members":       {"kind": KIND_LIMIT, "free_default": 0},

    # Boolean feature flags.
    "custom_branding":    {"kind": KIND_BOOL,  "free_default": False},
    "advanced_reporting": {"kind": KIND_BOOL,  "free_default": False},
    "api_access":         {"kind": KIND_BOOL,  "free_default": False},
    "priority_support":   {"kind": KIND_BOOL,  "free_default": False},

    # Lists.
    "invoice_templates":  {"kind": KIND_LIST,  "free_default": ["basic"]},
}


# Per-tier overrides. Anything omitted falls back to the free-tier default.
# Use ``None`` for ``KIND_LIMIT`` features that should be unlimited on that tier.
TIER_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "professional": {
        "clients_limit": None,    # unlimited
        "projects_limit": None,   # unlimited
        "custom_branding": True,
        "advanced_reporting": True,
        "invoice_templates": ["basic", "professional", "elegant"],
    },
    "business": {
        "clients_limit": None,    # unlimited
        "projects_limit": None,   # unlimited
        "custom_branding": True,
        "advanced_reporting": True,
        "team_members": 3,
        "api_access": True,
        "priority_support": True,
        "invoice_templates": ["basic", "professional", "elegant", "premium", "custom"],
    },
}


def feature_kind(feature_name: str) -> Optional[str]:
    """Return the kind ('bool'/'limit'/'list') of a feature, or None if unknown."""
    spec = FEATURE_SCHEMA.get(feature_name)
    return spec["kind"] if spec else None


def free_tier_features() -> Dict[str, Any]:
    """Return a fresh dict of all features at their free-tier defaults."""
    # Important: copy list values so callers can't mutate the schema.
    return {
        name: (list(spec["free_default"]) if spec["kind"] == KIND_LIST else spec["free_default"])
        for name, spec in FEATURE_SCHEMA.items()
    }


def features_for_tier(tier_name: Optional[str]) -> Dict[str, Any]:
    """Return the full feature dict for a named tier.

    Falls back to free-tier defaults when ``tier_name`` is None, empty, or
    not in ``TIER_OVERRIDES``.
    """
    features = free_tier_features()
    overrides = TIER_OVERRIDES.get((tier_name or "").lower(), {})
    for name, value in overrides.items():
        # Defensive: copy list overrides so they can't be mutated by callers.
        if isinstance(value, list):
            features[name] = list(value)
        else:
            features[name] = value
    return features
