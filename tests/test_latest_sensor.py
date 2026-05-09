"""Tests for `ReefLatestSensor._resolve_latest` — the freshness
comparison between a recorded reading and the live auto-source state.

Background: the original implementation always returned a recorded
reading when one existed, ignoring the live auto-source. So a stale
manual entry from 2 days ago would dominate forever, even when the
KH Keeper sensor was reading 7.84 right now. The auto-source listener
also filters out unchanged values (significance threshold), so it
doesn't always create a fresh recorded entry to bump the timestamp.

Fix: compare the latest-recorded `sample_taken_at` against the live
auto-source's `last_changed` and prefer whichever is newer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from reeftanktracker.sensor import ReefLatestSensor


def _make_sensor(
    *,
    latest_reading: dict | None,
    auto_source: str | None,
    auto_state: str | None = None,
    auto_last_changed: datetime | None = None,
    target_range: tuple[float | None, float | None] = (None, None),
):
    """Build a ReefLatestSensor wired to stubbed coordinator + hass."""
    coord = MagicMock()
    coord.latest_reading.return_value = latest_reading
    coord.get_auto_source.return_value = auto_source
    coord.get_target_range.return_value = target_range

    hass = MagicMock()
    if auto_source and auto_state is not None:
        state = MagicMock()
        state.state = auto_state
        state.last_changed = auto_last_changed
        state.last_updated = auto_last_changed
        hass.states.get.side_effect = (
            lambda eid: state if eid == auto_source else None
        )
    else:
        hass.states.get.return_value = None

    param = {
        "id": "kh", "name": "KH", "unit": "dKH",
        "min": 0, "max": 20, "step": 0.01, "precision": 2,
        "auto_source": "sensor.kh_keeper_kh",
    }
    sensor = ReefLatestSensor(coord, param)
    sensor.hass = hass
    return sensor


def test_live_auto_wins_when_newer_than_recorded():
    """KH Keeper just measured 7.84; manual reading is from 2 days
    ago at 7.6. Latest sensor returns 7.84."""
    now = datetime.now(timezone.utc)
    sensor = _make_sensor(
        latest_reading={
            "value": 7.6, "source": "manual",
            "sample_taken_at": (now - timedelta(days=2)).isoformat(),
            "recorded_at": (now - timedelta(days=2)).isoformat(),
        },
        auto_source="sensor.kh_keeper_kh",
        auto_state="7.84",
        auto_last_changed=now - timedelta(hours=2),
    )
    assert sensor.native_value == 7.84
    val, src = sensor._resolve_latest()
    assert val == 7.84
    assert src == "auto-live"


def test_recorded_wins_when_newer_than_auto_state():
    """Manual reading from today beats KH Keeper state from 3 days
    ago (e.g. doser is offline / hasn't tested recently)."""
    now = datetime.now(timezone.utc)
    sensor = _make_sensor(
        latest_reading={
            "value": 7.6, "source": "manual",
            "sample_taken_at": (now - timedelta(hours=2)).isoformat(),
            "recorded_at": (now - timedelta(hours=2)).isoformat(),
        },
        auto_source="sensor.kh_keeper_kh",
        auto_state="7.84",
        auto_last_changed=now - timedelta(days=3),
    )
    assert sensor.native_value == 7.6
    _, src = sensor._resolve_latest()
    assert src == "manual"


def test_no_auto_source_falls_back_to_recorded():
    """Param without an auto-source — recorded reading is the only
    answer."""
    now = datetime.now(timezone.utc)
    sensor = _make_sensor(
        latest_reading={
            "value": 7.5, "source": "manual",
            "sample_taken_at": now.isoformat(),
            "recorded_at": now.isoformat(),
        },
        auto_source=None,
    )
    assert sensor.native_value == 7.5


def test_no_recorded_uses_live_auto():
    """Fresh install — no readings recorded yet, but KH Keeper is
    reporting. Latest sensor returns the live state."""
    now = datetime.now(timezone.utc)
    sensor = _make_sensor(
        latest_reading=None,
        auto_source="sensor.kh_keeper_kh",
        auto_state="8.2",
        auto_last_changed=now,
    )
    assert sensor.native_value == 8.2
    _, src = sensor._resolve_latest()
    assert src == "auto-live"


def test_unavailable_auto_falls_back_to_recorded():
    """KH Keeper offline (state='unavailable') — recorded reading
    wins regardless of timestamps."""
    now = datetime.now(timezone.utc)
    sensor = _make_sensor(
        latest_reading={
            "value": 7.6, "source": "manual",
            "sample_taken_at": (now - timedelta(days=2)).isoformat(),
            "recorded_at": (now - timedelta(days=2)).isoformat(),
        },
        auto_source="sensor.kh_keeper_kh",
        auto_state="unavailable",
        auto_last_changed=now,
    )
    assert sensor.native_value == 7.6


def test_nothing_known_returns_none():
    """No recorded readings, no auto-source, no live state."""
    sensor = _make_sensor(
        latest_reading=None,
        auto_source=None,
    )
    assert sensor.native_value is None
