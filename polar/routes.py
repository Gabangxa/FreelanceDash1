"""
Routes for the Polar.sh subscription integration.

The blueprint registers under ``/subscriptions``. The webhook endpoint
``/subscriptions/webhook`` is CSRF-exempted in :mod:`app` since Polar
cannot carry a Flask-WTF token; instead, every webhook is HMAC-verified
against ``POLAR_WEBHOOK_SECRET`` via
:func:`polar.polar_api.verify_webhook_signature`.
"""
import logging
import os
from datetime import datetime
from typing import Optional

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import db
from errors import handle_db_errors
from models import User

from .models import Subscription, SubscriptionLog
from .polar_api import (
    PolarAPIError, WebhookVerificationError, get_polar_api, get_webhook_url,
    is_polar_api_configured, verify_webhook_signature,
)

logger = logging.getLogger(__name__)
bp = Blueprint("subscriptions", __name__, url_prefix="/subscriptions")


# Tier ID used inside the app + Subscription.tier_id column.
PROFESSIONAL_TIER_ID = "professional"


def _professional_price_id(billing: str) -> Optional[str]:
    """Return the Polar product_price_id for the given billing cadence."""
    if billing == "annual":
        return os.environ.get("POLAR_PROFESSIONAL_YEARLY_PRICE_ID")
    return os.environ.get("POLAR_PROFESSIONAL_MONTHLY_PRICE_ID")


def _build_tier_catalog() -> list:
    """Static, in-process pricing catalog rendered on the subscription page.

    Sourced from env so support can change pricing without a code deploy
    once we add an admin UI; for now the figures are hardcoded to match
    the agreed Free + Professional structure.
    """
    return [
        {
            "id": "free",
            "name": "Free",
            "description": "Solo essentials to get your freelance work off the ground.",
            "price_monthly": 0,
            "price_annually": 0,
            "features": [
                "Up to 3 clients",
                "Up to 5 projects",
                "Basic time tracking",
                "Standard invoicing",
            ],
        },
        {
            "id": PROFESSIONAL_TIER_ID,
            "name": "Professional",
            "description": "Everything you need to run your freelance business without limits.",
            "price_monthly": 13,
            "price_annually": 130,
            "features": [
                "Unlimited clients",
                "Unlimited projects",
                "Advanced time tracking with reporting",
                "All invoice templates",
                "Custom branding",
                "Email support",
            ],
        },
    ]


@bp.route("/")
@login_required
def index():
    """Show subscription status and the Free / Professional pricing cards."""
    api_configured = is_polar_api_configured()
    if not api_configured:
        logger.warning("Polar API not configured (POLAR_API_KEY missing)")
        flash(
            "Subscription service is being set up. Please check back soon.",
            "warning",
        )

    subscription = Subscription.query.filter_by(
        user_id=current_user.id
    ).first()

    return render_template(
        "polar/subscription.html",
        subscription=subscription,
        subscription_tiers=_build_tier_catalog(),
        api_configured=api_configured,
    )


@bp.route("/checkout/<tier_id>")
@login_required
def checkout(tier_id):
    """Redirect the user to Polar's hosted checkout for the given tier.

    Query param ``billing=monthly|annual`` selects the price; defaults to
    monthly. The current user's id is stamped onto the checkout's
    ``metadata`` so the resulting subscription comes back over the
    webhook with the SoloDolo user we should attach it to.
    """
    if tier_id != PROFESSIONAL_TIER_ID:
        flash("Invalid subscription tier.", "danger")
        return redirect(url_for("subscriptions.index"))

    if not is_polar_api_configured():
        logger.error("Checkout attempted but POLAR_API_KEY is not set")
        flash(
            "Subscription service is not yet configured. Please contact support.",
            "danger",
        )
        return redirect(url_for("subscriptions.index"))

    billing = request.args.get("billing", "monthly")
    if billing not in ("monthly", "annual"):
        billing = "monthly"

    price_id = _professional_price_id(billing)
    if not price_id:
        logger.error(
            "Polar price ID env var missing for billing=%s tier=%s",
            billing, tier_id,
        )
        flash(
            "Subscription pricing is not yet configured. Please contact support.",
            "danger",
        )
        return redirect(url_for("subscriptions.index"))

    success_url = url_for(
        "subscriptions.checkout_success", _external=True,
    )

    try:
        polar = get_polar_api()
        checkout_obj = polar.create_checkout(
            product_price_id=price_id,
            success_url=success_url,
            customer_email=current_user.email,
            metadata={
                "user_id": current_user.id,
                "tier_id": tier_id,
                "billing_interval": billing,
            },
        )
    except PolarAPIError:
        logger.exception("Polar checkout creation failed")
        flash(
            "Unable to start checkout right now. Please try again in a moment.",
            "danger",
        )
        return redirect(url_for("subscriptions.index"))

    checkout_url = checkout_obj.get("url")
    if not checkout_url:
        logger.error(
            "Polar checkout response missing url field: %s", checkout_obj
        )
        flash("Checkout could not be started. Please try again.", "danger")
        return redirect(url_for("subscriptions.index"))

    session["polar_checkout_id"] = checkout_obj.get("id")
    return redirect(checkout_url)


@bp.route("/checkout/success")
@login_required
def checkout_success():
    """Landing page after a successful Polar checkout.

    The Subscription row is created lazily by the webhook handler (which
    is the source of truth). We just clear the in-session checkout id
    and show a success flash; the subscription page will reflect the
    active plan as soon as Polar's webhook lands -- usually within a
    second or two of the redirect.
    """
    session.pop("polar_checkout_id", None)
    flash(
        "Thanks for subscribing! Your plan will activate momentarily.",
        "success",
    )
    return redirect(url_for("subscriptions.index"))


@bp.route("/webhook-url")
@login_required
def webhook_url():
    """Show the URL admins should paste into Polar's webhook config."""
    return render_template(
        "polar/webhook_url.html",
        webhook_url=get_webhook_url(),
        current_url=request.url_root,
    )


# ---------------------------------------------------------------------- #
# Webhook
# ---------------------------------------------------------------------- #
@bp.route("/webhook", methods=["POST"])
@handle_db_errors
def webhook():
    """Receive subscription events from Polar.

    Verifies the request signature against ``POLAR_WEBHOOK_SECRET``
    (standard-webhooks spec) before touching the database. Unsigned or
    badly-signed POSTs return 401 and never write anything.
    """
    secret = os.environ.get("POLAR_WEBHOOK_SECRET")
    if not secret:
        logger.error("Webhook hit but POLAR_WEBHOOK_SECRET is not set")
        return jsonify({"error": "webhook not configured"}), 503

    raw_body = request.get_data(cache=True)
    try:
        verify_webhook_signature(
            payload=raw_body,
            headers=request.headers,
            secret=secret,
        )
    except WebhookVerificationError as exc:
        logger.warning("Polar webhook signature verification failed: %s", exc)
        return jsonify({"error": "invalid signature"}), 401

    try:
        event = request.get_json(force=True, silent=False) or {}
    except Exception:  # noqa: BLE001 -- get_json may raise BadRequest
        logger.warning("Polar webhook had unparseable JSON body")
        return jsonify({"error": "invalid json"}), 400

    event_type = event.get("type")
    data = event.get("data") or {}
    logger.info("Polar webhook event: type=%s", event_type)

    if event_type == "subscription.created":
        _process_subscription_upsert(data, log_event="webhook_created")
    elif event_type in ("subscription.updated", "subscription.active",
                        "subscription.uncanceled"):
        _process_subscription_upsert(data, log_event=f"webhook_{event_type.split('.')[1]}")
    elif event_type in ("subscription.canceled", "subscription.cancelled",
                        "subscription.revoked"):
        _process_subscription_cancellation(data, event_type=event_type)
    else:
        logger.info("Unhandled Polar webhook event type: %s", event_type)

    return jsonify({"status": "ok"}), 200


def _user_id_from_metadata(data: dict) -> Optional[int]:
    """Pull the SoloDolo user_id we stamped onto the Polar checkout metadata."""
    metadata = data.get("metadata") or {}
    raw = metadata.get("user_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.error("Polar webhook metadata.user_id is not an int: %r", raw)
        return None


def _tier_name_from_data(data: dict) -> str:
    """Best-effort human tier name; falls back to 'Professional'."""
    product = data.get("product") or {}
    name = product.get("name")
    if name:
        return name
    return "Professional"


def _amount_from_data(data: dict) -> tuple:
    """Extract (amount_in_dollars, currency) from a Polar subscription dict.

    Polar reports amount as integer minor units (cents) on the subscription
    object; we store dollars in a Numeric column.
    """
    cents = data.get("amount")
    currency = (data.get("currency") or "USD").upper()
    if cents is None:
        return (None, currency)
    try:
        return (int(cents) / 100.0, currency)
    except (TypeError, ValueError):
        return (None, currency)


def _interval_from_data(data: dict) -> str:
    interval = data.get("recurring_interval") or "month"
    if interval in ("year", "yearly", "annual"):
        return "year"
    return "month"


def _parse_polar_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Polar emits RFC 3339 / ISO 8601 with trailing Z.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Unparseable Polar timestamp: %r", value)
        return None


def _process_subscription_upsert(data: dict, *, log_event: str) -> None:
    """Create or update a Subscription row from a Polar subscription payload."""
    polar_subscription_id = data.get("id")
    user_id = _user_id_from_metadata(data)

    if not polar_subscription_id or not user_id:
        logger.error(
            "Polar webhook missing id or metadata.user_id (id=%s, user_id=%s)",
            polar_subscription_id, user_id,
        )
        return

    user = User.query.get(user_id)
    if not user:
        logger.error("Polar webhook references unknown user_id=%s", user_id)
        return

    amount, currency = _amount_from_data(data)
    interval = _interval_from_data(data)
    tier_name = _tier_name_from_data(data)
    start_date = _parse_polar_datetime(
        data.get("started_at") or data.get("current_period_start")
    ) or datetime.utcnow()
    end_date = _parse_polar_datetime(data.get("current_period_end"))
    metadata = data.get("metadata") or {}
    tier_id = str(metadata.get("tier_id") or PROFESSIONAL_TIER_ID).lower()

    def _load_or_create():
        sub = (
            Subscription.query
            .filter_by(polar_subscription_id=polar_subscription_id)
            .first()
        )
        if sub is None:
            # Fall back to the user's existing row (e.g. they had a
            # superseded sub) so we don't duplicate on plan change.
            sub = Subscription.query.filter_by(user_id=user_id).first()
        if sub is None:
            sub = Subscription(user_id=user_id)
            db.session.add(sub)
        return sub

    try:
        subscription = _load_or_create()
        subscription.user_id = user_id
        subscription.polar_subscription_id = polar_subscription_id
        subscription.tier_id = tier_id
        subscription.tier_name = tier_name
        subscription.status = data.get("status") or "active"
        if amount is not None:
            subscription.amount = amount
        else:
            # Numeric column is NOT NULL; fall back to 0 so we never crash
            # on a malformed payload, but log loudly.
            if subscription.amount is None:
                logger.error(
                    "Polar payload had no amount; defaulting to 0 (sub %s)",
                    polar_subscription_id,
                )
                subscription.amount = 0
        subscription.currency = currency
        subscription.billing_interval = interval
        subscription.start_date = start_date
        subscription.end_date = end_date

        db.session.flush()  # populate subscription.id for the log row

        log_row = SubscriptionLog(
            user_id=user_id,
            subscription_id=subscription.id,
            event_type=log_event,
            details=data,
        )
        db.session.add(log_row)
        db.session.commit()
        logger.info(
            "Upserted Polar subscription %s for user %s (event=%s)",
            polar_subscription_id, user_id, log_event,
        )
    except IntegrityError:
        # Race: a sibling worker inserted the same polar_subscription_id
        # between our SELECT and our INSERT. Roll back, re-fetch, and
        # retry the field updates onto the row that won the race. Polar
        # retries failed webhooks, so this would otherwise eventually
        # 500-loop on us in production.
        db.session.rollback()
        logger.warning(
            "IntegrityError upserting Polar sub %s; retrying as update",
            polar_subscription_id,
        )
        existing = Subscription.query.filter_by(
            polar_subscription_id=polar_subscription_id
        ).first()
        if existing is None:
            logger.exception(
                "Polar sub %s integrity error but no winning row found",
                polar_subscription_id,
            )
            raise
        existing.status = data.get("status") or existing.status
        if amount is not None:
            existing.amount = amount
        existing.currency = currency
        existing.billing_interval = interval
        existing.tier_name = tier_name
        existing.tier_id = tier_id
        existing.end_date = end_date
        db.session.add(SubscriptionLog(
            user_id=user_id,
            subscription_id=existing.id,
            event_type=log_event,
            details=data,
        ))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("DB error processing Polar subscription upsert")
        raise


def _process_subscription_cancellation(data: dict, *, event_type: str) -> None:
    polar_subscription_id = data.get("id")
    user_id = _user_id_from_metadata(data)

    if not polar_subscription_id:
        logger.error("Polar cancellation webhook missing subscription id")
        return

    try:
        subscription = (
            Subscription.query
            .filter_by(polar_subscription_id=polar_subscription_id)
            .first()
        )
        if not subscription:
            logger.warning(
                "Polar cancellation webhook for unknown subscription %s",
                polar_subscription_id,
            )
            return

        subscription.status = "cancelled"
        subscription.cancel_at = (
            _parse_polar_datetime(data.get("ends_at"))
            or _parse_polar_datetime(data.get("current_period_end"))
            or datetime.utcnow()
        )
        log_row = SubscriptionLog(
            user_id=user_id or subscription.user_id,
            subscription_id=subscription.id,
            event_type=f"webhook_{event_type.split('.')[1]}",
            details=data,
        )
        db.session.add(log_row)
        db.session.commit()
        logger.info(
            "Cancelled Polar subscription %s (event=%s)",
            polar_subscription_id, event_type,
        )
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("DB error processing Polar cancellation")
        raise


# ---------------------------------------------------------------------- #
# Manual cancel from the in-app "Cancel Subscription" button
# ---------------------------------------------------------------------- #
@bp.route("/cancel", methods=["POST"])
@login_required
@handle_db_errors
def cancel_subscription():
    subscription = Subscription.query.filter_by(
        user_id=current_user.id
    ).first()
    if not subscription or subscription.status != "active":
        flash("No active subscription to cancel.", "warning")
        return redirect(url_for("subscriptions.index"))

    if not is_polar_api_configured():
        flash(
            "Subscription service is not configured. Please contact support.",
            "danger",
        )
        return redirect(url_for("subscriptions.index"))

    try:
        polar = get_polar_api()
        result = polar.cancel_subscription(subscription.polar_subscription_id)
    except PolarAPIError:
        logger.exception("Polar cancel API call failed")
        flash(
            "Unable to cancel subscription right now. Please try again later.",
            "danger",
        )
        return redirect(url_for("subscriptions.index"))

    subscription.status = "cancelled"
    subscription.cancel_at = (
        _parse_polar_datetime(result.get("ends_at"))
        or _parse_polar_datetime(result.get("current_period_end"))
        or datetime.utcnow()
    )
    db.session.add(SubscriptionLog(
        user_id=current_user.id,
        subscription_id=subscription.id,
        event_type="cancelled",
        details=result,
    ))
    db.session.commit()
    flash(
        "Your subscription will end at the close of the current billing period.",
        "success",
    )
    return redirect(url_for("subscriptions.index"))
