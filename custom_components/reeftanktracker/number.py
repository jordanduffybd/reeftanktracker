"""Number entry entities for Reef Tank Tracker.

One `number.reef_<id>_entry` per input parameter. When the user changes
the value (in Lovelace, the developer-tools Set State, or via service),
we immediately record a manual reading via the coordinator. No save
button. The entered value stays visible afterwards so it's clear what
was last entered, and the user can edit by typing a new value.

Behaviour:
  - the displayed state is the most recent MANUAL value
  - on async_set_native_value, record a reading at the current moment
  - the new value becomes the new displayed state via the dispatcher

If the user pulls down the value to e.g. 0 to clear it, that's still
treated as a real reading — they shouldn't accidentally reset entries,
so we don't have a "no value" state. To skip a parameter, just leave it
alone.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    SIGNAL_READING_RECORDED,
    SOURCE_MANUAL,
)
from .coordinator import ReefDataCoordinator
from .parameters import INPUT_PARAMETERS, ParameterDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ReefDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [ReefEntryNumber(coordinator, p) for p in INPUT_PARAMETERS]
    async_add_entities(entities)


class ReefEntryNumber(NumberEntity):
    """A number whose set_value records a manual reading."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    # Always available, even when no reading has been entered yet —
    # otherwise HA marks the entity unavailable and disables the input
    # in the entity-detail modal, which makes it impossible to set the
    # very first value from there. We're tracking "do we have data?"
    # via native_value=None instead.
    _attr_available = True
    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, DEVICE_ID)},
        name=DEVICE_NAME,
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
    )

    # Override available as a property too — some HA versions check the
    # property before the class attribute when rendering.
    @property
    def available(self) -> bool:
        return True

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        self._coordinator = coordinator
        self._param = param

        self._attr_unique_id = f"reef_{param['id']}_entry"
        # With has_entity_name + device, entity_id becomes
        # `number.reef_tank_<param>_entry`.
        self._attr_name = f"{param['name']} Entry"
        self._attr_icon = param.get("icon", "mdi:water")
        self._attr_native_unit_of_measurement = param.get("unit")
        self._attr_native_min_value = param.get("min", 0)
        self._attr_native_max_value = param.get("max", 100)
        self._attr_native_step = param.get("step", 0.1)

    @property
    def native_value(self) -> float | None:
        latest = self._coordinator.latest_manual(self._param["id"])
        if not latest:
            return None
        return latest["value"]

    async def async_set_native_value(self, value: float) -> None:
        """User typed a value — record it as a manual reading immediately.

        The method is taken from the session-level "Active Test Method"
        select (`select.reef_tank_active_test_method`). If that's set to
        "Unspecified" we leave method=None — better to record nothing
        than to stamp an inaccurate label.
        """
        await self._coordinator.async_record_reading(
            parameter=self._param["id"],
            value=float(value),
            unit=self._param.get("unit"),
            method=self._coordinator.active_method,
            source=SOURCE_MANUAL,
        )
        # async_record_reading dispatches SIGNAL_READING_RECORDED, which
        # we subscribe to below for state refresh.

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_reading(parameter: str | None = None) -> None:
            if parameter is None or parameter == self._param["id"]:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_READING_RECORDED, _on_reading
            )
        )
