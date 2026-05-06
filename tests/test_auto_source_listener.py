"""Auto-source listener behaviour.

Covers the throttle + significance filter and the bridge from upstream
state-change events to coordinator-recorded auto readings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.core import Event, State

from reeftanktracker import auto_source_listener as asl
from reeftanktracker.auto_source_listener import (
    AutoSourceListener,
    MAX_QUIET,
    MIN_INTERVAL,
)
from reeftanktracker.const import SOURCE_AUTO
from reeftanktracker.coordinator import ReefDataCoordinator


def _make_hass(state_by_entity: dict[str, State] | None = None) -> Any:
    import asyncio
    hass = MagicMock()
    hass.states.get.side_effect = lambda eid: (state_by_entity or {}).get(eid)
    # async_create_task schedules a coroutine on the running loop; in
    # tests we drain via `await asyncio.sleep(0)` after dispatching.
    hass.async_create_task.side_effect = lambda coro: asyncio.ensure_future(coro)
    return hass


def _state(entity_id: str, value: str, when: datetime | None = None) -> State:
    return State(entity_id, value, last_updated=when or datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_initial_state_records_one_reading_per_param(monkeypatch):
    """On start, current upstream value is captured immediately."""
    captured: list = []
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: captured.append((ids, cb)) or (lambda: None),
    )

    initial = _state("sensor.kh_keeper_kh", "8.30")
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()

    readings = coord.readings_for("kh")
    assert len(readings) == 1
    assert readings[0]["source"] == SOURCE_AUTO
    assert readings[0]["value"] == 8.30
    assert captured  # listener subscribed


@pytest.mark.asyncio
async def test_state_change_records_when_above_significance(monkeypatch):
    """A change ≥ parameter step triggers a recorded reading."""
    handler_holder: dict[str, Any] = {}
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: handler_holder.setdefault("cb", cb) or (lambda: None),
    )

    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    initial = _state("sensor.kh_keeper_kh", "8.30", when=t0)
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()  # records 8.30 at t0

    # Simulate upstream tick six minutes later, +0.10 dKH (well over step 0.05)
    later = _state(
        "sensor.kh_keeper_kh", "8.40", when=t0 + timedelta(minutes=6),
    )
    handler_holder["cb"](Event({"new_state": later}))

    # async_create_task scheduled the record — give the loop a tick
    await _drain()
    readings = coord.readings_for("kh")
    assert [r["value"] for r in readings] == [8.30, 8.40]


@pytest.mark.asyncio
async def test_state_change_below_significance_is_skipped(monkeypatch):
    handler_holder: dict[str, Any] = {}
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: handler_holder.setdefault("cb", cb) or (lambda: None),
    )

    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    initial = _state("sensor.kh_keeper_kh", "8.30", when=t0)
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()

    # +0.02 dKH after 6 minutes — over the interval but below the step (0.05)
    later = _state(
        "sensor.kh_keeper_kh", "8.32", when=t0 + timedelta(minutes=6),
    )
    handler_holder["cb"](Event({"new_state": later}))
    await _drain()

    assert len(coord.readings_for("kh")) == 1


@pytest.mark.asyncio
async def test_rapid_ticks_are_throttled(monkeypatch):
    """A second state change within MIN_INTERVAL is dropped."""
    handler_holder: dict[str, Any] = {}
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: handler_holder.setdefault("cb", cb) or (lambda: None),
    )

    t0 = datetime.now(timezone.utc) - timedelta(minutes=30)
    initial = _state("sensor.kh_keeper_kh", "8.30", when=t0)
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()

    # Big jump but only 1 minute later — should be filtered by interval
    soon = _state(
        "sensor.kh_keeper_kh", "9.50",
        when=t0 + (MIN_INTERVAL / 2),
    )
    handler_holder["cb"](Event({"new_state": soon}))
    await _drain()

    assert len(coord.readings_for("kh")) == 1


@pytest.mark.asyncio
async def test_quiet_window_forces_record_even_if_unchanged(monkeypatch):
    """If MAX_QUIET elapsed with no record, log it regardless of delta."""
    handler_holder: dict[str, Any] = {}
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: handler_holder.setdefault("cb", cb) or (lambda: None),
    )

    t0 = datetime.now(timezone.utc) - MAX_QUIET - timedelta(hours=1)
    initial = _state("sensor.kh_keeper_kh", "8.30", when=t0)
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()  # records once at t0

    # Same value, but 25h later — quiet window hit
    later = _state(
        "sensor.kh_keeper_kh", "8.30",
        when=t0 + MAX_QUIET + timedelta(hours=1),
    )
    handler_holder["cb"](Event({"new_state": later}))
    await _drain()

    assert len(coord.readings_for("kh")) == 2


@pytest.mark.asyncio
async def test_unavailable_state_is_ignored(monkeypatch):
    handler_holder: dict[str, Any] = {}
    monkeypatch.setattr(
        asl, "async_track_state_change_event",
        lambda hass, ids, cb: handler_holder.setdefault("cb", cb) or (lambda: None),
    )

    initial = _state("sensor.kh_keeper_kh", "unavailable")
    hass = _make_hass({"sensor.kh_keeper_kh": initial})
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({"kh": "sensor.kh_keeper_kh"})

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()

    assert coord.readings_for("kh") == []


@pytest.mark.asyncio
async def test_listener_idle_when_no_auto_sources(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(asl, "async_track_state_change_event", called)

    hass = _make_hass()
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_auto_sources({})  # no params configured

    listener = AutoSourceListener(hass, coord)
    await listener.async_start()
    listener.async_stop()  # should be a no-op

    called.assert_not_called()


async def _drain() -> None:
    """Yield to the event loop so any scheduled tasks run."""
    import asyncio
    await asyncio.sleep(0)
