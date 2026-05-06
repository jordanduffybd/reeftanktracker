"""Sensor entities for Reef Tank Tracker.

Per parameter we expose:
  sensor.reef_<id>_latest         — most recent recorded value
  sensor.reef_<id>_latest_method  — "Hanna ULR" / "Triton ICP" / etc.
  sensor.reef_<id>_latest_at      — sample timestamp (canonical)
  sensor.reef_<id>_days_since     — days since last MANUAL test
  sensor.reef_<id>_drift          — manual − auto, when both are fresh

Plus two tank-level sensors:
  sensor.reef_tank_habitat        — current habitat selection
  sensor.reef_tank_problem        — current problem selection
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    SIGNAL_HABITAT_CHANGED,
    SIGNAL_READING_RECORDED,
    SOURCE_MANUAL,
)


def _device_info() -> DeviceInfo:
    """Single shared device — all entities attach here so they're grouped
    in HA's UI under one row instead of 239 loose entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, DEVICE_ID)},
        name=DEVICE_NAME,
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
    )
from .coordinator import ReefDataCoordinator
from .parameters import ALL_PARAMETERS, ParameterDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ReefDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for param in ALL_PARAMETERS:
        entities.extend([
            ReefLatestSensor(coordinator, param),
            ReefLatestMethodSensor(coordinator, param),
            ReefLatestAtSensor(coordinator, param),
            ReefDaysSinceSensor(coordinator, param),
            ReefDriftSensor(coordinator, param),
        ])

    entities.extend([
        TankHabitatSensor(coordinator),
        TankProblemSensor(coordinator),
    ])

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Base sensor: subscribes to reading-recorded dispatch
# ---------------------------------------------------------------------------
class _ReefSensorBase(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef,
                 suffix: str, name: str) -> None:
        self._coordinator = coordinator
        self._param = param
        self._attr_unique_id = f"reef_{param['id']}_{suffix}"
        # Entity name combines parameter + variant (e.g. "KH Latest")
        # With has_entity_name=True the actual entity_id becomes
        # `sensor.reef_tank_<param>_<variant>`, derived from the device
        # name "Reef Tank" + the entity name.
        self._attr_name = f"{param['name']} {name}"
        self._attr_icon = param.get("icon", "mdi:water")

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_reading(parameter: str | None = None) -> None:
            # Re-render whether or not it's our parameter — cheap enough,
            # and tank-level changes don't pass a parameter at all.
            if parameter is None or parameter == self._param["id"]:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_READING_RECORDED, _on_reading
            )
        )


# ---------------------------------------------------------------------------
# Latest value
# ---------------------------------------------------------------------------
class ReefLatestSensor(_ReefSensorBase):
    """Most recent value for a parameter, by sample_taken_at.

    Uses Triton/manual readings authoritatively. If no manual or ICP value
    is present BUT an `auto_source` sensor exists, fall back to that
    sensor's current state.
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest", "Latest")
        self._attr_native_unit_of_measurement = param.get("unit")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if param.get("unit") == "°C":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def native_value(self) -> float | None:
        latest = self._coordinator.latest_reading(self._param["id"])
        if latest is not None:
            return _round(latest["value"], self._param.get("precision"))
        # Auto fallback — coordinator resolves user-configured override
        # over the hardcoded default in parameters.py.
        auto_src = self._coordinator.get_auto_source(self._param["id"])
        if auto_src:
            state = self.hass.states.get(auto_src)
            if state and state.state not in ("unknown", "unavailable", None):
                try:
                    return _round(float(state.state), self._param.get("precision"))
                except (ValueError, TypeError):
                    return None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self._coordinator.latest_reading(self._param["id"])
        auto_src = self._coordinator.get_auto_source(self._param["id"])
        if not latest:
            return {
                "source": "auto" if auto_src else None,
                "auto_source_entity": auto_src,
            }
        return {
            "source": latest["source"],
            "method": latest.get("method"),
            "sample_taken_at": latest["sample_taken_at"],
            "recorded_at": latest["recorded_at"],
            "test_id": latest.get("test_id"),
            "notes": latest.get("notes"),
            "auto_source_entity": auto_src,
        }


class ReefLatestMethodSensor(_ReefSensorBase):
    """The method used for the latest reading."""

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest_method", "Last Method")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        latest = self._coordinator.latest_reading(self._param["id"])
        if not latest:
            return None
        method = latest.get("method")
        if method:
            return method
        return latest.get("source", "").title() or None


class ReefLatestAtSensor(_ReefSensorBase):
    """When the latest reading was sampled."""

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest_at", "Last Sampled")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        latest = self._coordinator.latest_reading(self._param["id"])
        if not latest:
            return None
        try:
            return datetime.fromisoformat(latest["sample_taken_at"])
        except ValueError:
            return None


class ReefDaysSinceSensor(_ReefSensorBase):
    """Days since the last MANUAL test of this parameter.

    ICP imports don't reset this — the point is to track *when you last
    looked at it yourself*, since manual tests are the user's check-in.
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "days_since", "Days Since Test")
        self._attr_native_unit_of_measurement = "d"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        latest = self._coordinator.latest_manual(self._param["id"])
        if not latest:
            return None
        try:
            sampled = datetime.fromisoformat(latest["sample_taken_at"])
        except ValueError:
            return None
        delta = datetime.now(timezone.utc).astimezone() - sampled
        return max(0, delta.days)


class ReefDriftSensor(_ReefSensorBase):
    """Manual − auto, when both are fresh enough to compare.

    Returns None if there's no auto source for this parameter, or if
    either side is stale (>24 h).
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "drift", "Drift (manual − auto)")
        self._attr_native_unit_of_measurement = param.get("unit")
        self._attr_icon = "mdi:vector-difference"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        auto_src = self._coordinator.get_auto_source(self._param["id"])
        if not auto_src:
            return None
        manual = self._coordinator.latest_manual(self._param["id"])
        if not manual:
            return None
        try:
            sampled = datetime.fromisoformat(manual["sample_taken_at"])
        except ValueError:
            return None
        # Stale if older than a day
        age = datetime.now(timezone.utc).astimezone() - sampled
        if age.days > 1:
            return None
        state = self.hass.states.get(auto_src)
        if not state or state.state in ("unknown", "unavailable", None):
            return None
        try:
            auto_val = float(state.state)
        except (ValueError, TypeError):
            return None
        return _round(manual["value"] - auto_val, self._param.get("precision"))


# ---------------------------------------------------------------------------
# Tank-level sensors
# ---------------------------------------------------------------------------
class _TankSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_info = _device_info()

    def __init__(self, coordinator: ReefDataCoordinator) -> None:
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_HABITAT_CHANGED, self.async_write_ha_state
            )
        )


class TankHabitatSensor(_TankSensor):
    _attr_unique_id = "reef_tank_habitat"
    _attr_name = "Tank Habitat"
    _attr_icon = "mdi:waves"

    @property
    def native_value(self) -> str:
        return self._coordinator.tank.get("habitat")


class TankProblemSensor(_TankSensor):
    _attr_unique_id = "reef_tank_problem"
    _attr_name = "Tank Problem"
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self) -> str:
        return self._coordinator.tank.get("problem")


def _round(value: float | None, precision: int | None) -> float | None:
    if value is None:
        return None
    if precision is None:
        return value
    return round(value, precision)
