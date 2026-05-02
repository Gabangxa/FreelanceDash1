"""
Dynamic IP allowlist for webhook sources.

GitHub and Stripe both publish their webhook IP ranges and rotate them
periodically. Hard-coding the lists in source means the app silently
starts rejecting legitimate webhooks when an upstream rotates an IP.

This module fetches and caches the lists in the shared webhook storage
backend (Redis or DB) with a 6h TTL. If the upstream fetch fails we fall
back to the previously hard-coded ranges so a transient network blip
doesn't cause an outage.

Important: ``get_ranges`` consults the cache first and only fetches when
the cache is empty/expired. A normal webhook request must NOT trigger an
outbound HTTP call.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Dict, List, Optional

import requests

from webhooks.storage import get_storage

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

# Short backoff TTL used when upstream is unreachable. We cache the static
# fallback list under this TTL so repeated webhook requests during an
# outage read from cache instead of re-hitting upstream on every request.
# After the backoff expires we'll retry the upstream once.
FALLBACK_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes

# Static fallback IP ranges. Used both as the safety-net when the upstream
# fetch fails and as the "shape" of which sources we know how to refresh.
FALLBACK_RANGES: Dict[str, List[str]] = {
    "github": [
        "140.82.112.0/20",
        "185.199.108.0/22",
        "192.30.252.0/22",
        "143.55.64.0/20",
    ],
    "stripe": [
        "54.187.174.169/32",
        "54.187.205.235/32",
        "54.187.216.72/32",
        "54.241.31.99/32",
        "54.241.31.102/32",
        "54.241.34.107/32",
    ],
}

GITHUB_META_URL = "https://api.github.com/meta"
STRIPE_IPS_URL = "https://stripe.com/files/ips/ips_webhooks.json"
HTTP_TIMEOUT_SECONDS = 5


def _fetch_github() -> Optional[List[str]]:
    resp = requests.get(GITHUB_META_URL, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, list) and hooks:
        return [str(h) for h in hooks]
    return None


def _fetch_stripe() -> Optional[List[str]]:
    resp = requests.get(STRIPE_IPS_URL, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return None
    # Stripe publishes the list under the WEBHOOKS key; tolerate the
    # lower-cased variant just in case.
    raw = data.get("WEBHOOKS") or data.get("webhooks")
    if isinstance(raw, list) and raw:
        # Stripe lists bare addresses, normalise to /32 so the downstream
        # ipaddress.ip_network() call accepts them uniformly.
        normalised: List[str] = []
        for ip in raw:
            ip_s = str(ip)
            if "/" not in ip_s:
                ip_s = ip_s + "/32"
            normalised.append(ip_s)
        return normalised
    return None


_FETCHERS: Dict[str, Callable[[], Optional[List[str]]]] = {
    "github": _fetch_github,
    "stripe": _fetch_stripe,
}


def _cache_key(source: str) -> str:
    return f"ip_ranges:{source}"


def get_ranges(source: str) -> List[str]:
    """Return IP ranges for ``source``, refreshing from upstream if the
    cache is empty/expired. Falls back to the hard-coded list on any
    failure so we never start rejecting legitimate traffic on a blip.
    """
    if source not in FALLBACK_RANGES:
        # Unknown source: caller's existing "no allowlist => allow all"
        # behaviour stays in WebhookSecurity.validate_ip_allowlist.
        return []

    storage = None
    try:
        storage = get_storage()
        cached = storage.cache_get(_cache_key(source))
        if cached:
            try:
                value = json.loads(cached)
                if isinstance(value, list) and value:
                    return [str(v) for v in value]
            except (ValueError, TypeError):
                logger.warning(
                    "Corrupt cached IP ranges for %s; refetching", source
                )
    except Exception as exc:  # pragma: no cover - defensive
        # Storage outage shouldn't take the webhook receiver offline; we
        # still try to refresh and ultimately fall back to the static list.
        logger.warning(
            "Webhook storage unavailable when reading IP ranges for %s: %s",
            source, exc,
        )

    fresh = _try_fetch(source)
    if fresh:
        if storage is not None:
            try:
                storage.cache_set(
                    _cache_key(source), json.dumps(fresh), CACHE_TTL_SECONDS
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to cache IP ranges for %s: %s", source, exc
                )
        return fresh

    logger.warning(
        "Falling back to static hard-coded IP ranges for %s "
        "(upstream fetch failed or returned empty list)", source,
    )
    fallback = list(FALLBACK_RANGES[source])
    # Cache the fallback under a short TTL so we don't re-hit upstream on
    # every webhook request during an outage. The next get_ranges() call
    # within the backoff window will read this cached fallback and skip
    # the HTTP call entirely; once the TTL expires we'll retry upstream.
    if storage is not None:
        try:
            storage.cache_set(
                _cache_key(source),
                json.dumps(fallback),
                FALLBACK_CACHE_TTL_SECONDS,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to cache fallback IP ranges for %s: %s", source, exc
            )
    return fallback


def _try_fetch(source: str) -> Optional[List[str]]:
    fetcher = _FETCHERS.get(source)
    if fetcher is None:
        return None
    try:
        return fetcher()
    except Exception as exc:
        logger.warning(
            "Failed to fetch upstream IP ranges for %s: %s", source, exc
        )
        return None


def refresh_now(source: str) -> bool:
    """Force an upstream refresh and prime the cache. Returns True iff the
    upstream fetch succeeded. Used by the startup health log so operators
    can see at a glance whether GitHub/Stripe are reachable on boot.
    """
    fresh = _try_fetch(source)
    if not fresh:
        return False
    try:
        get_storage().cache_set(
            _cache_key(source), json.dumps(fresh), CACHE_TTL_SECONDS
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to write cached IP ranges for %s: %s", source, exc
        )
    return True
