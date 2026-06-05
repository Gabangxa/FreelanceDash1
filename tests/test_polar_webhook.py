"""Tests for the Polar.sh webhook endpoint and signature verification.

These cover the security boundary (unsigned / wrong-signature rejection)
and the happy paths for ``subscription.created`` (creates a Subscription
row + log) and ``subscription.canceled`` (flips the row to cancelled).
"""
import base64
import hashlib
import hmac
import json
import os
import time

import pytest

from app import db
from models import User
from polar.models import Subscription, SubscriptionLog


WEBHOOK_SECRET_PLAIN = "supersecrettestkey"


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _set_polar_env(monkeypatch):
    """All webhook tests need a webhook secret + API key visible."""
    monkeypatch.setenv("POLAR_API_KEY", "test-api-key")
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", WEBHOOK_SECRET_PLAIN)


@pytest.fixture
def webhook_user(app):
    """A persistent user that webhooks can attach subscriptions to."""
    with app.app_context():
        user = User(username="polaruser", email="polar@example.com")
        user.password_hash = "x"  # not used; bypass for Subscription FK only
        db.session.add(user)
        db.session.commit()
        user_id = user.id
    yield user_id
    with app.app_context():
        SubscriptionLog.query.filter_by(user_id=user_id).delete()
        Subscription.query.filter_by(user_id=user_id).delete()
        User.query.filter_by(id=user_id).delete()
        db.session.commit()


def _sign(secret: str, msg_id: str, ts: int, body: bytes) -> str:
    signed = f"{msg_id}.{ts}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("utf-8")


def _post_event(client, event: dict, *, sign: bool = True,
                wrong_sig: bool = False, secret: str = WEBHOOK_SECRET_PLAIN):
    body = json.dumps(event).encode("utf-8")
    msg_id = "evt_test_" + str(int(time.time() * 1000))
    ts = int(time.time())
    headers = {"Content-Type": "application/json"}
    if sign:
        sig = _sign(secret, msg_id, ts, body)
        if wrong_sig:
            # Flip the last char of the base64 signature so it stops matching
            # but is still valid base64.
            v, b64 = sig.split(",", 1)
            tweaked = b64[:-2] + ("A" if b64[-2] != "A" else "B") + b64[-1]
            sig = v + "," + tweaked
        headers.update({
            "webhook-id": msg_id,
            "webhook-timestamp": str(ts),
            "webhook-signature": sig,
        })
    return client.post("/subscriptions/webhook", data=body, headers=headers)


def _subscription_event(event_type: str, *, user_id: int,
                        polar_sub_id: str = "sub_polar_abc123",
                        status: str = "active") -> dict:
    return {
        "type": event_type,
        "data": {
            "id": polar_sub_id,
            "status": status,
            "amount": 1300,  # $13.00 in minor units
            "currency": "USD",
            "recurring_interval": "month",
            "started_at": "2026-05-03T12:00:00Z",
            "current_period_end": "2026-06-03T12:00:00Z",
            "product": {"name": "Professional"},
            "metadata": {
                "user_id": str(user_id),
                "tier_id": "professional",
                "billing_interval": "monthly",
            },
        },
    }


# --------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------- #
def test_webhook_missing_signature_is_rejected(client, webhook_user, app):
    event = _subscription_event("subscription.created", user_id=webhook_user)
    response = _post_event(client, event, sign=False)
    assert response.status_code == 401
    with app.app_context():
        assert Subscription.query.filter_by(user_id=webhook_user).count() == 0


def test_webhook_wrong_signature_is_rejected(client, webhook_user, app):
    event = _subscription_event("subscription.created", user_id=webhook_user)
    response = _post_event(client, event, wrong_sig=True)
    assert response.status_code == 401
    with app.app_context():
        assert Subscription.query.filter_by(user_id=webhook_user).count() == 0


def test_webhook_returns_503_when_secret_not_configured(
    client, webhook_user, monkeypatch
):
    monkeypatch.delenv("POLAR_WEBHOOK_SECRET", raising=False)
    event = _subscription_event("subscription.created", user_id=webhook_user)
    response = _post_event(client, event, sign=False)
    assert response.status_code == 503


# --------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------- #
def test_subscription_created_persists_row_and_log(
    client, webhook_user, app
):
    event = _subscription_event(
        "subscription.created", user_id=webhook_user,
        polar_sub_id="sub_polar_create_1",
    )
    response = _post_event(client, event)
    assert response.status_code == 200, response.get_data(as_text=True)
    with app.app_context():
        sub = Subscription.query.filter_by(user_id=webhook_user).one()
        assert sub.polar_subscription_id == "sub_polar_create_1"
        assert sub.tier_id == "professional"
        assert sub.tier_name == "Professional"
        assert sub.status == "active"
        assert float(sub.amount) == pytest.approx(13.00)
        assert sub.currency == "USD"
        assert sub.billing_interval == "month"
        assert SubscriptionLog.query.filter_by(
            user_id=webhook_user, event_type="webhook_created"
        ).count() == 1


def test_subscription_canceled_flips_status(client, webhook_user, app):
    # First seed an active subscription via the same path.
    create_event = _subscription_event(
        "subscription.created", user_id=webhook_user,
        polar_sub_id="sub_polar_cancel_1",
    )
    assert _post_event(client, create_event).status_code == 200

    cancel_event = _subscription_event(
        "subscription.canceled", user_id=webhook_user,
        polar_sub_id="sub_polar_cancel_1", status="canceled",
    )
    cancel_event["data"]["ends_at"] = "2026-06-03T12:00:00Z"
    response = _post_event(client, cancel_event)
    assert response.status_code == 200, response.get_data(as_text=True)

    with app.app_context():
        sub = Subscription.query.filter_by(
            polar_subscription_id="sub_polar_cancel_1"
        ).one()
        assert sub.status == "cancelled"
        assert sub.cancel_at is not None
        assert SubscriptionLog.query.filter_by(
            user_id=webhook_user, event_type="webhook_canceled"
        ).count() == 1


def test_concurrent_subscription_created_for_same_user_keeps_one_row(
    app, webhook_user, monkeypatch
):
    """Two parallel ``subscription.created`` deliveries for the same user
    must collapse to a single Subscription row.

    We can't faithfully race two threads against the same in-memory
    SQLite, so we simulate the race deterministically:

    1. Worker A wins -- a subscription row already exists in the DB.
    2. Worker B's webhook fires. Inside ``_load_or_create`` the
       ``filter_by(user_id=...)`` lookup is patched to return None just
       once, mimicking the stale read worker B would have done before A
       committed. Worker B then attempts an INSERT, which trips the new
       unique index on ``subscription.user_id`` and raises
       IntegrityError. The recovery path must re-fetch the surviving row
       and finish the upsert without exploding -- and we must end with
       exactly one Subscription row for that user.
    """
    from flask_sqlalchemy.query import Query as FSAQuery
    from polar import routes as polar_routes

    with app.app_context():
        winner = Subscription(
            user_id=webhook_user,
            polar_subscription_id="sub_polar_winner",
            tier_id="professional", tier_name="Professional",
            status="active", amount=13, currency="USD",
            billing_interval="month",
        )
        db.session.add(winner)
        db.session.commit()

    real_filter_by = FSAQuery.filter_by
    state = {"stale_done": False}

    def patched_filter_by(self, **kwargs):
        # Mimic the stale read exactly once: the first user_id-only
        # lookup against the Subscription table sees nothing, forcing
        # _load_or_create to attempt an INSERT that races into the
        # unique index. Subsequent lookups (including the post-rollback
        # re-fetch in the IntegrityError handler) hit the real DB.
        if not state["stale_done"] and list(kwargs.keys()) == ["user_id"]:
            try:
                entity = self.column_descriptions[0]["entity"]
            except (AttributeError, IndexError, KeyError):
                entity = None
            if entity is Subscription:
                state["stale_done"] = True
                # Return a query that is guaranteed to match no rows.
                return real_filter_by(self, user_id=-1)
        return real_filter_by(self, **kwargs)

    monkeypatch.setattr(FSAQuery, "filter_by", patched_filter_by)

    with app.app_context():
        polar_routes._process_subscription_upsert(
            data={
                "id": "sub_polar_loser",
                "status": "active",
                "amount": 1300,
                "currency": "USD",
                "recurring_interval": "month",
                "started_at": "2026-05-03T12:00:00Z",
                "current_period_end": "2026-06-03T12:00:00Z",
                "product": {"name": "Professional"},
                "metadata": {
                    "user_id": str(webhook_user),
                    "tier_id": "professional",
                    "billing_interval": "monthly",
                },
            },
            log_event="webhook_created",
        )

    assert state["stale_done"], (
        "Test bug: stale-read patch never fired, so the race wasn't "
        "actually simulated."
    )
    with app.app_context():
        rows = Subscription.query.filter_by(user_id=webhook_user).all()
        assert len(rows) == 1, (
            f"Expected exactly one Subscription per user; got {len(rows)}: "
            f"{[r.polar_subscription_id for r in rows]}"
        )
        # The IntegrityError recovery should claim the surviving row for
        # the most-recent webhook's polar_subscription_id so subsequent
        # webhooks for that id keep landing on the right row.
        assert rows[0].polar_subscription_id == "sub_polar_loser"


def test_unhandled_event_type_returns_200_without_dbwrite(
    client, webhook_user, app
):
    event = {
        "type": "customer.created",
        "data": {"id": "cust_x", "metadata": {"user_id": str(webhook_user)}},
    }
    response = _post_event(client, event)
    assert response.status_code == 200
    with app.app_context():
        assert Subscription.query.filter_by(user_id=webhook_user).count() == 0
