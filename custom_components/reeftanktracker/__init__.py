"""Reef Tank Tracker — top-level integration setup.

Single-instance integration. The config flow creates one entry; on
async_setup_entry we:
  - load persistent state via ReefDataCoordinator
  - register services (record_reading, add_inventory, set_habitat, ...)
  - forward to sensor and number platforms
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    HABITATS,
    PROBLEMS,
    INVENTORY_CATEGORIES,
    SERVICE_ADD_INVENTORY,
    SERVICE_IMPORT_ICP,
    SERVICE_RECORD_READING,
    SERVICE_REMOVE_INVENTORY,
    SERVICE_SET_HABITAT,
    SOURCE_AUTO,
    SOURCE_ICP,
    SOURCE_MANUAL,
)
from .config_flow import OPT_AUTO_SOURCE_PREFIX, auto_source_key
from .coordinator import ReefDataCoordinator
from .dashboard import (
    diagnose_dashboard,
    install_dashboard_if_missing,
    regenerate_dashboard,
    schedule_install,
)
from .parameters import INPUT_PARAMETERS

SERVICE_REGENERATE_DASHBOARD = "regenerate_dashboard"
SERVICE_BACKFILL_STATISTICS = "backfill_statistics"
SERVICE_DIAGNOSE_DASHBOARD = "diagnose_dashboard"

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "number", "select"]


# ---------------------------------------------------------------------------
# Service schemas
# ---------------------------------------------------------------------------
RECORD_READING_SCHEMA = vol.Schema({
    vol.Required("parameter"): cv.string,
    vol.Required("value"): vol.Coerce(float),
    vol.Optional("unit"): cv.string,
    vol.Optional("method"): cv.string,
    vol.Optional("source", default=SOURCE_MANUAL): vol.In(
        [SOURCE_MANUAL, SOURCE_AUTO, SOURCE_ICP]
    ),
    vol.Optional("sample_taken_at"): cv.string,
    vol.Optional("test_id"): cv.string,
    vol.Optional("notes"): cv.string,
})

ADD_INVENTORY_SCHEMA = vol.Schema({
    vol.Required("name"): cv.string,
    vol.Required("category"): vol.In(INVENTORY_CATEGORIES),
    vol.Optional("type"): cv.string,
    vol.Optional("added_at"): cv.string,
    vol.Optional("count", default=1): vol.Coerce(int),
    vol.Optional("notes"): cv.string,
    vol.Optional("photo"): cv.string,
})

REMOVE_INVENTORY_SCHEMA = vol.Schema({
    vol.Required("id"): cv.string,
    vol.Optional("removed_at"): cv.string,
})

SET_HABITAT_SCHEMA = vol.Schema({
    vol.Optional("habitat"): vol.In(HABITATS),
    vol.Optional("problem"): vol.In(PROBLEMS),
    vol.Optional("method"): cv.string,
})

IMPORT_ICP_SCHEMA = vol.Schema({
    vol.Required("test_record"): dict,
})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """No yaml configuration; setup happens via config entry."""
    return True


def _resolve_auto_sources(entry: ConfigEntry) -> dict[str, str]:
    """Build the param_id → sensor entity_id map.

    Precedence:
      1. user-saved options (entry.options[auto_source_<param>])
      2. hardcoded default in parameters.py (param["auto_source"])
      3. nothing — manual / ICP-only
    """
    resolved: dict[str, str] = {}
    for p in INPUT_PARAMETERS:
        saved = entry.options.get(auto_source_key(p["id"]))
        default = p.get("auto_source")
        chosen = saved if saved else default
        if chosen:
            resolved[p["id"]] = chosen
    return resolved


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change so new auto-sources take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ReefDataCoordinator(hass)
    await coordinator.async_load()
    coordinator.set_auto_sources(_resolve_auto_sources(entry))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # When the user changes options, reload the entry so the new
    # auto-source map is picked up by all sensors.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register services. They're registered on the first entry only —
    # since we're single-instance, that's a non-issue.
    await _async_register_services(hass, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Auto-install the Lovelace dashboard. Deferred until HA is started
    # so the lovelace integration's data is fully populated — calling
    # this from setup-entry directly is too early on a cold boot.
    schedule_install(hass, coordinator)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_RECORD_READING, SERVICE_ADD_INVENTORY,
                SERVICE_REMOVE_INVENTORY, SERVICE_SET_HABITAT,
                SERVICE_IMPORT_ICP, SERVICE_REGENERATE_DASHBOARD,
                SERVICE_BACKFILL_STATISTICS, SERVICE_DIAGNOSE_DASHBOARD,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_register_services(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> None:
    """Register service handlers. Idempotent."""

    async def handle_record_reading(call: ServiceCall) -> None:
        await coordinator.async_record_reading(
            parameter=call.data["parameter"],
            value=call.data["value"],
            unit=call.data.get("unit"),
            method=call.data.get("method"),
            source=call.data["source"],
            sample_taken_at=call.data.get("sample_taken_at"),
            test_id=call.data.get("test_id"),
            notes=call.data.get("notes"),
        )

    async def handle_add_inventory(call: ServiceCall) -> None:
        await coordinator.async_add_inventory(
            category=call.data["category"],
            name=call.data["name"],
            type=call.data.get("type"),
            added_at=call.data.get("added_at"),
            count=call.data.get("count", 1),
            notes=call.data.get("notes"),
            photo=call.data.get("photo"),
        )

    async def handle_remove_inventory(call: ServiceCall) -> None:
        await coordinator.async_remove_inventory(
            entry_id=call.data["id"],
            removed_at=call.data.get("removed_at"),
        )

    async def handle_set_habitat(call: ServiceCall) -> None:
        await coordinator.async_set_habitat(
            habitat=call.data.get("habitat"),
            problem=call.data.get("problem"),
            method=call.data.get("method"),
        )

    async def handle_import_icp(call: ServiceCall) -> None:
        await coordinator.async_record_icp_test(call.data["test_record"])

    async def handle_regenerate_dashboard(call: ServiceCall) -> None:
        await regenerate_dashboard(hass, coordinator)

    async def handle_backfill_statistics(call: ServiceCall) -> None:
        n = await coordinator.async_backfill_statistics(
            parameter=call.data.get("parameter") or None,
        )
        _LOGGER.info("Backfilled %d statistic points", n)

    async def handle_diagnose_dashboard(call: ServiceCall) -> None:
        # Logs the diagnostic at WARNING level so it's visible at the
        # default log level — no need to bump verbosity.
        await diagnose_dashboard(hass, coordinator)

    if not hass.services.has_service(DOMAIN, SERVICE_RECORD_READING):
        hass.services.async_register(
            DOMAIN, SERVICE_RECORD_READING, handle_record_reading,
            schema=RECORD_READING_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_ADD_INVENTORY, handle_add_inventory,
            schema=ADD_INVENTORY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_REMOVE_INVENTORY, handle_remove_inventory,
            schema=REMOVE_INVENTORY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SET_HABITAT, handle_set_habitat,
            schema=SET_HABITAT_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_ICP, handle_import_icp,
            schema=IMPORT_ICP_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_REGENERATE_DASHBOARD, handle_regenerate_dashboard,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_BACKFILL_STATISTICS, handle_backfill_statistics,
            schema=vol.Schema({vol.Optional("parameter"): cv.string}),
        )
        hass.services.async_register(
            DOMAIN, SERVICE_DIAGNOSE_DASHBOARD, handle_diagnose_dashboard,
        )
