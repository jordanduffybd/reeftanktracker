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
from .alk_advisor import AlkAdvisorSnapshotter, DEFAULTS as ADVISOR_DEFAULTS
from .auto_source_listener import AutoSourceListener
from .config_flow import auto_source_key
from .coordinator import ReefDataCoordinator
from .dashboard import (
    diagnose_dashboard,
    regenerate_dashboard,
    schedule_install,
)
from .parameters import INPUT_PARAMETERS

SERVICE_REGENERATE_DASHBOARD = "regenerate_dashboard"
SERVICE_BACKFILL_STATISTICS = "backfill_statistics"
SERVICE_DIAGNOSE_DASHBOARD = "diagnose_dashboard"
SERVICE_ACK_ALK_RECOMMENDATION = "acknowledge_alk_recommendation"
SERVICE_DISMISS_ALK_RECOMMENDATION = "dismiss_alk_recommendation"
SERVICE_LOG_DEMAND_CHANGE = "log_demand_change"

# Re-imported from .const for service name strings
from .const import (
    SERVICE_ADD_SUPPLEMENT_PROFILE,
    SERVICE_REMOVE_SUPPLEMENT_PROFILE,
    SERVICE_LIST_SUPPLEMENT_PROFILES,
    SERVICE_LOG_WATER_CHANGE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "number", "select"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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

ACK_ALK_SCHEMA = vol.Schema({
    vol.Required("applied_value_mL"): vol.Coerce(float),
    vol.Optional("prev_value_mL"): vol.Coerce(float),
})

DISMISS_ALK_SCHEMA = vol.Schema({
    vol.Optional("suggested_value_mL", default=0.0): vol.Coerce(float),
})

LOG_DEMAND_CHANGE_SCHEMA = vol.Schema({
    vol.Optional("parameter", default="kh"): cv.string,
    vol.Required("reason"): cv.string,
    vol.Optional("expected_direction", default="unknown"): vol.In(
        ["increase", "decrease", "unknown"]
    ),
    vol.Optional("magnitude_hint_pct"): vol.Coerce(float),
})

ADD_SUPPLEMENT_PROFILE_SCHEMA = vol.Schema({
    vol.Required("label"): cv.string,
    vol.Required("eff_dkh_per_mL_per_100L"): vol.All(
        vol.Coerce(float), vol.Range(min=0.001, max=5.0),
    ),
    vol.Optional("label_patterns", default=[]): vol.All(
        cv.ensure_list, [cv.string],
    ),
    vol.Optional("notes"): cv.string,
})

REMOVE_SUPPLEMENT_PROFILE_SCHEMA = vol.Schema({
    vol.Required("id"): cv.string,
})

LOG_WATER_CHANGE_SCHEMA = vol.Schema({
    vol.Optional("parameter", default="kh"): cv.string,
    vol.Required("percent"): vol.All(
        vol.Coerce(float), vol.Range(min=0.1, max=100.0),
    ),
    vol.Optional("salt_mix_kh"): vol.Coerce(float),
    vol.Optional("notes"): cv.string,
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


def _resolve_advisor_config(entry: ConfigEntry) -> dict[str, Any]:
    """Pull all advisor-section options into a flat dict.

    Only pass through keys the advisor knows about (everything in
    ADVISOR_DEFAULTS). Empty values fall back to the algorithm's defaults.
    """
    out: dict[str, Any] = {}
    for key in ADVISOR_DEFAULTS:
        if key in entry.options:
            out[key] = entry.options[key]
    return out


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change so new auto-sources take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ReefDataCoordinator(hass)
    await coordinator.async_load()
    coordinator.set_auto_sources(_resolve_auto_sources(entry))
    coordinator.set_advisor_config(_resolve_advisor_config(entry))
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # When the user changes options, reload the entry so the new
    # auto-source map is picked up by all sensors.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register services. They're registered on the first entry only —
    # since we're single-instance, that's a non-issue.
    await _async_register_services(hass, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Subscribe to upstream auto-source sensors — every state change
    # there can become a recorded reading. Started after platforms so
    # the entities exist when the listener captures initial state.
    listener = AutoSourceListener(hass, coordinator)
    await listener.async_start()
    entry.async_on_unload(listener.async_stop)

    # Daily snapshotter for the alk advisor. Captures one snapshot/day
    # at the configured local time regardless of whether the advisor is
    # enabled — keeps history ready for when the user turns it on.
    snapshotter = AlkAdvisorSnapshotter(hass, coordinator)
    await snapshotter.async_start()
    entry.async_on_unload(snapshotter.async_stop)

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
                SERVICE_ACK_ALK_RECOMMENDATION,
                SERVICE_DISMISS_ALK_RECOMMENDATION,
                SERVICE_LOG_DEMAND_CHANGE,
                SERVICE_ADD_SUPPLEMENT_PROFILE,
                SERVICE_REMOVE_SUPPLEMENT_PROFILE,
                SERVICE_LIST_SUPPLEMENT_PROFILES,
                SERVICE_LOG_WATER_CHANGE,
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

    async def handle_ack_alk(call: ServiceCall) -> None:
        applied = float(call.data["applied_value_mL"])
        prev = call.data.get("prev_value_mL")
        if prev is None:
            # If the user didn't tell us their previous dose, infer from
            # the last persisted snapshot. This is best-effort.
            snaps = coordinator.advisor_snapshots("kh")
            for s in reversed(snaps):
                if s.get("dose_mL") is not None:
                    prev = float(s["dose_mL"])
                    break
        await coordinator.async_record_advisor_acknowledgment(
            "kh",
            applied_value_mL=applied,
            prev_value_mL=float(prev) if prev is not None else 0.0,
        )

    async def handle_dismiss_alk(call: ServiceCall) -> None:
        await coordinator.async_record_advisor_dismissal(
            "kh",
            suggested_value_mL=float(call.data.get("suggested_value_mL", 0.0)),
        )

    async def handle_log_demand_change(call: ServiceCall) -> None:
        await coordinator.async_record_advisor_demand_change(
            call.data.get("parameter", "kh"),
            reason=call.data["reason"],
            expected_direction=call.data.get("expected_direction", "unknown"),
            magnitude_hint_pct=call.data.get("magnitude_hint_pct"),
        )

    async def handle_add_supplement_profile(call: ServiceCall) -> None:
        entry = await coordinator.async_add_supplement_profile(
            label=call.data["label"],
            eff_dkh_per_mL_per_100L=call.data["eff_dkh_per_mL_per_100L"],
            label_patterns=call.data.get("label_patterns"),
            notes=call.data.get("notes"),
        )
        _LOGGER.warning(
            "Added supplement profile id=%s label=%r eff=%s patterns=%s",
            entry["id"], entry["label"],
            entry["eff_dkh_per_mL_per_100L"], entry["label_patterns"],
        )

    async def handle_remove_supplement_profile(call: ServiceCall) -> None:
        await coordinator.async_remove_supplement_profile(call.data["id"])
        _LOGGER.warning("Removed supplement profile id=%s", call.data["id"])

    async def handle_list_supplement_profiles(call: ServiceCall) -> None:
        # Local import to avoid hoisting alk_advisor's HA-helper imports
        # into the top of __init__.py.
        from .alk_advisor import all_profiles
        merged = all_profiles(coordinator)
        user_ids = {p["id"] for p in coordinator.supplement_profiles}
        lines = ["Supplement profiles (BUILTIN unless tagged USER):"]
        for pid, prof in merged.items():
            tag = "USER" if pid in user_ids else "BUILTIN"
            eff = prof["eff_dkh_per_mL_per_100L"]
            eff_s = f"{eff} dKH/mL/100L" if eff is not None else "(sentinel)"
            lines.append(f"  [{tag}] {pid} — {prof['label']} — {eff_s}")
        _LOGGER.warning("\n".join(lines))

    async def handle_log_water_change(call: ServiceCall) -> None:
        await coordinator.async_record_water_change(
            call.data.get("parameter", "kh"),
            percent=call.data["percent"],
            salt_mix_kh=call.data.get("salt_mix_kh"),
            notes=call.data.get("notes"),
        )

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
        hass.services.async_register(
            DOMAIN, SERVICE_ACK_ALK_RECOMMENDATION, handle_ack_alk,
            schema=ACK_ALK_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_DISMISS_ALK_RECOMMENDATION, handle_dismiss_alk,
            schema=DISMISS_ALK_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LOG_DEMAND_CHANGE, handle_log_demand_change,
            schema=LOG_DEMAND_CHANGE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_ADD_SUPPLEMENT_PROFILE, handle_add_supplement_profile,
            schema=ADD_SUPPLEMENT_PROFILE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_REMOVE_SUPPLEMENT_PROFILE, handle_remove_supplement_profile,
            schema=REMOVE_SUPPLEMENT_PROFILE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LIST_SUPPLEMENT_PROFILES, handle_list_supplement_profiles,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LOG_WATER_CHANGE, handle_log_water_change,
            schema=LOG_WATER_CHANGE_SCHEMA,
        )
