"""Map a request endpoint to the top-level navbar section it belongs to."""

# Endpoints that live in one blueprint but belong under a different navbar
# item -- the dashboard and time pages are routes on the projects blueprint.
# Checked before the blueprint fallback below. Only endpoints that render a
# page need an entry; redirect-only POST handlers never show the navbar.
_ENDPOINT_SECTIONS = {
    'projects.dashboard': 'dashboard',
    'projects.batch_time_entries': 'time',
    'projects.time_entry_statistics': 'time',
    'projects.edit_time_entry': 'time',
}

_BLUEPRINT_SECTIONS = {
    'projects': 'projects',
    'clients': 'clients',
    'invoices': 'invoices',
    'admin': 'admin',
}


def nav_section(endpoint):
    """Return the navbar section for ``endpoint``, or None if no item applies.

    None covers unmatched URLs (404/405), where Flask sets no endpoint, and
    pages with no navbar item, such as settings and notifications.
    """
    if not endpoint:
        return None
    if endpoint in _ENDPOINT_SECTIONS:
        return _ENDPOINT_SECTIONS[endpoint]
    blueprint = endpoint.partition('.')[0]
    return _BLUEPRINT_SECTIONS.get(blueprint)
