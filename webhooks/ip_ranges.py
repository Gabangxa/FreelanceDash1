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

Cache payload shape
-------------------
The cached value is a JSON object so the admin status panel can surface
when the list was last refreshed and whether it came from upstream or
the static fallback::

    {
        "ranges":    ["140.82.112.0/20", ...],
        "fetched_at": "2026-05-02T12:34:56Z",   # ISO-8601 UTC
        "origin":    "upstream" | "fallback"
    }

A bare list is also tolerated as a legacy/forward-compat shape (older
caches, hand-primed test fixtures) and is treated as an unknown-origin
entry so reads never raise just because the metadata is missing.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

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

ORIGIN_UPSTREAM = "upstream"
ORIGIN_FALLBACK = "fallback"
ORIGIN_UNKNOWN = "unknown"


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


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _build_payload(ranges: List[str], origin: str) -> str:
    return json.dumps(
        {
            "ranges": list(ranges),
            "fetched_at": _utc_now_iso(),
            "origin": origin,
        }
    )


def _parse_cached(raw: str) -> Optional[Dict[str, Any]]:
    """Parse a cached payload into a normalised dict.

    Returns ``None`` for malformed/empty payloads so callers refetch.
    Tolerates the legacy bare-list shape so a cache primed before the
    metadata was added (or by a hand-rolled test fixture) still serves
    the ranges without crashing.
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if isinstance(value, dict):
        ranges = value.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            return None
        return {
            "ranges": [str(v) for v in ranges],
            "fetched_at": value.get("fetched_at"),
            "origin": value.get("origin") or ORIGIN_UNKNOWN,
        }

    if isinstance(value, list) and value:
        return {
            "ranges": [str(v) for v in value],
            "fetched_at": None,
            "origin": ORIGIN_UNKNOWN,
        }

    return None


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
            parsed = _parse_cached(cached)
            if parsed is not None:
                return list(parsed["ranges"])
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
                    _cache_key(source),
                    _build_payload(fresh, ORIGIN_UPSTREAM),
                    CACHE_TTL_SECONDS,
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
                _build_payload(fallback, ORIGIN_FALLBACK),
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
            _cache_key(source),
            _build_payload(fresh, ORIGIN_UPSTREAM),
            CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to write cached IP ranges for %s: %s", source, exc
        )
    return True


def get_status(source: str) -> Dict[str, Any]:
    """Return a snapshot of the current allowlist state for ``source``.

    Used by the admin ``/webhooks/security/status`` endpoint so operators
    can tell at a glance whether the dynamic refresh is healthy without
    having to grep server logs.

    The returned dict is JSON-serialisable and shaped as::

        {
            "source":       "github",
            "range_count":  N,
            "fetched_at":   "<iso>" | None,
            "origin":       "upstream" | "fallback" | "unknown",
            "cached":       True | False,
        }

    ``cached=False`` means the storage backend has no entry yet (e.g. the
    boot-time refresh hasn't run yet, or the entry has been evicted) and
    the values shown reflect the static fallback list. ``cached=True``
    means the values were read from the shared storage backend.

    Reading status must never raise: if storage itself is unavailable we
    still return a best-effort snapshot derived from the static fallback
    so the admin panel keeps rendering during a Redis/DB outage.
    """
    if source not in FALLBACK_RANGES:
        return {
            "source": source,
            "range_count": 0,
            "fetched_at": None,
            "origin": ORIGIN_UNKNOWN,
            "cached": False,
        }

    fallback = FALLBACK_RANGES[source]
    try:
        cached = get_storage().cache_get(_cache_key(source))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Webhook storage unavailable when reading IP range status "
            "for %s: %s",
            source, exc,
        )
        cached = None

    if cached:
        parsed = _parse_cached(cached)
        if parsed is not None:
            return {
                "source": source,
                "range_count": len(parsed["ranges"]),
                "fetched_at": parsed["fetched_at"],
                "origin": parsed["origin"],
                "cached": True,
            }

    return {
        "source": source,
        "range_count": len(fallback),
        "fetched_at": None,
        "origin": ORIGIN_FALLBACK,
        "cached": False,
    }


def all_statuses() -> List[Dict[str, Any]]:
    """Return ``get_status`` for every known source, in stable order."""
    return [get_status(source) for source in sorted(FALLBACK_RANGES.keys())]
