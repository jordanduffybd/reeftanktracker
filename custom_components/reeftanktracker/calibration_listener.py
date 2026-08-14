"""Subscribe to KH Keeper calibration events.

The kh-keeper-bridge add-on (0.1.15+) publishes a retained MQTT
discovery sensor `sensor.kh_keeper_last_calibration`. The state is an
ISO timestamp; the attributes carry the full event payload (`prev`,
`new`, `delta`, `source`, `hanna_value`, `serial`). When that entity's
state changes, a calibration just happened on the tester — either via
the bridge (drop-test or raw adjustment), via the Smart Reef mobile
app, or via the device's physical UI.

We listen to state changes on that entity (no new MQTT dependency
needed — the bridge already exposes it as an HA-discovered sensor)
and forward each event to the coordinator, which:

  - records the event in `advisor.kh.calibration_events`,
  - records the Hanna test value (when present) as a manual KH reading,
  - marks a settling window around the event so the alk advisor's
    snapshot loop skips captures that would otherwise contaminate the
    empirical-potency slope derivation.

This is the reeftank companion to kh-keeper-bridge's 0.1.15 release.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from .coordinator import ReefDataCoordinator

_LOGGER = logging.getLogger(__name__)


def derive_calibration_entity(kh_source_entity_id: str | None) -> str | None:
    """Auto-derive the `last_calibration` entity from the KH source.

    KH-keeper-bridge naming convention: every sensor lives under
    `sensor.kh_keeper_<key>` so swapping the last segment yields the
    sibling entity. Returns None if the input doesn't follow the
    pattern (e.g. the user pointed the KH source at a different
    integration's entity).
    """
    if not kh_source_entity_id:
        return None
    if not kh_source_entity_id.startswith("sensor.kh_keeper_"):
        return None
    return "sensor.kh_keeper_last_calibration"


class CalibrationEventListener:
    """One instance per ConfigEntry. Reloads cleanly because
    __init__.py recreates the listener on options change."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ReefDataCoordinator,
        entity_id: str | None,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entity_id = entity_id
        self._unsub: Any = None
        # Track the last processed ts so a duplicate-fire (e.g. HA
        # republishes the retained MQTT topic on connect) is a no-op
        # before we even hit the coordinator's de-dupe.
        self._last_processed_ts: str | None = None

    async def async_start(self) -> None:
        if not self._entity_id:
            _LOGGER.debug("No calibration source entity configured — listener idle")
            return
        _LOGGER.info(
            "Calibration listener tracking %s", self._entity_id,
        )
        self._unsub = async_track_state_change_event(
            self._hass, [self._entity_id], self._handle_state_change,
        )
        # Pick up the current state on startup so we record any events
        # that fired while reeftank was down (or that arrived from the
        # bridge's retained `last_calibration` topic before we started
        # listening).
        state = self._hass.states.get(self._entity_id)
        if state is not None:
            await self._process(state, reason="initial")

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_state_change(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        self._hass.async_create_task(
            self._process(new_state, reason="state_change")
        )

    async def _process(self, state: State, *, reason: str) -> None:
        if state.state in {"unknown", "unavailable", "none", ""}:
            return
        attrs = dict(state.attributes or {})
        # The bridge publishes the event payload in attributes; pull
        # the canonical timestamp from there (state is the same value
        # but normalised by HA).
        ts = attrs.get("ts") or state.state
        if not ts:
            _LOGGER.debug(
                "Calibration entity %s state had no ts attribute; skipping",
                self._entity_id,
            )
            return
        if ts == self._last_processed_ts:
            return
        event = {
            "ts": ts,
            "prev": attrs.get("prev"),
            "new": attrs.get("new"),
            "delta": attrs.get("delta"),
            "source": attrs.get("source") or "device",
            "hanna_value": attrs.get("hanna_value"),
            "serial": attrs.get("serial") or "unknown",
        }
        if event["prev"] is None or event["new"] is None:
            _LOGGER.debug(
                "Calibration entity %s attrs missing prev/new (%s); "
                "ignoring — likely a stale or non-bridge payload",
                self._entity_id, attrs,
            )
            return
        try:
            await self._coordinator.async_record_calibration_event(event)
            self._last_processed_ts = ts
            _LOGGER.debug(
                "Calibration event processed (%s): %s → %s (Δ %s) src=%s",
                reason, event["prev"], event["new"], event["delta"],
                event["source"],
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to record calibration event from %s: %s",
                self._entity_id, exc,
            )
