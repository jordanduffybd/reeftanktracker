"""Subscribe to upstream sensors and record auto readings.

Each parameter may declare an `auto_source` sensor (e.g. KH Keeper for KH,
ReefBeat probe for temperature). When that sensor's state changes, we
record a `source="auto"` reading via the coordinator so:

  - the parameter's history graph accumulates auto data alongside manual
    runs, going through HA's long-term statistics,
  - the Latest sensor reflects the freshest known value regardless of
    source (manual readings still win when they're more recent because
    `latest_reading` orders by sample_taken_at),
  - the Drift sensor re-renders on every upstream tick (since recording
    a reading dispatches `SIGNAL_READING_RECORDED`).

To avoid spamming high-cadence probes (pH / temperature) into the
readings list, we apply two filters per parameter:
  - significance: skip if |Δvalue| < parameter `step`
  - min interval: skip if the previous auto record was <5 min ago
Both are bypassed if the previous auto record is >24 h old, so we always
keep at least one reading per day per configured parameter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import SOURCE_AUTO
from .coordinator import ReefDataCoordinator
from .parameters import INPUT_PARAMETERS, ParameterDef, get_parameter

_LOGGER = logging.getLogger(__name__)

MIN_INTERVAL = timedelta(minutes=5)
MAX_QUIET = timedelta(hours=24)
_IGNORED_STATES = {"unknown", "unavailable", "none", ""}


class AutoSourceListener:
    """Records auto readings when configured upstream sensors change.

    One instance per ConfigEntry. The integration reloads on options
    change (see `_async_update_listener` in __init__.py), so this object
    is recreated cleanly whenever the auto-source map changes.
    """

    def __init__(self, hass: HomeAssistant, coordinator: ReefDataCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        # entity_id → list of params that watch it (handles the unlikely
        # case where two parameters share the same sensor)
        self._entity_to_params: dict[str, list[ParameterDef]] = {}
        # param_id → (last_recorded_value, last_recorded_at) — in-memory
        # throttle state. Resets on reload; the coordinator's stored
        # readings are the durable record.
        self._last_record: dict[str, tuple[float, datetime]] = {}
        self._unsub: Any = None

    async def async_start(self) -> None:
        self._build_entity_map()
        if not self._entity_to_params:
            _LOGGER.debug("No auto-sources configured — listener idle")
            return

        entity_ids = list(self._entity_to_params.keys())
        _LOGGER.info(
            "Auto-source listener tracking %d entities: %s",
            len(entity_ids), ", ".join(entity_ids),
        )
        self._unsub = async_track_state_change_event(
            self._hass, entity_ids, self._handle_state_change,
        )

        # Capture the current state of each tracked entity so the user
        # gets at least one auto reading immediately on integration load
        # rather than having to wait for the next upstream tick.
        for entity_id, params in self._entity_to_params.items():
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            for param in params:
                await self._maybe_record(param, state, reason="initial")

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _build_entity_map(self) -> None:
        for param in INPUT_PARAMETERS:
            entity_id = self._coordinator.get_auto_source(param["id"])
            if not entity_id:
                continue
            self._entity_to_params.setdefault(entity_id, []).append(param)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Forward HA state-change events to the recorder."""
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        params = self._entity_to_params.get(new_state.entity_id, [])
        for param in params:
            # Schedule the async record but don't await — state-change
            # callbacks must return synchronously.
            self._hass.async_create_task(
                self._maybe_record(param, new_state, reason="state_change")
            )

    async def _maybe_record(
        self, param: ParameterDef, state: State, *, reason: str,
    ) -> None:
        """Apply throttle + significance filters, then record if worth it."""
        if state.state in _IGNORED_STATES:
            return
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return

        sample_at = state.last_updated or datetime.now(timezone.utc)
        if not self._should_record(param, value, sample_at):
            return

        try:
            await self._coordinator.async_record_reading(
                parameter=param["id"],
                value=value,
                unit=param.get("unit"),
                method=None,
                source=SOURCE_AUTO,
                sample_taken_at=sample_at.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to record auto reading for %s from %s: %s",
                param["id"], state.entity_id, exc,
            )
            return
        self._last_record[param["id"]] = (value, sample_at)
        _LOGGER.debug(
            "Auto reading recorded (%s): %s=%s from %s",
            reason, param["id"], value, state.entity_id,
        )

    def _should_record(
        self, param: ParameterDef, value: float, sample_at: datetime,
    ) -> bool:
        last = self._last_record.get(param["id"])
        if last is None:
            return True
        last_value, last_at = last

        # Always record if quiet for too long — keeps history dense even
        # when the upstream value is stable.
        if sample_at - last_at >= MAX_QUIET:
            return True

        # Throttle rapid ticks regardless of value — mainly for pH/temp
        # probes that publish every few seconds.
        if sample_at - last_at < MIN_INTERVAL:
            return False

        # Require a value change at least as large as the parameter's
        # step (display precision floor). For KH that's 0.05 dKH, for pH
        # 0.01, for temperature 0.1 °C.
        threshold = float(param.get("step", 0))
        if abs(value - last_value) < threshold:
            return False
        return True


def get_auto_source_step(param_id: str) -> float | None:
    """Helper for tests / introspection — the configured significance step."""
    p = get_parameter(param_id)
    if p is None:
        return None
    return float(p.get("step", 0)) or None
