"""
Tests for utils.security.is_safe_url -- regression coverage for C3
(open-redirect filter accepting javascript: and similar payloads).
"""
import pytest

from utils.security import is_safe_url


HOST = "https://app.example.com/"


@pytest.mark.parametrize("target", [
    "/projects",
    "/projects?foo=bar",
    "/auth/login",
    "https://app.example.com/dashboard",
])
def test_safe_targets_are_accepted(target):
    assert is_safe_url(target, host_url=HOST) is True


@pytest.mark.parametrize("target", [
    # Empty / invalid
    None,
    "",
    "   ",
    # External hosts
    "http://evil.com/path",
    "https://evil.com/path",
    # Protocol-relative (browsers treat as foreign host)
    "//evil.com/path",
    "  //evil.com/path",
    # Backslash-prefixed (some browsers normalize to a host)
    "\\evil.com",
    "/\\evil.com/path",
    # Dangerous schemes that have empty netloc and would have slipped past
    # the previous urlparse(...).netloc != '' check.
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
])
def test_dangerous_targets_are_rejected(target):
    assert is_safe_url(target, host_url=HOST) is False


def test_non_string_input_is_rejected():
    assert is_safe_url(12345, host_url=HOST) is False
    assert is_safe_url(["/projects"], host_url=HOST) is False


def test_returns_false_outside_request_context_when_no_host_provided():
    """Without a request context AND without an explicit host_url, the
    helper must fail safe rather than raising RuntimeError -- otherwise a
    background thread could crash on a perfectly innocuous string."""
    assert is_safe_url("/projects") is False


def test_uses_request_host_url_when_inside_request_context(app):
    """When called from inside a Flask request context the helper picks up
    request.host_url automatically."""
    with app.test_request_context("/", base_url="http://localhost.test/"):
        assert is_safe_url("/projects") is True
        assert is_safe_url("https://evil.com/x") is False
        assert is_safe_url("javascript:alert(1)") is False
