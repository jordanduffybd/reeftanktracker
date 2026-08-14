"""Tests for the dose-change listener (implicit acknowledgement).

When the user changes a doser's `_daily_dose` entity directly (ReefBeat
mobile app, manual edit, etc.) the listener classifies the change against
the current suggestion:

  - within tolerance      → implicit Acknowledgment
  - moved toward suggest  → implicit Acknowledgment (partial)
  - moved away from sugg  → implicit DemandChange

Suggestions come from the same compute paths the explicit Ack handler
uses (alk_advisor.compute_for_entity / param_advisor.compute_for_param),
so the tests stub those to return controllable Recommendation objects.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from reeftanktracker.coordinator import ReefDataCoordinator


@dataclass
class _FakeRec:
    """Minimum surface area for `_classify_change` to read."""
    suggested_dose_mL: float | None


@pytest.fixture
def hass_with_states():
    """An HA mock that holds a state dict + supports states.get(eid)."""
    import homeassistant.core as core_mod
    hass = MagicMock()
    states_by_id: dict = {}
    hass.async_create_task = lambda coro: asyncio.create_task(coro)

    def _get(eid):
        return states_by_id.get(eid)
    hass.states.get.side_effect = _get
    hass._states_by_id = states_by_id
    hass._State = core_mod.State
    return hass


def _set_state(hass, eid: str, value: float | str) -> None:
    hass._states_by_id[eid] = hass._State(
        entity_id=eid, state=str(value),
    )


async def _setup_listener_with_alk_head(
    hass, monkeypatch, *, suggested: float | None, live_total: float,
    alk_head_entity: str = "sensor.fake_alk_head",
):
    """Common scaffold: configure coordinator with one alk head, stub
    the alk advisor compute_for_entity to return `suggested`, stub
    _sum_dose_mL to return `live_total`, and return a started listener."""
    from reeftanktracker.dose_change_listener import DoseChangeListener
    import reeftanktracker.alk_advisor as alk_advisor_mod
    import reeftanktracker.dose_change_listener as listener_mod
    from reeftanktracker.alk_advisor import OPT_ALK_HEADS, OPT_KH_SOURCE

    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({
        OPT_ALK_HEADS: [alk_head_entity],
        OPT_KH_SOURCE: "sensor.fake_kh",
    })

    monkeypatch.setattr(
        alk_advisor_mod, "compute_for_entity",
        lambda h, c: _FakeRec(suggested_dose_mL=suggested) if suggested is not None else None,
    )
    monkeypatch.setattr(
        alk_advisor_mod, "_sum_dose_mL",
        lambda h, eids: live_total,
    )

    captured: dict = {}
    def _track(hass_, ids, cb):
        captured["cb"] = cb
        captured["ids"] = ids
        return lambda: None
    monkeypatch.setattr(
        listener_mod, "async_track_state_change_event", _track,
    )

    listener = DoseChangeListener(hass, coord)
    await listener.async_start()
    return coord, listener, captured


def _fire_change(hass, captured, eid: str, old: float | None, new: float | None):
    """Build a state-change Event and pass it through the captured cb."""
    import homeassistant.core as core_mod
    old_state = (
        core_mod.State(entity_id=eid, state=str(old))
        if old is not None else None
    )
    new_state = (
        core_mod.State(entity_id=eid, state=str(new))
        if new is not None else None
    )
    captured["cb"](core_mod.Event({
        "new_state": new_state,
        "old_state": old_state,
    }))


# ---------------------------------------------------------------------------
# Within-tolerance → implicit ack
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_change_to_suggestion_records_implicit_ack(
    hass_with_states, monkeypatch,
):
    """User bumped Foundation B from 9.3 → 11.0; advisor suggests 11.0
    exactly. Should record an implicit ack."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)  # seed pre-change
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=11.0, live_total=11.0,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.3, 11.0)
    await asyncio.sleep(0); await asyncio.sleep(0)

    acks = coord._advisor_blob("kh")["acknowledgments"]
    assert len(acks) == 1
    a = acks[0]
    assert a["applied_value_mL"] == 11.0
    assert a["prev_value_mL"] == 9.3
    assert a["implicit"] is True
    assert a["suggested_value_mL"] == 11.0


@pytest.mark.asyncio
async def test_within_10pct_tolerance_records_ack(hass_with_states, monkeypatch):
    """User bumped 9.3 → 10.5; advisor suggested 11.0. Gap 0.5 mL on
    target 11.0 = 4.5% < 10% tolerance → records ack."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=11.0, live_total=10.5,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.3, 10.5)
    await asyncio.sleep(0); await asyncio.sleep(0)

    acks = coord._advisor_blob("kh")["acknowledgments"]
    assert len(acks) == 1
    assert acks[0]["applied_value_mL"] == 10.5


# ---------------------------------------------------------------------------
# Partial (toward but not within) → implicit ack still
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_moved_toward_suggestion_records_partial_ack(
    hass_with_states, monkeypatch,
):
    """User bumped 9.3 → 9.8; advisor suggested 12.0. Direction is right
    but well outside tolerance. Records ack flagged as partial."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=12.0, live_total=9.8,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.3, 9.8)
    await asyncio.sleep(0); await asyncio.sleep(0)

    acks = coord._advisor_blob("kh")["acknowledgments"]
    assert len(acks) == 1
    a = acks[0]
    assert a["applied_value_mL"] == 9.8
    assert a["implicit"] is True
    # tolerance_pct reflects how far from the suggestion the user landed.
    assert a["tolerance_pct"] > 10.0


# ---------------------------------------------------------------------------
# Moved away → demand change
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_moved_away_from_suggestion_records_demand_change(
    hass_with_states, monkeypatch,
):
    """User dropped 9.3 → 7.0; advisor was suggesting 11.0 (increase).
    Records as DemandChange, not an Ack."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=11.0, live_total=7.0,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.3, 7.0)
    await asyncio.sleep(0); await asyncio.sleep(0)

    assert coord._advisor_blob("kh")["acknowledgments"] == []
    demand = coord._advisor_blob("kh")["demand_changes"]
    assert len(demand) == 1
    assert "Auto-detected" in demand[0]["reason"]
    assert demand[0]["expected_direction"] == "falling"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_recommendation_means_no_ack(hass_with_states, monkeypatch):
    """When the advisor has no active suggestion (disabled, cooldown
    expired, etc.) a dose change doesn't synthesise an ack."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=None, live_total=11.0,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.3, 11.0)
    await asyncio.sleep(0); await asyncio.sleep(0)

    assert coord._advisor_blob("kh")["acknowledgments"] == []
    assert coord._advisor_blob("kh")["demand_changes"] == []


@pytest.mark.asyncio
async def test_tiny_change_below_min_is_ignored(hass_with_states, monkeypatch):
    """A 0.02 mL change is below MIN_CHANGE_ML — ignored as noise."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.30)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=11.0, live_total=9.32,
    )
    _fire_change(hass, captured, "sensor.fake_alk_head", 9.30, 9.32)
    await asyncio.sleep(0); await asyncio.sleep(0)

    assert coord._advisor_blob("kh")["acknowledgments"] == []
    assert coord._advisor_blob("kh")["demand_changes"] == []


@pytest.mark.asyncio
async def test_unavailable_states_are_ignored(hass_with_states, monkeypatch):
    """ReefBeat probes flap to `unavailable` periodically — must not
    trigger phantom acks."""
    hass = hass_with_states
    _set_state(hass, "sensor.fake_alk_head", 9.3)
    coord, listener, captured = await _setup_listener_with_alk_head(
        hass, monkeypatch, suggested=11.0, live_total=9.3,
    )
    # Simulate transition to unavailable.
    import homeassistant.core as core_mod
    old_state = core_mod.State(entity_id="sensor.fake_alk_head", state="9.3")
    new_state = core_mod.State(
        entity_id="sensor.fake_alk_head", state="unavailable",
    )
    captured["cb"](core_mod.Event({
        "new_state": new_state, "old_state": old_state,
    }))
    await asyncio.sleep(0); await asyncio.sleep(0)

    assert coord._advisor_blob("kh")["acknowledgments"] == []


# ---------------------------------------------------------------------------
# Acknowledgment record shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_explicit_ack_does_not_set_implicit_flag(hass_with_states):
    """The existing dashboard ack path must still record `implicit`
    absent — implicit is opt-in via kwarg."""
    coord = ReefDataCoordinator(hass_with_states)
    await coord.async_load()
    await coord.async_record_advisor_acknowledgment(
        "kh", applied_value_mL=11.0, prev_value_mL=9.3,
    )
    a = coord._advisor_blob("kh")["acknowledgments"][0]
    assert "implicit" not in a
    assert "suggested_value_mL" not in a
    assert "tolerance_pct" not in a


@pytest.mark.asyncio
async def test_implicit_ack_stores_diagnostic_fields(hass_with_states):
    """When the listener records via `implicit=True`, the diagnostic
    fields are persisted so the dashboard can show context."""
    coord = ReefDataCoordinator(hass_with_states)
    await coord.async_load()
    await coord.async_record_advisor_acknowledgment(
        "kh",
        applied_value_mL=10.5, prev_value_mL=9.3,
        implicit=True, suggested_value_mL=11.0, tolerance_pct=4.5,
    )
    a = coord._advisor_blob("kh")["acknowledgments"][0]
    assert a["implicit"] is True
    assert a["suggested_value_mL"] == 11.0
    assert a["tolerance_pct"] == 4.5
