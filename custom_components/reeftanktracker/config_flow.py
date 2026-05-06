"""Config + Options flow for Reef Tank Tracker.

Setup is a one-click confirm (no options at install time).

The Options flow exposes a per-parameter "auto source sensor" picker.
For each input parameter (KH, pH, Temperature, etc.) the user can pick
a HA sensor whose state will feed the integration's `latest` value
when no manual reading is fresh enough. Leaving a field empty means
the parameter is manual / ICP-only.

Defaults: parameters with `auto_source` declared in `parameters.py`
are pre-filled with that entity ID. The user can confirm, change, or
clear them.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
)

from .const import DOMAIN
from .parameters import INPUT_PARAMETERS

# Options dict keys for each parameter's auto source sensor
OPT_AUTO_SOURCE_PREFIX = "auto_source_"


def auto_source_key(param_id: str) -> str:
    return f"{OPT_AUTO_SOURCE_PREFIX}{param_id}"


class ReefTankTrackerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        # Single instance only.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title="Reef Tank Tracker", data={})

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "ReefTankOptionsFlow":
        return ReefTankOptionsFlow(entry)


class ReefTankOptionsFlow(config_entries.OptionsFlow):
    """Per-parameter auto-source sensor picker.

    Saved into `entry.options` as `auto_source_<param_id>: <entity_id>`.
    The sensor platform reads this map at startup and uses each entity
    as the parameter's "auto" fallback / drift comparison source.
    """

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # Strip empty strings so we don't clutter the options dict.
            cleaned = {k: v for k, v in user_input.items() if v}
            return self.async_create_entry(title="", data=cleaned)

        # Build a schema row per input parameter.
        # Default: existing option, or the hardcoded parameter default,
        # or empty if neither.
        schema_dict: dict[Any, Any] = {}
        for p in INPUT_PARAMETERS:
            key = auto_source_key(p["id"])
            current = self._entry.options.get(key, p.get("auto_source") or "")
            schema_dict[vol.Optional(key, default=current)] = EntitySelector(
                EntitySelectorConfig(domain=["sensor", "input_number"])
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "param_count": str(len(INPUT_PARAMETERS)),
            },
        )
