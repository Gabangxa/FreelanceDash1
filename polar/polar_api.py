"""
Polar.sh API client for SoloDolo.

Targets the real production Polar v1 API at ``https://api.polar.sh``.
The previous version of this file used guessed endpoint names
(``subscription/tiers``, ``checkout/session``, ``subscriptions/{id}/cancel``,
``subscriptions/{id}/upgrade``) that do not exist in Polar's real API
surface -- those have been removed. The supported operations are:

* ``create_checkout``   -- POST /v1/checkouts/
* ``get_checkout``      -- GET  /v1/checkouts/{id}
* ``get_subscription``  -- GET  /v1/subscriptions/{id}
* ``cancel_subscription`` -- PATCH /v1/subscriptions/{id}
                            (cancel_at_period_end=true; graceful cancel)

Webhook signature verification (standard-webhooks spec) lives in
:func:`verify_webhook_signature`.
"""
import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)


BASE_URL = "https://api.polar.sh/"
DEFAULT_TIMEOUT = 10  # seconds


class PolarAPIError(Exception):
    """Raised for any error talking to Polar (network, HTTP, parsing)."""


class PolarAPI:
    """Thin wrapper over Polar's v1 REST API."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("POLAR_API_KEY")
        if not self.api_key:
            raise PolarAPIError("POLAR_API_KEY is not configured")

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SoloDolo/1.0 (+https://solodolo.xyz)",
        })

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Optional[Mapping[str, Any]] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        url = urljoin(BASE_URL, endpoint.lstrip("/"))
        logger.info("Polar API %s %s", method.upper(), endpoint)
        try:
            response = self.session.request(
                method, url, json=json, timeout=timeout
            )
        except requests.exceptions.Timeout as exc:
            logger.error("Polar API timeout: %s %s", method.upper(), endpoint)
            raise PolarAPIError("Polar API request timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error(
                "Polar API connection error: %s %s", method.upper(), endpoint
            )
            raise PolarAPIError("Could not reach Polar API") from exc

        if not response.ok:
            # Try to surface Polar's error body for debuggability, without
            # leaking the request payload (which may contain customer email).
            body_excerpt = response.text[:500] if response.text else ""
            logger.error(
                "Polar API error %s on %s %s: %s",
                response.status_code, method.upper(), endpoint, body_excerpt,
            )
            if response.status_code in (401, 403):
                raise PolarAPIError(
                    "Polar API rejected our credentials. Check POLAR_API_KEY."
                )
            raise PolarAPIError(
                f"Polar API returned HTTP {response.status_code}"
            )

        # Some endpoints (DELETE, etc.) return empty bodies on success.
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PolarAPIError("Polar API returned non-JSON body") from exc

    # ------------------------------------------------------------------ #
    # checkouts
    # ------------------------------------------------------------------ #
    def create_checkout(
        self,
        *,
        product_price_id: str,
        success_url: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a Polar hosted-checkout session.

        Returns the raw checkout object; the caller redirects the user to
        ``response["url"]``. ``metadata`` is round-tripped on the resulting
        Subscription object, which is how we recover the SoloDolo user_id
        when webhooks come back.
        """
        body: Dict[str, Any] = {
            "product_price_id": product_price_id,
            "success_url": success_url,
        }
        if customer_email:
            body["customer_email"] = customer_email
        if metadata:
            # Polar requires metadata values to be strings.
            body["metadata"] = {k: str(v) for k, v in metadata.items()}
        return self._request("post", "/v1/checkouts/", json=body)

    def get_checkout(self, checkout_id: str) -> Dict[str, Any]:
        return self._request("get", f"/v1/checkouts/{checkout_id}")

    # ------------------------------------------------------------------ #
    # subscriptions
    # ------------------------------------------------------------------ #
    def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return self._request("get", f"/v1/subscriptions/{subscription_id}")

    def cancel_subscription(self, subscription_id: str) -> Dict[str, Any]:
        """Cancel at the end of the current billing period (graceful)."""
        return self._request(
            "patch",
            f"/v1/subscriptions/{subscription_id}",
            json={"cancel_at_period_end": True},
        )


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #
_polar_api_instance: Optional[PolarAPI] = None


def get_polar_api() -> PolarAPI:
    """Return a process-wide PolarAPI client, creating it on first use."""
    global _polar_api_instance
    if _polar_api_instance is None:
        _polar_api_instance = PolarAPI()
    return _polar_api_instance


def reset_polar_api_for_tests() -> None:
    """Forget the cached client. Used by tests that monkeypatch the env."""
    global _polar_api_instance
    _polar_api_instance = None


def is_polar_api_configured() -> bool:
    """True if POLAR_API_KEY is set in the environment."""
    return bool(os.environ.get("POLAR_API_KEY"))


def get_webhook_url() -> str:
    """Build the absolute URL Polar should POST webhooks to."""
    from flask import url_for
    try:
        return url_for("subscriptions.webhook", _external=True)
    except RuntimeError:
        # Outside of a request context (CLI / docs page during boot test).
        return "https://solodolo.xyz/subscriptions/webhook"


# ---------------------------------------------------------------------- #
# Webhook signature verification (standard-webhooks spec)
# ---------------------------------------------------------------------- #
# Polar publishes webhooks per https://www.standardwebhooks.com/. The signing
# secret is delivered as ``whsec_<base64-encoded-key>``. Each request carries:
#
#   webhook-id        -- unique ID per delivery
#   webhook-timestamp -- unix seconds of when Polar sent the event
#   webhook-signature -- "v1,<base64(hmac_sha256(signed_payload))>"
#                        (multiple sigs separated by spaces during key rotation)
#
# signed_payload = f"{webhook_id}.{webhook_timestamp}.{raw_body}"

WEBHOOK_TOLERANCE_SECONDS = 5 * 60  # reject events older/newer than 5 min


class WebhookVerificationError(Exception):
    """Raised when a webhook signature fails verification."""


def _decode_secret(secret: str) -> bytes:
    """Decode a standard-webhooks ``whsec_<base64>`` secret to raw bytes.

    Falls back to treating the secret as a raw UTF-8 string if it doesn't
    match the prefixed format -- some Polar-compatible providers (and the
    Polar dashboard's "plain string" fallback for legacy tenants) hand out
    a non-prefixed secret.
    """
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_"):])
        except (ValueError, base64.binascii.Error) as exc:
            raise WebhookVerificationError(
                "Malformed webhook secret (not valid base64 after whsec_)"
            ) from exc
    return secret.encode("utf-8")


def verify_webhook_signature(
    *,
    payload: bytes,
    headers: Mapping[str, str],
    secret: str,
    tolerance_seconds: int = WEBHOOK_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> None:
    """Validate a Polar webhook per the standard-webhooks spec.

    Raises :class:`WebhookVerificationError` on any failure and returns
    ``None`` on success. Header names are matched case-insensitively
    because Flask preserves the original casing the client sent.
    """
    # Case-insensitive header lookup.
    lower = {k.lower(): v for k, v in headers.items()}
    msg_id = lower.get("webhook-id")
    msg_ts = lower.get("webhook-timestamp")
    msg_sig = lower.get("webhook-signature")

    if not (msg_id and msg_ts and msg_sig):
        raise WebhookVerificationError(
            "Missing required webhook headers (webhook-id / -timestamp / "
            "-signature)"
        )

    # Replay protection.
    try:
        ts = int(msg_ts)
    except (TypeError, ValueError) as exc:
        raise WebhookVerificationError(
            "webhook-timestamp is not an integer"
        ) from exc

    current = int(now if now is not None else time.time())
    if abs(current - ts) > tolerance_seconds:
        raise WebhookVerificationError(
            "Webhook timestamp outside tolerance (possible replay)"
        )

    key = _decode_secret(secret)
    signed_payload = f"{msg_id}.{msg_ts}.".encode("utf-8") + payload
    expected = base64.b64encode(
        hmac.new(key, signed_payload, hashlib.sha256).digest()
    ).decode("utf-8")

    # Header may carry multiple sigs ("v1,sigA v1,sigB") during key rotation.
    candidates = []
    for token in msg_sig.split():
        if "," in token:
            version, value = token.split(",", 1)
            if version == "v1":
                candidates.append(value)
    if not candidates:
        raise WebhookVerificationError("No v1 signatures found in header")

    for candidate in candidates:
        if hmac.compare_digest(candidate, expected):
            return
    raise WebhookVerificationError("No webhook signature matched")
