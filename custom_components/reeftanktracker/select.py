"""Select entities for tank context (habitat & problem).

These are user-facing dropdowns so the active habitat / problem can be
changed from any Lovelace card without writing automations or calling
services. The underlying state is held by the coordinator — selecting a
new option just routes through `coordinator.async_set_habitat()`.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    HABITATS,
    PROBLEMS,
    SIGNAL_ADVISOR_FORM_CHANGED,
    SIGNAL_HABITAT_CHANGED,
    TEST_METHODS,
)
from .coordinator import ReefDataCoordinator


def _device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, DEVICE_ID)},
        name=DEVICE_NAME,
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ReefDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        TankHabitatSelect(coordinator),
        TankProblemSelect(coordinator),
        TankMethodSelect(coordinator),
        AdvisorDemandDirectionSelect(coordinator),
    ])


class _TankSelectBase(SelectEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()

    def __init__(self, coordinator: ReefDataCoordinator) -> None:
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_HABITAT_CHANGED, self.async_write_ha_state
            )
        )


class TankHabitatSelect(_TankSelectBase):
    """Pick the current tank habitat (SPS Outer Reef, LPS Dominant, etc.)."""

    _attr_unique_id = "reef_tank_habitat_select"
    _attr_name = "Habitat"
    _attr_icon = "mdi:waves"
    _attr_options = list(HABITATS)

    @property
    def current_option(self) -> str | None:
        return self._coordinator.tank.get("habitat")

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_habitat(habitat=option)


class TankProblemSelect(_TankSelectBase):
    """Pick the current tank problem (or None)."""

    _attr_unique_id = "reef_tank_problem_select"
    _attr_name = "Problem"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_options = list(PROBLEMS)

    @property
    def current_option(self) -> str | None:
        return self._coordinator.tank.get("problem")

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_habitat(problem=option)


class TankMethodSelect(_TankSelectBase):
    """Active test method for the current session.

    Used as the default method label when the user types into a number
    entry entity. Set this once per session (e.g. "Hanna ULR" before a
    Hanna run, "Salifert" before a Salifert run) and all auto-saved
    entries during that session will be tagged with the selected method.

    Selecting "Unspecified" leaves method=None on entries — useful when
    you're not tracking which kit you used.
    """

    _attr_unique_id = "reef_tank_method_select"
    _attr_name = "Active Test Method"
    _attr_icon = "mdi:flask-outline"
    _attr_options = list(TEST_METHODS)

    @property
    def current_option(self) -> str | None:
        return self._coordinator.tank.get("method")

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_habitat(method=option)


class AdvisorDemandDirectionSelect(SelectEntity):
    """Inline form select for the demand-change `expected_direction`.

    State is held in the coordinator's advisor form blob so the user's
    selection persists across HA restarts and is readable via template
    in the dashboard's "Log demand change" submit button.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_unique_id = "reef_advisor_form_demand_direction"
    _attr_name = "Demand change direction"
    _attr_icon = "mdi:swap-vertical"
    _attr_options = ["increase", "decrease", "unknown"]
    _attr_device_info = _device_info()

    def __init__(self, coordinator: ReefDataCoordinator) -> None:
        self._coordinator = coordinator

    @property
    def current_option(self) -> str | None:
        v = self._coordinator.advisor_form_value("demand_direction")
        return str(v) if v in self._attr_options else "unknown"

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.async_set_advisor_form_value(
            "demand_direction", option,
        )
        # Force HA to re-read current_option and update state. Without
        # this, non-polling entities (`_attr_should_poll = False`) can
        # leave stale state in HA's state machine after a service call.
        self.async_write_ha_state()
