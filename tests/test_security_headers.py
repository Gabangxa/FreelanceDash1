"""
Permissions-Policy header syntax.

The header was sent in the legacy Feature-Policy form ("geolocation 'none'").
Permissions-Policy is an RFC 8941 structured-field dictionary, so browsers
failed to parse it, logged a console error on every page, and ignored the
whole header -- none of the intended feature restrictions applied.
"""
import re

DENIED_FEATURES = {"geolocation", "microphone", "camera", "payment", "usb"}

# A dictionary member whose value is an empty inner list: deny to all origins.
_DENY_ALL_MEMBER = re.compile(r"[a-z][a-z0-9-]*=\(\)")


def test_permissions_policy_denies_features_in_parseable_syntax(client):
    header = client.get("/auth/login").headers["Permissions-Policy"]
    members = [member.strip() for member in header.split(",")]

    assert all(_DENY_ALL_MEMBER.fullmatch(member) for member in members), header
    features = [member.split("=")[0] for member in members]
    assert sorted(features) == sorted(DENIED_FEATURES)
