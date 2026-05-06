"""Config flow for Reef Tank Tracker.

Single-instance integration. The setup form just confirms — no options
to collect at install time. Tank name, habitat, and problem are managed
via the `set_habitat` service / Lovelace UI after install.
"""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


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
