"""Auto-install + regenerate the Reef Tank Lovelace dashboard.

On first integration setup we create a new dashboard at
`/reef-tank-tracker` populated with cards for every parameter, an entry
section, latest values, and cadence diagnostics. The dashboard config is
written via HA's `Store` helpers — same mechanism HA uses internally —
so it's editable after install (the user can tweak cards manually
without losing them on integration reload).

A `user_removed_dashboard` flag in the coordinator's storage prevents
re-creation if the user explicitly removes the dashboard. To bring it
back, call `reeftanktracker.regenerate_dashboard` (which clears the
flag and writes a fresh layout).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .coordinator import ReefDataCoordinator
from .parameters import INPUT_PARAMETERS

_LOGGER = logging.getLogger(__name__)

# HA's storage-mode Lovelace uses these conventions:
#   .storage/lovelace_dashboards    — registry of all custom dashboards
#   .storage/lovelace.<url_path>    — content of each dashboard
DASHBOARD_URL_PATH = "reef-tank-tracker"
DASHBOARD_ID = "reef_tank_tracker"
DASHBOARD_STORE_KEY = f"lovelace.{DASHBOARD_ID}"
DASHBOARDS_REGISTRY_KEY = "lovelace_dashboards"


async def install_dashboard_if_missing(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> bool:
    """Create the Reef Tank dashboard if it doesn't already exist.

    Returns True if a fresh dashboard was created or refreshed.
    Returns False if skipped (user-removed flag, or HA Lovelace not
    in storage mode and we don't want to fight that).
    """
    if coordinator.is_dashboard_user_removed():
        _LOGGER.debug("Dashboard skipped — user previously removed it")
        return False

    # Add to dashboards registry so it appears in the sidebar.
    registry_store = Store(hass, 1, DASHBOARDS_REGISTRY_KEY)
    registry = (await registry_store.async_load()) or {"items": []}
    items = registry.get("items", [])

    if not any(d.get("url_path") == DASHBOARD_URL_PATH for d in items):
        items.append({
            "id": DASHBOARD_ID,
            "url_path": DASHBOARD_URL_PATH,
            "mode": "storage",
            "title": "Reef Tank",
            "icon": "mdi:fishbowl",
            "show_in_sidebar": True,
            "require_admin": False,
        })
        registry["items"] = items
        await registry_store.async_save(registry)
        _LOGGER.info("Registered Reef Tank dashboard at /%s", DASHBOARD_URL_PATH)

    # Write the dashboard contents.
    dashboard_store = Store(hass, 1, DASHBOARD_STORE_KEY)
    config = build_dashboard_config()
    await dashboard_store.async_save({"config": config})
    _LOGGER.info("Wrote Reef Tank dashboard config (%d views)", len(config["views"]))
    return True


async def regenerate_dashboard(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> None:
    """Force a fresh dashboard rebuild and clear any user-removed flag."""
    await coordinator.async_set_dashboard_user_removed(False)
    await install_dashboard_if_missing(hass, coordinator)


# ---------------------------------------------------------------------------
# Layout generators
# ---------------------------------------------------------------------------
def build_dashboard_config() -> dict[str, Any]:
    """Build the full Reef Tank dashboard config from the parameter list."""
    return {
        "title": "Reef Tank",
        "views": [
            _build_test_session_view(),
            _build_overview_view(),
            _build_diagnostics_view(),
        ],
    }


def _build_test_session_view() -> dict[str, Any]:
    """Hanna run view: per-parameter auto-saving entry rows."""
    entry_rows = [
        {
            "entity": f"number.reef_tank_{p['id']}_entry",
            "name": f"{p['name']} ({p['unit']})",
            "secondary_info": "last-updated",
        }
        for p in INPUT_PARAMETERS
    ]
    latest_tiles = [
        {
            "type": "tile",
            "entity": f"sensor.reef_tank_{p['id']}_latest",
            "name": p["name"],
            "vertical": True,
            "state_content": ["state", "last-changed"],
        }
        for p in INPUT_PARAMETERS
    ]
    return {
        "title": "Test Session",
        "path": "test-session",
        "icon": "mdi:flask",
        "type": "sections",
        "max_columns": 3,
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Tank context", "icon": "mdi:waves"},
                    {
                        "type": "tile",
                        "entity": "sensor.reef_tank_tank_habitat",
                        "name": "Habitat",
                        "icon": "mdi:waves",
                    },
                    {
                        "type": "tile",
                        "entity": "sensor.reef_tank_tank_problem",
                        "name": "Problem",
                        "icon": "mdi:alert-circle-outline",
                    },
                ],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Test Session — auto-saves on entry",
                     "icon": "mdi:flask"},
                    {
                        "type": "entities",
                        "title": "Type a value, it's recorded immediately",
                        "show_header_toggle": False,
                        "entities": entry_rows,
                    },
                ],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Latest values",
                     "icon": "mdi:chart-line"},
                    *latest_tiles,
                ],
            },
        ],
    }


def _build_overview_view() -> dict[str, Any]:
    """At-a-glance current values + days-since-test."""
    cards = []
    for p in INPUT_PARAMETERS:
        cards.append({
            "type": "tile",
            "entity": f"sensor.reef_tank_{p['id']}_latest",
            "name": p["name"],
            "vertical": True,
        })
    cards_days = [
        {
            "type": "tile",
            "entity": f"sensor.reef_tank_{p['id']}_days_since",
            "name": f"{p['name']} last tested",
            "vertical": True,
        }
        for p in INPUT_PARAMETERS
    ]
    return {
        "title": "Overview",
        "path": "overview",
        "icon": "mdi:fishbowl",
        "type": "sections",
        "max_columns": 4,
        "sections": [
            {
                "type": "grid",
                "column_span": 4,
                "cards": [
                    {"type": "heading", "heading": "Latest values",
                     "icon": "mdi:waves"},
                    *cards,
                ],
            },
            {
                "type": "grid",
                "column_span": 4,
                "cards": [
                    {"type": "heading", "heading": "Days since last test",
                     "icon": "mdi:calendar-clock"},
                    *cards_days,
                ],
            },
        ],
    }


def _build_diagnostics_view() -> dict[str, Any]:
    """Method, drift, and timestamp diagnostics for each parameter."""
    rows = []
    for p in INPUT_PARAMETERS:
        rows.append({
            "type": "entities",
            "title": p["name"],
            "show_header_toggle": False,
            "entities": [
                {"entity": f"sensor.reef_tank_{p['id']}_latest", "name": "Latest"},
                {"entity": f"sensor.reef_tank_{p['id']}_latest_method",
                 "name": "Method"},
                {"entity": f"sensor.reef_tank_{p['id']}_latest_at",
                 "name": "Sample time"},
                {"entity": f"sensor.reef_tank_{p['id']}_days_since",
                 "name": "Days since manual test"},
                {"entity": f"sensor.reef_tank_{p['id']}_drift",
                 "name": "Drift (manual − auto)"},
            ],
        })
    return {
        "title": "Diagnostics",
        "path": "diagnostics",
        "icon": "mdi:bug-outline",
        "type": "sections",
        "max_columns": 3,
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Per-parameter diagnostics",
                     "icon": "mdi:test-tube"},
                    *rows,
                ],
            },
        ],
    }
