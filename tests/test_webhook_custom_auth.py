"""
Security regression tests for the generic webhook ingest (FD-003).

Before the fix, WEBHOOK_*_SECRET was never loaded, so signature
verification was silently skipped for every source, and the 'custom'
source -- which has no IP allowlist -- was therefore a fully
unauthenticated endpoint: anyone could POST a payload naming any
``user_id`` and inject a notification into that account.

These assert that:
  * an unsigned 'custom' webhook is rejected and injects nothing, and
  * a correctly HMAC-signed 'custom' webhook still works (so the fix
    closes the hole without breaking legitimate authenticated use).
"""
import hashlib
import hmac
import json
import uuid

import pytest

from app import db
from models import Notification, User


def _make_user(suffix):
    user = User(username=f"wh_{suffix}", email=f"wh_{suffix}@example.com")
    user.set_password("testpassword123")
    db.session.add(user)
    db.session.commit()
    return user


def _sign(secret, body):
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def test_unsigned_custom_webhook_is_rejected(client, app, db_session, monkeypatch):
    monkeypatch.delenv("WEBHOOK_CUSTOM_SECRET", raising=False)
    suffix = uuid.uuid4().hex[:8]
    victim = _make_user(suffix)

    body = json.dumps({
        "user_id": victim.id,
        "title": "pwn",
        "message": "<img src=x onerror=alert(1)>",
    })
    resp = client.post(
        "/webhooks/receive/custom",
        data=body,
        content_type="application/json",
    )

    assert resp.status_code == 401, resp.data[:400]
    assert Notification.query.filter_by(user_id=victim.id).count() == 0


def test_custom_webhook_with_wrong_signature_is_rejected(client, app, db_session, monkeypatch):
    monkeypatch.setenv("WEBHOOK_CUSTOM_SECRET", "s3cret-value")
    suffix = uuid.uuid4().hex[:8]
    victim = _make_user(suffix)

    body = json.dumps({"user_id": victim.id, "title": "x", "message": "y"})
    resp = client.post(
        "/webhooks/receive/custom",
        data=body,
        content_type="application/json",
        headers={"X-Signature": "sha256=" + "0" * 64},
    )

    assert resp.status_code == 401, resp.data[:400]
    assert Notification.query.filter_by(user_id=victim.id).count() == 0


def test_signed_custom_webhook_is_accepted(client, app, db_session, monkeypatch):
    secret = "s3cret-value"
    monkeypatch.setenv("WEBHOOK_CUSTOM_SECRET", secret)
    suffix = uuid.uuid4().hex[:8]
    target = _make_user(suffix)

    body = json.dumps({
        "user_id": target.id,
        "title": "Deploy finished",
        "message": "Build 123 shipped",
    })
    resp = client.post(
        "/webhooks/receive/custom",
        data=body,
        content_type="application/json",
        headers={"X-Signature": _sign(secret, body)},
    )

    assert resp.status_code == 200, resp.data[:400]
    # The authenticated payload is delivered to the named user.
    assert Notification.query.filter_by(user_id=target.id).count() == 1
