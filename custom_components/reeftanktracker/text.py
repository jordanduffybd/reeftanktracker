"""Text-input entities backed by coordinator form state.

These exist solely to give the Alk Advisor dashboard inline text inputs
for free-form fields (water-change notes, demand-change reason).
The submit button on the dashboard reads each entity's state via
template — `{{ states('text.reef_advisor_form_wc_notes') }}` — and
passes it to the underlying service.

State persists across reloads via `coordinator.async_set_advisor_form_value`.
"""
from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_ID, DEVICE_MANUFACTURER, DEVICE_MODEL, DEVICE_NAME,
    DOMAIN, SIGNAL_ADVISOR_FORM_CHANGED,
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
        AdvisorFormText(
            coordinator,
            unique_id="reef_advisor_form_wc_notes",
            name="Water change notes",
            icon="mdi:note-text-outline",
            form_key="wc_notes",
        ),
        AdvisorFormText(
            coordinator,
            unique_id="reef_advisor_form_demand_reason",
            name="Demand change reason",
            icon="mdi:swap-vertical",
            form_key="demand_reason",
        ),
    ])


class AdvisorFormText(TextEntity):
    """Free-text form input persisted in the coordinator's advisor form blob."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 255
    _attr_device_info = _device_info()

    def __init__(
        self,
        coordinator: ReefDataCoordinator,
        *,
        unique_id: str,
        name: str,
        icon: str,
        form_key: str,
    ) -> None:
        self._coordinator = coordinator
        self._form_key = form_key
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_icon = icon

    @property
    def native_value(self) -> str:
        v = self._coordinator.advisor_form_value(self._form_key)
        return str(v) if v is not None else ""

    async def async_set_value(self, value: str) -> None:
        # No dispatcher subscription — subscribing caused mid-edit
        # re-renders that scrambled in-flight text in adjacent fields.
        # Explicit write_ha_state because non-polling entities don't
        # auto-refresh after the service call returns.
        await self._coordinator.async_set_advisor_form_value(
            self._form_key, value,
        )
        self.async_write_ha_state()
