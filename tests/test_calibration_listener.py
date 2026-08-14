"""Tests for the KH Keeper calibration listener + coordinator helpers.

The bridge (0.1.15+) publishes `sensor.kh_keeper_last_calibration` with
the calibration event in `attributes`. We listen to its state changes,
record the event under `advisor.kh.calibration_events`, capture the
Hanna value as a manual KH reading, and mark a settling window around
the event so the alk snapshotter skips contaminated captures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reeftanktracker.calibration_listener import (
    CalibrationEventListener,
    derive_calibration_entity,
)
from reeftanktracker.const import SOURCE_MANUAL
from reeftanktracker.coordinator import ReefDataCoordinator


def _iso(when: datetime) -> str:
    return when.astimezone().isoformat()


# ---------------------------------------------------------------------------
# Entity-name derivation
# ---------------------------------------------------------------------------
def test_derive_calibration_entity_from_kh_source():
    assert derive_calibration_entity("sensor.kh_keeper_kh") == (
        "sensor.kh_keeper_last_calibration"
    )


def test_derive_calibration_entity_returns_none_for_non_kh_keeper_source():
    """User pointed the KH source at a non-bridge entity — no derivation."""
    assert derive_calibration_entity("sensor.trident_kh") is None
    assert derive_calibration_entity("input_number.manual_kh") is None
    assert derive_calibration_entity("") is None
    assert derive_calibration_entity(None) is None


# ---------------------------------------------------------------------------
# Coordinator.async_record_calibration_event
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_calibration_event_appends_to_advisor_blob(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    event = {
        "ts": _iso(datetime.now(timezone.utc)),
        "prev": -1.01,
        "new": -0.57,
        "delta": 0.44,
        "source": "ha_drop_test",
        "hanna_value": 8.55,
        "serial": "kh_keeper_test",
    }
    await coord.async_record_calibration_event(event)

    blob = coord._advisor_blob("kh")
    assert len(blob["calibration_events"]) == 1
    e = blob["calibration_events"][0]
    assert e["prev"] == -1.01
    assert e["new"] == -0.57
    assert e["delta"] == 0.44
    assert e["source"] == "ha_drop_test"
    assert e["hanna_value"] == 8.55


@pytest.mark.asyncio
async def test_record_calibration_event_records_hanna_reading(hass):
    """An `ha_drop_test`-sourced event with a Hanna value also gets
    inserted as a manual KH reading — that's the user's ground truth."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    ts = _iso(datetime.now(timezone.utc))
    await coord.async_record_calibration_event({
        "ts": ts, "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.55,
        "serial": "khk",
    })

    kh_readings = list(coord.readings_for("kh"))
    assert len(kh_readings) == 1
    r = kh_readings[0]
    assert r["value"] == 8.55
    assert r["source"] == SOURCE_MANUAL
    assert "Hanna" in (r.get("method") or "")
    assert r["sample_taken_at"] == ts


@pytest.mark.asyncio
async def test_device_calibration_does_not_record_phantom_reading(hass):
    """Device-side calibrations don't expose the Hanna value the user
    actually measured — so we record the event but NOT a reading."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    await coord.async_record_calibration_event({
        "ts": _iso(datetime.now(timezone.utc)),
        "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "device", "hanna_value": None, "serial": "khk",
    })

    assert list(coord.readings_for("kh")) == []
    assert len(coord._advisor_blob("kh")["calibration_events"]) == 1


@pytest.mark.asyncio
async def test_record_calibration_event_dedupes_on_serial_and_ts(hass):
    """Retained MQTT topics re-fire on every connect — the same event
    should never be recorded twice."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    event = {
        "ts": _iso(datetime.now(timezone.utc)),
        "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.55, "serial": "khk",
    }
    await coord.async_record_calibration_event(event)
    await coord.async_record_calibration_event(event)  # duplicate
    await coord.async_record_calibration_event(event)  # duplicate

    assert len(coord._advisor_blob("kh")["calibration_events"]) == 1
    # And the Hanna reading is also recorded once, not three times.
    assert len(list(coord.readings_for("kh"))) == 1


# ---------------------------------------------------------------------------
# Settling-window check
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_settling_window_true_within_24h(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    cal_ts = datetime.now(timezone.utc)
    await coord.async_record_calibration_event({
        "ts": _iso(cal_ts), "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.5, "serial": "khk",
    })

    # 6h before, 6h after, exactly 24h after — all within the window.
    assert coord.is_in_calibration_settling_window(
        _iso(cal_ts - timedelta(hours=6))
    )
    assert coord.is_in_calibration_settling_window(
        _iso(cal_ts + timedelta(hours=6))
    )
    assert coord.is_in_calibration_settling_window(
        _iso(cal_ts + timedelta(hours=24))
    )


@pytest.mark.asyncio
async def test_settling_window_false_outside_24h(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    cal_ts = datetime.now(timezone.utc)
    await coord.async_record_calibration_event({
        "ts": _iso(cal_ts), "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.5, "serial": "khk",
    })

    # 25h before / after — outside.
    assert not coord.is_in_calibration_settling_window(
        _iso(cal_ts - timedelta(hours=25))
    )
    assert not coord.is_in_calibration_settling_window(
        _iso(cal_ts + timedelta(hours=25))
    )


@pytest.mark.asyncio
async def test_settling_window_false_with_no_events(hass):
    """A fresh coordinator with no events — every timestamp is outside."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    assert not coord.is_in_calibration_settling_window(
        _iso(datetime.now(timezone.utc))
    )


# ---------------------------------------------------------------------------
# Listener wiring (smoke test — full state-change events tested
# separately if the integration grows that surface)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_listener_idle_when_no_entity_configured(hass):
    """No entity → listener doesn't subscribe; async_start is a no-op."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    listener = CalibrationEventListener(hass, coord, entity_id=None)
    await listener.async_start()
    assert listener._unsub is None


@pytest.mark.asyncio
async def test_listener_processes_state_with_full_event_payload(hass, monkeypatch):
    """Drive the listener with a synthetic state object and verify the
    event flows through to the coordinator."""
    import asyncio
    import reeftanktracker.calibration_listener as listener_mod
    import homeassistant.core as core_mod

    # Capture the registered handler so the test can fire it. Patch on
    # the listener module — `async_track_state_change_event` is bound
    # at import time so patching the source module doesn't update the
    # local reference.
    captured: dict = {}
    def _track(hass, ids, cb):
        captured["cb"] = cb
        captured["ids"] = ids
        return lambda: None
    monkeypatch.setattr(
        listener_mod, "async_track_state_change_event", _track,
    )

    # hass.async_create_task is a MagicMock by default — wire it to
    # asyncio.create_task so the listener's handler actually runs.
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    # No initial state — start with the entity missing.
    hass.states.get.return_value = None

    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    listener = CalibrationEventListener(
        hass, coord, entity_id="sensor.kh_keeper_last_calibration",
    )
    await listener.async_start()
    assert "cb" in captured

    # Synthesise a state-change event from the bridge.
    ts = _iso(datetime.now(timezone.utc))
    new_state = core_mod.State(
        entity_id="sensor.kh_keeper_last_calibration",
        state=ts,
        attributes={
            "ts": ts, "prev": -1.0, "new": -0.5, "delta": 0.5,
            "source": "ha_drop_test", "hanna_value": 8.55, "serial": "khk",
        },
    )
    captured["cb"](core_mod.Event({"new_state": new_state}))
    # Run any tasks the handler scheduled.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    blob = coord._advisor_blob("kh")
    assert len(blob["calibration_events"]) == 1
    assert blob["calibration_events"][0]["hanna_value"] == 8.55


# ---------------------------------------------------------------------------
# AlkAdvisorSnapshotter — settling-window integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_snapshotter_skips_when_inside_settling_window(hass, monkeypatch):
    """When a calibration event was just recorded, the daily snapshot
    must NOT capture — otherwise the step-change in displayed KH gets
    attributed to dose and explodes the empirical-potency derivation."""
    from reeftanktracker.alk_advisor import (
        AlkAdvisorSnapshotter, OPT_ALK_HEADS, OPT_KH_SOURCE,
    )
    import reeftanktracker.alk_advisor as advisor_mod

    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({
        OPT_ALK_HEADS: ["sensor.fake_alk_head"],
        OPT_KH_SOURCE: "sensor.fake_kh",
    })
    # Calibration just happened.
    await coord.async_record_calibration_event({
        "ts": _iso(datetime.now(timezone.utc)),
        "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.55, "serial": "khk",
    })

    # Stub the dose/kh readers so the snapshotter believes it has data.
    monkeypatch.setattr(
        advisor_mod, "_read_float_state", lambda hass, eid: 8.55,
    )
    monkeypatch.setattr(
        advisor_mod, "_sum_dose_mL", lambda hass, eids: 11.0,
    )

    snap = AlkAdvisorSnapshotter(hass, coord)
    snapshots_before = len(coord._advisor_blob("kh")["snapshots"])
    # The Hanna reading we recorded above pushed one snapshot via the
    # record-reading path; ignore that in our before count by measuring
    # the delta. Calibration record adds the Hanna manual reading
    # which auto-snapshots — that's fine for this test, it's the
    # *daily* _capture path we're checking.
    await snap._capture(datetime.now(timezone.utc))

    # _capture must have skipped — snapshots count unchanged.
    assert len(coord._advisor_blob("kh")["snapshots"]) == snapshots_before


@pytest.mark.asyncio
async def test_snapshotter_captures_when_outside_settling_window(hass, monkeypatch):
    """Calibration event from 3 days ago — outside 24h window, snapshot
    proceeds normally."""
    from reeftanktracker.alk_advisor import (
        AlkAdvisorSnapshotter, OPT_ALK_HEADS, OPT_KH_SOURCE,
    )
    import reeftanktracker.alk_advisor as advisor_mod

    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({
        OPT_ALK_HEADS: ["sensor.fake_alk_head"],
        OPT_KH_SOURCE: "sensor.fake_kh",
    })
    await coord.async_record_calibration_event({
        "ts": _iso(datetime.now(timezone.utc) - timedelta(days=3)),
        "prev": -1.0, "new": -0.5, "delta": 0.5,
        "source": "ha_drop_test", "hanna_value": 8.55, "serial": "khk",
    })
    monkeypatch.setattr(
        advisor_mod, "_read_float_state", lambda hass, eid: 8.55,
    )
    monkeypatch.setattr(
        advisor_mod, "_sum_dose_mL", lambda hass, eids: 11.0,
    )

    snap = AlkAdvisorSnapshotter(hass, coord)
    snapshots_before = len(coord._advisor_blob("kh")["snapshots"])
    await snap._capture(datetime.now(timezone.utc))

    # One new snapshot recorded.
    assert len(coord._advisor_blob("kh")["snapshots"]) == snapshots_before + 1


@pytest.mark.asyncio
async def test_listener_ignores_payload_without_prev_or_new(hass, monkeypatch):
    """If the attribute payload is missing prev/new (stale or unrelated
    sensor), the listener must not record anything."""
    import reeftanktracker.calibration_listener as listener_mod
    import homeassistant.core as core_mod

    captured: dict = {}
    def _track(hass, ids, cb):
        captured["cb"] = cb
        return lambda: None
    monkeypatch.setattr(
        listener_mod, "async_track_state_change_event", _track,
    )
    import asyncio
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    hass.states.get.return_value = None

    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    listener = CalibrationEventListener(
        hass, coord, entity_id="sensor.kh_keeper_last_calibration",
    )
    await listener.async_start()

    bad_state = core_mod.State(
        entity_id="sensor.kh_keeper_last_calibration",
        state="2026-06-15T10:00:00+00:00",
        attributes={"ts": "2026-06-15T10:00:00+00:00"},  # no prev/new
    )
    captured["cb"](core_mod.Event({"new_state": bad_state}))
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert coord._advisor_blob("kh")["calibration_events"] == []
