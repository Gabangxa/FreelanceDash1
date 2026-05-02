"""
Centralized duration conversion + formatting helpers.

Why this module exists
----------------------
``TimeEntry.duration`` is stored in **minutes** in the database (see
``models.py``). Before this module was introduced, the same conversions
were re-implemented inline in at least six places:

* ``admin/routes.py``     -- divided by 3600 (latent factor-of-60 bug
                             fixed in C2 -- this module makes that
                             impossible to reintroduce).
* ``projects/routes.py``  -- ``entry.duration / 60.0`` and
                             ``int(time_diff.total_seconds() / 60)``
                             scattered across ~10 spots, plus a local
                             nested ``def format_duration(minutes)``.
* ``templates/projects/time_statistics.html`` and ``task_detail.html``
                          -- inline ``{% set hours = entry.duration // 60 %}``
                             ``{% set mins  = entry.duration %  60 %}``.

A single typo in any of those (``/3600`` vs ``/60``, ``//`` vs ``/``)
silently corrupts the dashboard. Concentrating the math here means
there is exactly one place to read, test, and audit.

All functions tolerate ``None`` and treat it as zero, which matches how
the database represents "no duration recorded yet".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple, Union

# Single source of truth for the unit conversion factor. If you ever want
# to change the storage unit (say, to seconds), this is the one place to
# audit -- everything else routes through these helpers.
MINUTES_PER_HOUR: int = 60
SECONDS_PER_MINUTE: int = 60

Number = Union[int, float]
Minutes = Optional[Number]


def _coerce_minutes(value: Minutes) -> float:
    """Internal: normalize a possibly-None duration into a float of minutes.

    Negative durations are treated as zero -- a TimeEntry with a negative
    minute count is corrupt data, and surfacing a negative ``hours``
    figure on the dashboard is worse than zero.
    """
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def minutes_to_hours(minutes: Minutes) -> float:
    """Convert a minute count to fractional hours.

    >>> minutes_to_hours(90)
    1.5
    >>> minutes_to_hours(None)
    0.0
    """
    return _coerce_minutes(minutes) / MINUTES_PER_HOUR


def hours_to_minutes(hours: Optional[Number]) -> int:
    """Convert fractional hours to whole minutes (rounded down).

    Used by the batch-time-entry import path where users type "1.5" hours
    and we need to persist 90 in ``TimeEntry.duration``.

    Defensive on bad input -- ``None``, empty strings, garbage, and
    negative numbers all collapse to ``0``. This matches the policy of
    ``minutes_to_hours`` so the whole module behaves consistently:
    callers should still validate user input upstream and surface a
    friendly error, but a stray bad value can never produce a negative
    duration in the database.
    """
    if hours is None:
        return 0
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return 0
    if h <= 0:
        return 0
    return int(h * MINUTES_PER_HOUR)


def timedelta_to_minutes(td: timedelta) -> int:
    """Convert a ``timedelta`` into whole minutes (rounded down).

    Used when computing ``TimeEntry.duration`` from start/end timestamps.
    Negative timedeltas raise ``ValueError`` -- callers should validate
    end > start before this point and surface a friendly error to the
    user (see ``projects/routes.py``).
    """
    if td is None:
        raise ValueError("timedelta_to_minutes called with None")
    total_seconds = td.total_seconds()
    if total_seconds < 0:
        raise ValueError(
            f"Cannot convert a negative timedelta to a duration "
            f"(got {td!r}). Validate end_time > start_time first."
        )
    return int(total_seconds // SECONDS_PER_MINUTE)


def split_minutes(minutes: Minutes) -> Tuple[int, int]:
    """Split a minute count into a ``(hours, minutes)`` tuple.

    Convenient for templates that want to render the two parts
    separately (e.g. with different styling).
    """
    total = int(_coerce_minutes(minutes))
    return divmod(total, MINUTES_PER_HOUR)


def format_duration(minutes: Minutes) -> str:
    """Render a minute count as a compact human string.

    * ``None`` / ``0``  -> ``"0m"``
    * ``< 60``          -> ``"45m"``
    * ``>= 60``         -> ``"2h 30m"`` (or ``"2h"`` when there are no
                          stray minutes)

    Registered as the Jinja ``format_duration`` filter in ``app.py`` so
    templates can write ``{{ entry.duration|format_duration }}`` instead
    of duplicating ``{% set hours = entry.duration // 60 %}`` math.
    """
    hours, mins = split_minutes(minutes)
    if hours == 0:
        return f"{mins}m"
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"
