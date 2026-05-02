"""
Tests for ``utils.duration`` -- the single source of truth for converting
between minutes (the storage unit), hours, timedeltas, and display
strings. These tests guard against the C2-style factor-of-60 bug ever
returning.
"""
from datetime import timedelta

import pytest

from utils.duration import (
    MINUTES_PER_HOUR,
    format_duration,
    hours_to_minutes,
    minutes_to_hours,
    split_minutes,
    timedelta_to_minutes,
)


# ---------------------------------------------------------------------------
# minutes_to_hours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, 0.0),
        (60, 1.0),
        (90, 1.5),
        (15, 0.25),
        (None, 0.0),
        ("", 0.0),     # garbage in -> zero, never crash dashboards
        ("abc", 0.0),
        (-30, 0.0),    # negative durations are corrupt; surface as 0
    ],
)
def test_minutes_to_hours(minutes, expected):
    assert minutes_to_hours(minutes) == expected


def test_minutes_to_hours_uses_real_factor():
    """Guard against a C2-style /3600 regression: if anyone changes the
    constant, this test fails loudly."""
    assert MINUTES_PER_HOUR == 60
    assert minutes_to_hours(MINUTES_PER_HOUR) == 1.0


# ---------------------------------------------------------------------------
# hours_to_minutes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hours, expected",
    [
        (0, 0),
        (1, 60),
        (1.5, 90),
        (0.25, 15),
        (None, 0),
        (2.999, 179),  # int() truncates, matches batch-import behavior
        (-1, 0),       # negatives clamp to 0 (matches minutes_to_hours)
        (-0.5, 0),
        ("", 0),       # garbage in -> zero, never crash batch import
        ("abc", 0),
        ("2.5", 150),  # numeric strings still convert
    ],
)
def test_hours_to_minutes(hours, expected):
    assert hours_to_minutes(hours) == expected


def test_hours_to_minutes_round_trip_is_lossless_for_whole_hours():
    for h in range(0, 24):
        assert minutes_to_hours(hours_to_minutes(h)) == float(h)


# ---------------------------------------------------------------------------
# timedelta_to_minutes
# ---------------------------------------------------------------------------

def test_timedelta_to_minutes_basic():
    assert timedelta_to_minutes(timedelta(hours=1)) == 60
    assert timedelta_to_minutes(timedelta(minutes=45)) == 45
    assert timedelta_to_minutes(timedelta(hours=2, minutes=30)) == 150


def test_timedelta_to_minutes_truncates_seconds():
    # 30 minutes 59 seconds -> 30 minutes (we don't round up partial mins)
    assert timedelta_to_minutes(timedelta(minutes=30, seconds=59)) == 30


def test_timedelta_to_minutes_zero_is_allowed():
    assert timedelta_to_minutes(timedelta(0)) == 0


def test_timedelta_to_minutes_negative_raises():
    with pytest.raises(ValueError):
        timedelta_to_minutes(timedelta(minutes=-5))


def test_timedelta_to_minutes_none_raises():
    with pytest.raises(ValueError):
        timedelta_to_minutes(None)


# ---------------------------------------------------------------------------
# split_minutes / format_duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, (0, 0)),
        (45, (0, 45)),
        (60, (1, 0)),
        (150, (2, 30)),
        (None, (0, 0)),
    ],
)
def test_split_minutes(minutes, expected):
    assert split_minutes(minutes) == expected


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (None, "0m"),
        (0, "0m"),
        (1, "1m"),
        (59, "59m"),
        (60, "1h"),
        (90, "1h 30m"),
        (125, "2h 5m"),
        (1440, "24h"),
    ],
)
def test_format_duration(minutes, expected):
    assert format_duration(minutes) == expected


# ---------------------------------------------------------------------------
# Jinja filter wiring
# ---------------------------------------------------------------------------

def test_format_duration_is_registered_as_jinja_filter(app):
    """The whole point of centralizing is that templates use the same
    helper as Python code. Verify the filter is wired up."""
    assert "format_duration" in app.jinja_env.filters
    assert "minutes_to_hours" in app.jinja_env.filters
    rendered = app.jinja_env.from_string("{{ 90|format_duration }}").render()
    assert rendered == "1h 30m"
