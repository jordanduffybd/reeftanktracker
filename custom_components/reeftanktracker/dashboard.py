"""Auto-install + regenerate the Reef Tank Lovelace dashboard.

We add a new storage-mode dashboard at `/reef-tank-tracker` populated
with cards for every parameter, an entry section, latest values, and
diagnostics. We use HA's Lovelace `dashboards_collection` API directly
(rather than writing storage files) so the registration is reflected
in HA's in-memory state immediately — no HA restart required.

Triggered via `homeassistant_started` event so it runs after the
lovelace integration is fully initialised. Setup-entry calls run too
early to safely touch lovelace internals.

A `user_removed_dashboard` flag in the coordinator's storage prevents
re-creation if the user explicitly deletes the dashboard. To bring it
back, call `reeftanktracker.regenerate_dashboard` (which clears the
flag and rebuilds the layout).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .coordinator import ReefDataCoordinator
from .parameters import INPUT_PARAMETERS

_LOGGER = logging.getLogger(__name__)

DASHBOARD_URL_PATH = "reef-tank-tracker"
DASHBOARD_ID = "reef_tank_tracker"
DASHBOARD_STORE_KEY = f"lovelace.{DASHBOARD_ID}"


def schedule_install(hass: HomeAssistant, coordinator: ReefDataCoordinator) -> None:
    """Delay dashboard install until HA is fully started.

    Called from `async_setup_entry`. If HA's already running (e.g. on
    config reload) we install immediately; otherwise we wait for the
    `homeassistant_started` event so the lovelace integration is up.
    """
    if hass.is_running:
        hass.async_create_task(install_dashboard_if_missing(hass, coordinator))
        return

    @callback
    def _on_started(_event: Any) -> None:
        hass.async_create_task(install_dashboard_if_missing(hass, coordinator))

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def install_dashboard_if_missing(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> bool:
    """Create the Reef Tank dashboard if it doesn't already exist.

    Returns True if a dashboard was created or its content rewritten.
    """
    if coordinator.is_dashboard_user_removed():
        _LOGGER.debug("Dashboard skipped — user previously removed it")
        return False

    # ─────────────────────────────────────────────────────────────────
    # Step 1: register the dashboard in HA's in-memory dashboards
    # collection (so it appears in the sidebar without restart).
    # ─────────────────────────────────────────────────────────────────
    lovelace_data = hass.data.get("lovelace")
    if not lovelace_data:
        _LOGGER.warning(
            "Lovelace integration not loaded yet — cannot auto-install "
            "dashboard. Call reeftanktracker.regenerate_dashboard later."
        )
        return False

    # `lovelace_data` may be a LovelaceData dataclass or a dict
    # depending on HA version. Handle both shapes.
    dashboards_collection = (
        getattr(lovelace_data, "dashboards_collection", None)
        or (lovelace_data.get("dashboards_collection")
            if isinstance(lovelace_data, dict) else None)
    )
    if dashboards_collection is None:
        _LOGGER.warning(
            "Could not find lovelace dashboards_collection on hass.data; "
            "skipping auto-install (HA Lovelace API shape changed?)."
        )
        return False

    # Check if already registered
    existing = [
        d for d in dashboards_collection.async_items()
        if d.get("url_path") == DASHBOARD_URL_PATH
    ]
    if not existing:
        try:
            await dashboards_collection.async_create_item({
                "url_path": DASHBOARD_URL_PATH,
                "mode": "storage",
                "title": "Reef Tank",
                "icon": "mdi:fishbowl",
                "show_in_sidebar": True,
                "require_admin": False,
            })
            _LOGGER.info("Registered Reef Tank dashboard at /%s", DASHBOARD_URL_PATH)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to register dashboard: %s", exc, exc_info=True)
            return False

    # ─────────────────────────────────────────────────────────────────
    # Step 2: write the dashboard's content via Store.
    # The lovelace integration reads `lovelace.<id>` from .storage on
    # demand when the dashboard is opened, so writing here is enough.
    # ─────────────────────────────────────────────────────────────────
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
