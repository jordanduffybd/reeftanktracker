"""Daily KH aggregation for the alk advisor snapshot (0.5.12).

Before 0.5.12 the daily snapshot read the configured source entity's
*instantaneous* state at 23:55. That threw away every KH Keeper test but
the last one of the day, and it never saw manual Hanna titrations at all
(those land in the coordinator's readings, not on the Keeper entity).

`_daily_value` replaces that with two rules:
  1. Median over every reading in the 24h window, not a point sample.
  2. Manual/ICP readings override auto readings entirely when both exist
     — a Hanna titration is more accurate than the Keeper's colourimetric
     result, so we don't dilute it by averaging the two together.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

from reeftanktracker.alk_advisor import _daily_value
from reeftanktracker.const import SOURCE_AUTO, SOURCE_ICP, SOURCE_MANUAL


UTC = timezone.utc
NOW = datetime(2026, 8, 14, 23, 55, tzinfo=UTC)


def _reading(value: float, source: str, *, hours_ago: float) -> dict[str, Any]:
    return {
        "parameter": "kh",
        "value": value,
        "source": source,
        "sample_taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


def _coord(readings: list[dict[str, Any]]) -> Any:
    c = MagicMock()
    c.readings_for.return_value = readings
    return c


def test_returns_none_when_window_empty():
    value, kind, n_manual, n_auto = _daily_value(
        _coord([]), "kh", window_end=NOW,
    )
    assert value is None
    assert kind is None
    assert (n_manual, n_auto) == (0, 0)


def test_medians_all_auto_readings_in_window():
    """Four Keeper tests in one day — median, not just the last one."""
    readings = [
        _reading(8.6, SOURCE_AUTO, hours_ago=20),
        _reading(8.4, SOURCE_AUTO, hours_ago=14),
        _reading(8.2, SOURCE_AUTO, hours_ago=8),
        _reading(9.6, SOURCE_AUTO, hours_ago=1),   # bad titration, last of day
    ]
    value, kind, n_manual, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert kind == "auto"
    assert n_auto == 4
    # Median of [8.2, 8.4, 8.6, 9.6] = 8.5 — the spike does not dominate.
    # The pre-0.5.12 instantaneous read would have returned 9.6.
    assert value == 8.5


def test_single_auto_reading_still_works():
    """Jordan sometimes runs the Keeper once a day."""
    value, kind, _, n_auto = _daily_value(
        _coord([_reading(8.31, SOURCE_AUTO, hours_ago=3)]),
        "kh", window_end=NOW,
    )
    assert (value, kind, n_auto) == (8.31, "auto", 1)


def test_manual_overrides_auto_entirely():
    """A Hanna test present → Keeper readings are ignored, not averaged."""
    readings = [
        _reading(8.2, SOURCE_AUTO, hours_ago=18),
        _reading(8.3, SOURCE_AUTO, hours_ago=12),
        _reading(8.9, SOURCE_MANUAL, hours_ago=6),
    ]
    value, kind, n_manual, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert kind == "manual"
    assert value == 8.9          # NOT the 8.466 mean of all three
    assert (n_manual, n_auto) == (1, 2)


def test_multiple_manual_readings_are_medianed():
    readings = [
        _reading(8.0, SOURCE_AUTO, hours_ago=20),
        _reading(8.5, SOURCE_MANUAL, hours_ago=10),
        _reading(8.7, SOURCE_MANUAL, hours_ago=8),
        _reading(8.9, SOURCE_MANUAL, hours_ago=2),
    ]
    value, kind, n_manual, _ = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, kind, n_manual) == (8.7, "manual", 3)


def test_icp_counts_as_manual():
    """ICP is lab work — ground truth relative to the probe."""
    readings = [
        _reading(8.1, SOURCE_AUTO, hours_ago=12),
        _reading(8.8, SOURCE_ICP, hours_ago=4),
    ]
    value, kind, n_manual, _ = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, kind, n_manual) == (8.8, "manual", 1)


def test_readings_outside_window_are_excluded():
    readings = [
        _reading(9.9, SOURCE_AUTO, hours_ago=30),   # yesterday
        _reading(8.3, SOURCE_AUTO, hours_ago=5),
    ]
    value, _, _, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, n_auto) == (8.3, 1)


def test_stale_manual_does_not_override_todays_auto():
    """A Hanna test from three days ago must not suppress today's Keeper
    readings — the override is within-window only."""
    readings = [
        _reading(9.5, SOURCE_MANUAL, hours_ago=72),
        _reading(8.3, SOURCE_AUTO, hours_ago=6),
        _reading(8.5, SOURCE_AUTO, hours_ago=2),
    ]
    value, kind, n_manual, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert kind == "auto"
    assert (n_manual, n_auto) == (0, 2)
    assert value == 8.4


def test_non_numeric_and_bool_values_are_skipped():
    readings = [
        _reading(8.4, SOURCE_AUTO, hours_ago=6),
        {"parameter": "kh", "value": None, "source": SOURCE_AUTO,
         "sample_taken_at": (NOW - timedelta(hours=5)).isoformat()},
        {"parameter": "kh", "value": True, "source": SOURCE_AUTO,
         "sample_taken_at": (NOW - timedelta(hours=4)).isoformat()},
        {"parameter": "kh", "value": "8.9", "source": SOURCE_AUTO,
         "sample_taken_at": (NOW - timedelta(hours=3)).isoformat()},
    ]
    value, _, _, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, n_auto) == (8.4, 1)


def test_malformed_timestamp_is_skipped():
    readings = [
        {"parameter": "kh", "value": 9.9, "source": SOURCE_AUTO,
         "sample_taken_at": "not-a-date"},
        {"parameter": "kh", "value": 8.2, "source": SOURCE_AUTO},  # missing
        _reading(8.4, SOURCE_AUTO, hours_ago=2),
    ]
    value, _, _, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, n_auto) == (8.4, 1)


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    readings = [
        {"parameter": "kh", "value": 8.45, "source": SOURCE_AUTO,
         "sample_taken_at": naive},
    ]
    value, kind, _, n_auto = _daily_value(
        _coord(readings), "kh", window_end=NOW,
    )
    assert (value, kind, n_auto) == (8.45, "auto", 1)
