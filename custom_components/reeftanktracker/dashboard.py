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

    Tries (in order) three strategies for registering the dashboard:
      1. `hass.data["lovelace"].dashboards_collection` — exists in some versions
      2. Importing `DashboardsCollection` directly and bootstrapping it —
         works on HA 2024+ where the collection isn't exposed via hass.data
      3. Direct file writes to `.storage/lovelace_dashboards` — last resort,
         requires HA restart to be picked up

    Step 2 covers the 2024+ era cleanly. Step 1 covers older versions.
    Step 3 is the safety net if HA changes the API again — at least the
    dashboard files are written and a restart fixes it.
    """
    if coordinator.is_dashboard_user_removed():
        _LOGGER.debug("Dashboard skipped — user previously removed it")
        return False

    config = build_dashboard_config()
    new_dashboard = {
        "url_path": DASHBOARD_URL_PATH,
        "mode": "storage",
        "title": "Reef Tank",
        "icon": "mdi:fishbowl",
        "show_in_sidebar": True,
        "require_admin": False,
    }

    # ────── Strategy 1: collection on hass.data["lovelace"]
    coll = _try_get_dashboards_collection(hass)
    if coll is not None:
        try:
            existing = [
                d for d in coll.async_items()
                if d.get("url_path") == DASHBOARD_URL_PATH
            ]
            if not existing:
                await coll.async_create_item(new_dashboard)
                _LOGGER.info(
                    "Registered Reef Tank dashboard via in-memory collection"
                )
            await _write_dashboard_content(hass, config)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "In-memory collection registration failed (%s); "
                "falling back to bootstrap strategy.", exc,
            )

    # ────── Strategy 2: bootstrap a DashboardsCollection from disk
    try:
        from homeassistant.components.lovelace.dashboard import (  # noqa: PLC0415
            DashboardsCollection,
        )
    except ImportError:
        _LOGGER.warning(
            "DashboardsCollection import failed; falling back to direct file write."
        )
    else:
        try:
            dc = DashboardsCollection(hass)
            await dc.async_load()
            existing = [
                d for d in dc.async_items()
                if d.get("url_path") == DASHBOARD_URL_PATH
            ]
            if not existing:
                await dc.async_create_item(new_dashboard)
                _LOGGER.info(
                    "Registered Reef Tank dashboard via bootstrapped collection — "
                    "RESTART Home Assistant to see it in the sidebar."
                )
            await _write_dashboard_content(hass, config)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Bootstrapped collection failed (%s); falling back to direct file write.",
                exc,
            )

    # ────── Strategy 3: write the storage files directly. Requires restart.
    try:
        registry_store = Store(hass, 1, "lovelace_dashboards")
        registry = (await registry_store.async_load()) or {"items": []}
        items = registry.get("items", [])
        if not any(d.get("url_path") == DASHBOARD_URL_PATH for d in items):
            items.append({"id": DASHBOARD_ID, **new_dashboard})
            registry["items"] = items
            await registry_store.async_save(registry)
        await _write_dashboard_content(hass, config)
        _LOGGER.warning(
            "Wrote Reef Tank dashboard to .storage directly (HA Lovelace "
            "API not accessible). RESTART Home Assistant to see it in the sidebar."
        )
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.exception("All dashboard install strategies failed")
        return False


def _try_get_dashboards_collection(hass: HomeAssistant) -> Any:
    """Probe hass.data for a dashboards_collection across HA versions."""
    lovelace_data = hass.data.get("lovelace")
    if not lovelace_data:
        return None
    # LovelaceData dataclass attribute (older HA)
    coll = getattr(lovelace_data, "dashboards_collection", None)
    if coll is not None:
        return coll
    # Dict-shaped (some HA versions)
    if isinstance(lovelace_data, dict):
        coll = lovelace_data.get("dashboards_collection")
        if coll is not None:
            return coll
    # Some versions stash it under a different key
    for k in ("dashboards", "dashboard_collection"):
        v = (getattr(lovelace_data, k, None)
             if not isinstance(lovelace_data, dict)
             else lovelace_data.get(k))
        # The DashboardsCollection class has async_create_item;
        # the dashboards dict (LovelaceConfig instances) does not.
        if v is not None and hasattr(v, "async_create_item"):
            return v
    return None


async def _write_dashboard_content(hass: HomeAssistant, config: dict[str, Any]) -> None:
    """Write the dashboard's view YAML to .storage/lovelace.<id>."""
    dashboard_store = Store(hass, 1, DASHBOARD_STORE_KEY)
    await dashboard_store.async_save({"config": config})
    _LOGGER.info(
        "Wrote Reef Tank dashboard config (%d views) to .storage/%s",
        len(config["views"]), DASHBOARD_STORE_KEY,
    )


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
