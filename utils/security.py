"""
Security utility helpers.

These helpers are intentionally side-effect free and importable from anywhere
in the application. Keep them small, well-tested, and dependency-light so they
can be exercised in unit tests without a Flask app context.
"""
from urllib.parse import urlparse, urljoin

from flask import request


_ALLOWED_SCHEMES = {"http", "https"}


def is_safe_url(target, host_url=None):
    """Return True iff ``target`` is a safe URL to redirect a user to.

    "Safe" means:

    * The target is a non-empty string.
    * It does not start with characters that browsers may treat as a
      protocol-relative or backslash-host reference (``//``, ``\\``).
    * After being joined onto the application's host URL it has an http(s)
      scheme.
    * Its netloc matches the application's own netloc.

    Crucially this rejects ``javascript:``, ``data:`` and ``vbscript:`` URLs
    even though they parse with an empty netloc, which is the loophole the
    naive ``urlparse(target).netloc != ''`` check leaves open.

    Parameters
    ----------
    target:
        The candidate URL, typically the value of a ``?next=`` query argument.
    host_url:
        Optional override for the application's host URL. When omitted, the
        current Flask request's ``host_url`` is used. Allowing it to be passed
        in keeps this helper unit-testable without a request context.
    """
    if not target or not isinstance(target, str):
        return False

    # Browsers/clients may interpret protocol-relative or backslash-prefixed
    # URLs as foreign hosts even though urlparse treats them as path-only.
    stripped = target.strip()
    if not stripped:
        return False
    if stripped.startswith("//") or stripped.startswith("\\") or stripped.startswith("/\\"):
        return False

    if host_url is None:
        # Outside of a request context (e.g. background thread, unit test
        # without a test_request_context) there's nothing to compare
        # against. Fail safe.
        try:
            host_url = request.host_url
        except RuntimeError:
            return False

    ref_url = urlparse(host_url)
    test_url = urlparse(urljoin(host_url, target))

    if test_url.scheme and test_url.scheme.lower() not in _ALLOWED_SCHEMES:
        return False

    # After urljoin, a relative path inherits the host's scheme/netloc, so
    # legitimate relative paths like "/projects" pass this check.
    return ref_url.netloc == test_url.netloc
