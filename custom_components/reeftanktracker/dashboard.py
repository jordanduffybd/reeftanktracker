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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
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

    Important: WARNING-level logs at every milestone so the user can see
    progress even with the default log filter. Without these the install
    looked silent on some setups.
    """
    _LOGGER.warning(
        "Reef Tank dashboard install starting (user_removed_flag=%s)",
        coordinator.is_dashboard_user_removed(),
    )
    if coordinator.is_dashboard_user_removed():
        _LOGGER.warning(
            "Dashboard skipped — user_removed_dashboard flag is set. "
            "Call reeftanktracker.regenerate_dashboard to clear and rebuild."
        )
        return False

    config = build_dashboard_config(hass)
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
    _LOGGER.warning("Strategy 1 (in-memory collection): %s",
                    "available" if coll else "NOT FOUND on hass.data['lovelace']")
    if coll is not None:
        try:
            existing = [
                d for d in coll.async_items()
                if d.get("url_path") == DASHBOARD_URL_PATH
            ]
            if existing:
                _LOGGER.warning(
                    "Dashboard already registered (Strategy 1) — refreshing content"
                )
            else:
                await coll.async_create_item(new_dashboard)
                _LOGGER.warning(
                    "✓ Strategy 1: Registered Reef Tank dashboard at /%s "
                    "via in-memory collection — should appear in sidebar immediately",
                    DASHBOARD_URL_PATH,
                )
            await _write_dashboard_content(hass, config)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Strategy 1 raised %s — falling back to bootstrap strategy",
                exc, exc_info=True,
            )

    # ────── Strategy 2: bootstrap a DashboardsCollection from disk
    DashboardsCollection = None
    for module_path in (
        "homeassistant.components.lovelace.dashboard",
        "homeassistant.components.lovelace",
    ):
        try:
            mod = __import__(module_path, fromlist=["DashboardsCollection"])
            DashboardsCollection = getattr(mod, "DashboardsCollection", None)
            if DashboardsCollection is not None:
                break
        except ImportError:
            continue

    _LOGGER.warning(
        "Strategy 2 (bootstrap DashboardsCollection): %s",
        "imported" if DashboardsCollection else "NOT FOUND",
    )

    if DashboardsCollection is not None:
        try:
            dc = DashboardsCollection(hass)
            await dc.async_load()
            existing = [
                d for d in dc.async_items()
                if d.get("url_path") == DASHBOARD_URL_PATH
            ]
            if existing:
                _LOGGER.warning(
                    "Dashboard already registered on disk (Strategy 2) — "
                    "refreshing content. URL: /%s", DASHBOARD_URL_PATH,
                )
            else:
                await dc.async_create_item(new_dashboard)
                _LOGGER.warning(
                    "✓ Strategy 2: Registered Reef Tank dashboard at /%s. "
                    "RESTART Home Assistant once to see it in the sidebar.",
                    DASHBOARD_URL_PATH,
                )
            await _write_dashboard_content(hass, config)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Strategy 2 raised %s — falling back to direct file write",
                exc, exc_info=True,
            )

    # ────── Strategy 3: write the storage files directly. Requires restart.
    _LOGGER.warning("Strategy 3 (direct file write): writing to .storage/lovelace_dashboards")
    try:
        registry_store = Store(hass, 1, "lovelace_dashboards")
        registry = (await registry_store.async_load()) or {"items": []}
        items = registry.get("items", [])
        if any(d.get("url_path") == DASHBOARD_URL_PATH for d in items):
            _LOGGER.warning(
                "Dashboard registry entry exists at /%s — only refreshing content",
                DASHBOARD_URL_PATH,
            )
        else:
            items.append({"id": DASHBOARD_ID, **new_dashboard})
            registry["items"] = items
            await registry_store.async_save(registry)
            _LOGGER.warning(
                "✓ Strategy 3: Added /%s to lovelace_dashboards registry. "
                "RESTART Home Assistant to see it in the sidebar.",
                DASHBOARD_URL_PATH,
            )
        await _write_dashboard_content(hass, config)
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.exception("All dashboard install strategies failed")
        return False


async def diagnose_dashboard(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> dict[str, Any]:
    """Dump what we know about the dashboard install state.

    Surfaced via the `reeftanktracker.diagnose_dashboard` service. Helps
    figure out which strategy (if any) succeeded and what's currently
    registered without spelunking through .storage manually.
    """
    info: dict[str, Any] = {
        "user_removed_flag": coordinator.is_dashboard_user_removed(),
        "expected_url": f"/{DASHBOARD_URL_PATH}",
    }

    lovelace_data = hass.data.get("lovelace")
    info["lovelace_data_type"] = type(lovelace_data).__name__
    info["lovelace_data_keys"] = (
        list(lovelace_data.keys()) if isinstance(lovelace_data, dict)
        else [a for a in dir(lovelace_data) if not a.startswith("_")][:20]
        if lovelace_data else []
    )

    coll = _try_get_dashboards_collection(hass)
    info["strategy_1_collection_found"] = coll is not None
    if coll is not None:
        info["strategy_1_dashboards"] = [
            d.get("url_path") for d in coll.async_items()
        ]

    try:
        registry_store = Store(hass, 1, "lovelace_dashboards")
        registry = (await registry_store.async_load()) or {"items": []}
        info["registry_dashboards"] = [
            d.get("url_path") for d in registry.get("items", [])
        ]
        info["registry_has_reef_tank"] = any(
            d.get("url_path") == DASHBOARD_URL_PATH
            for d in registry.get("items", [])
        )
    except Exception as exc:  # noqa: BLE001
        info["registry_error"] = str(exc)

    try:
        content_store = Store(hass, 1, DASHBOARD_STORE_KEY)
        content = await content_store.async_load()
        info["dashboard_content_exists"] = content is not None
        if content:
            views = (content.get("config") or {}).get("views") or []
            info["dashboard_content_views"] = len(views)
    except Exception as exc:  # noqa: BLE001
        info["content_error"] = str(exc)

    _LOGGER.warning("Dashboard diagnostic:\n%s",
                    "\n".join(f"  {k}: {v}" for k, v in info.items()))
    return info


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
    """Push the dashboard config and notify connected clients.

    Prefer routing through HA's `LovelaceStorage.async_save(config)`:
    that path updates the in-memory cache AND fires
    `EVENT_LOVELACE_UPDATED`, so any browser tab on the dashboard
    refreshes without an HA restart. The previous behaviour (direct
    `Store.async_save`) wrote disk only — the user had to restart HA
    before changes surfaced.

    Falls back to direct Store write if the LovelaceStorage instance
    isn't reachable (e.g. install Strategies 2/3 where we never wired
    the dashboard into `hass.data['lovelace'].dashboards`). The fallback
    requires a restart and we log loudly so it's obvious which path ran.
    """
    storage = _find_lovelace_storage(hass)
    if storage is not None:
        save = getattr(storage, "async_save", None)
        if save is not None:
            try:
                await save(config)
                _LOGGER.info(
                    "Pushed Reef Tank dashboard config (%d views) via "
                    "LovelaceStorage.async_save — open clients will reload",
                    len(config["views"]),
                )
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "LovelaceStorage.async_save raised %s — falling back to "
                    "direct Store write (restart HA to see the new layout)",
                    exc, exc_info=True,
                )

    dashboard_store = Store(hass, 1, DASHBOARD_STORE_KEY)
    await dashboard_store.async_save({"config": config})
    _LOGGER.warning(
        "Wrote Reef Tank dashboard config (%d views) to .storage/%s via "
        "direct Store (LovelaceStorage unreachable). RESTART HA to surface "
        "the new layout.",
        len(config["views"]), DASHBOARD_STORE_KEY,
    )


def _find_lovelace_storage(hass: HomeAssistant) -> Any:
    """Resolve the LovelaceStorage instance for our dashboard's url_path.

    Modern HA exposes `hass.data['lovelace'].dashboards` — a dict of
    `url_path → LovelaceConfig` (LovelaceStorage for storage-mode
    dashboards). Older versions use a plain dict. We probe both.
    """
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        return None
    dashboards = getattr(lovelace_data, "dashboards", None)
    if dashboards is None and isinstance(lovelace_data, dict):
        dashboards = lovelace_data.get("dashboards")
    if not isinstance(dashboards, dict):
        return None
    return dashboards.get(DASHBOARD_URL_PATH)


async def regenerate_dashboard(
    hass: HomeAssistant, coordinator: ReefDataCoordinator
) -> None:
    """Force a fresh dashboard rebuild and clear any user-removed flag."""
    await coordinator.async_set_dashboard_user_removed(False)
    await install_dashboard_if_missing(hass, coordinator)


# ---------------------------------------------------------------------------
# Layout generators
# ---------------------------------------------------------------------------
def _build_uid_to_eid(hass: HomeAssistant) -> dict[str, str]:
    """Map our integration's unique_ids to their actual entity_ids.

    Earlier versions (pre-device-grouping) created entities with names
    like `sensor.kh_latest`. Later versions emit `sensor.reef_tank_kh_latest`.
    The HA entity registry keeps original IDs sticky once created, so the
    actual entity_id depends on which version first registered each one.
    Always look up at dashboard-generation time rather than guessing.
    """
    if hass is None:
        return {}
    registry = er.async_get(hass)
    return {
        entry.unique_id: entry.entity_id
        for entry in registry.entities.values()
        if entry.platform == DOMAIN
    }


def _eid(uid_map: dict[str, str], unique_id: str, fallback_domain: str = "sensor") -> str:
    """Resolve a unique_id to its actual entity_id, with a sensible fallback."""
    return uid_map.get(unique_id, f"{fallback_domain}.{unique_id}")


def build_dashboard_config(hass: HomeAssistant | None = None) -> dict[str, Any]:
    """Build the full Reef Tank dashboard config from the parameter list."""
    uid_map = _build_uid_to_eid(hass) if hass else {}
    return {
        "title": "Reef Tank",
        "views": [
            _build_test_session_view(uid_map),
            _build_overview_view(uid_map),
            _build_advisor_view(uid_map),
            _build_diagnostics_view(uid_map),
        ],
    }


def _build_test_session_view(uid_map: dict[str, str]) -> dict[str, Any]:
    """Hanna run view: per-parameter auto-saving entry rows."""
    entry_rows = [
        {
            "entity": _eid(uid_map, f"reef_{p['id']}_entry", "number"),
            "name": f"{p['name']} ({p['unit']})",
            "secondary_info": "last-updated",
        }
        for p in INPUT_PARAMETERS
    ]
    latest_tiles = [
        {
            "type": "tile",
            "entity": _eid(uid_map, f"reef_{p['id']}_latest"),
            "name": p["name"],
            "vertical": True,
            "state_content": ["state", "last-changed"],
        }
        for p in INPUT_PARAMETERS
    ]
    # Tank-level tiles. Use the select entities (so they're tappable to
    # change habitat/problem/method right from the dashboard) when
    # they're in the registry, else fall back to the read-only sensor.
    habitat_e = _eid(uid_map, "reef_tank_habitat_select", "select") \
        if "reef_tank_habitat_select" in uid_map \
        else _eid(uid_map, "reef_tank_habitat")
    problem_e = _eid(uid_map, "reef_tank_problem_select", "select") \
        if "reef_tank_problem_select" in uid_map \
        else _eid(uid_map, "reef_tank_problem")
    method_e = _eid(uid_map, "reef_tank_method_select", "select")

    context_cards = [
        {"type": "heading", "heading": "Tank context", "icon": "mdi:waves"},
        {"type": "tile", "entity": habitat_e, "name": "Habitat", "icon": "mdi:waves"},
        {"type": "tile", "entity": problem_e, "name": "Problem",
         "icon": "mdi:alert-circle-outline"},
    ]
    if "reef_tank_method_select" in uid_map:
        context_cards.append({
            "type": "tile", "entity": method_e, "name": "Active Test Method",
            "icon": "mdi:flask-outline",
        })

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
                "cards": context_cards,
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


def _build_overview_view(uid_map: dict[str, str]) -> dict[str, Any]:
    """At-a-glance current values + days-since-test."""
    cards = []
    for p in INPUT_PARAMETERS:
        cards.append({
            "type": "tile",
            "entity": _eid(uid_map, f"reef_{p['id']}_latest"),
            "name": p["name"],
            "vertical": True,
        })
    cards_days = [
        {
            "type": "tile",
            "entity": _eid(uid_map, f"reef_{p['id']}_days_since"),
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


def _build_advisor_view(uid_map: dict[str, str]) -> dict[str, Any]:
    """Alk dosing advisor — recommendation, controls, calculation details."""
    advisor_eid = _eid(uid_map, "reef_alk_advisor_recommendation")

    # Top tile: the headline recommendation
    headline_card = {
        "type": "tile",
        "entity": advisor_eid,
        "name": "Suggested daily dose",
        "icon": "mdi:test-tube",
        "vertical": True,
        "state_content": ["state", "last-changed"],
    }

    # Show-your-work attributes — quick reference
    attributes_card = {
        "type": "entities",
        "title": "Show your work",
        "show_header_toggle": False,
        "entities": [
            {"type": "attribute", "entity": advisor_eid, "name": "KH median",
             "attribute": "kh_median"},
            {"type": "attribute", "entity": advisor_eid, "name": "Target band low",
             "attribute": "target_min"},
            {"type": "attribute", "entity": advisor_eid, "name": "Target band high",
             "attribute": "target_max"},
            {"type": "attribute", "entity": advisor_eid, "name": "Current dose (mL/day)",
             "attribute": "current_dose_mL"},
            {"type": "attribute", "entity": advisor_eid, "name": "Suggested dose (mL/day)",
             "attribute": "suggested_dose_mL"},
            {"type": "attribute", "entity": advisor_eid, "name": "Change (mL)",
             "attribute": "change_mL"},
            {"type": "attribute", "entity": advisor_eid, "name": "Change (%)",
             "attribute": "change_pct"},
            {"type": "attribute", "entity": advisor_eid, "name": "Confidence",
             "attribute": "confidence"},
            {"type": "attribute", "entity": advisor_eid, "name": "Reason",
             "attribute": "reason"},
            {"type": "attribute", "entity": advisor_eid, "name": "Cooldown until",
             "attribute": "cooldown_until"},
            {"type": "attribute", "entity": advisor_eid, "name": "Calibration warning",
             "attribute": "calibration_warning"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Last demand change", "attribute": "last_demand_change_at"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Days since demand change",
             "attribute": "days_since_demand_change"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Observed slope (dKH/day)",
             "attribute": "observed_slope_dkh_per_day"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Spec efficiency (dKH/mL)",
             "attribute": "spec_efficiency_dkh_per_mL"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Supplement (auto-detected)",
             "attribute": "detected_supplement_label"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Spec efficiency source",
             "attribute": "spec_efficiency_source"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Empirical potency (dKH/mL)",
             "attribute": "empirical_potency_dkh_per_mL"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Empirical / spec ratio",
             "attribute": "empirical_to_spec_ratio"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Empirical basis",
             "attribute": "empirical_potency_basis"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Spec drift warning",
             "attribute": "spec_drift_warning"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Last water change",
             "attribute": "last_water_change_at"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Days since water change",
             "attribute": "days_since_water_change"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Samples excluded (WC settling)",
             "attribute": "samples_excluded_for_wc"},
            {"type": "attribute", "entity": advisor_eid,
             "name": "Samples used", "attribute": "samples_used"},
        ],
    }

    # Action buttons — call services. We don't pre-fill the values; the
    # user enters their own in the developer-tools-style call dialog
    # the button selector opens.
    actions_card = {
        "type": "entities",
        "title": "Actions",
        "show_header_toggle": False,
        "entities": [
            {
                "type": "call-service",
                "name": "Acknowledge — I applied this in Reefbeat",
                "icon": "mdi:check-circle",
                "action_name": "Acknowledge",
                "service": "reeftanktracker.acknowledge_alk_recommendation",
                "service_data": {},
            },
            {
                "type": "call-service",
                "name": "Dismiss — ignore this suggestion",
                "icon": "mdi:close-circle",
                "action_name": "Dismiss",
                "service": "reeftanktracker.dismiss_alk_recommendation",
                "service_data": {},
            },
            {
                "type": "call-service",
                "name": "Log demand change — corals added/removed",
                "icon": "mdi:swap-vertical",
                "action_name": "Log",
                "service": "reeftanktracker.log_demand_change",
                "service_data": {"reason": ""},
            },
            {
                "type": "call-service",
                "name": "Log water change",
                "icon": "mdi:water-sync",
                "action_name": "Log",
                "service": "reeftanktracker.log_water_change",
                "service_data": {"percent": 10.0},
            },
        ],
    }

    help_card = {
        "type": "markdown",
        "content": (
            "**Custom supplements:** add via Developer Tools → "
            "`reeftanktracker.add_supplement_profile`. List with "
            "`reeftanktracker.list_supplement_profiles` (logs at WARNING).\n\n"
            "**Water changes:** log via the button above or "
            "`reeftanktracker.log_water_change`. Snapshots within "
            "the settling window after a water change are excluded "
            "from the slope/median calculation; the rolling window "
            "is otherwise unchanged.\n\n"
            "**Observed vs spec:** when you acknowledge a dose change, "
            "the advisor estimates the supplement's actual potency from "
            "before/after slope. If it drifts more than the configured "
            "threshold from spec, you'll see `spec_drift_warning: true` "
            "and a note in the reason text — consider switching to a "
            "Custom profile with the observed value if it persists."
        ),
    }

    return {
        "title": "Alk Advisor",
        "path": "alk-advisor",
        "icon": "mdi:test-tube",
        "type": "sections",
        "max_columns": 3,
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Alkalinity dosing advisor",
                     "icon": "mdi:test-tube"},
                    headline_card,
                ],
            },
            {
                "type": "grid",
                "column_span": 2,
                "cards": [attributes_card],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [actions_card],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [help_card],
            },
        ],
    }


def _build_diagnostics_view(uid_map: dict[str, str]) -> dict[str, Any]:
    """Method, drift, and timestamp diagnostics for each parameter."""
    rows = []
    for p in INPUT_PARAMETERS:
        rows.append({
            "type": "entities",
            "title": p["name"],
            "show_header_toggle": False,
            "entities": [
                {"entity": _eid(uid_map, f"reef_{p['id']}_latest"), "name": "Latest"},
                {"entity": _eid(uid_map, f"reef_{p['id']}_latest_method"),
                 "name": "Method"},
                {"entity": _eid(uid_map, f"reef_{p['id']}_latest_at"),
                 "name": "Sample time"},
                {"entity": _eid(uid_map, f"reef_{p['id']}_days_since"),
                 "name": "Days since manual test"},
                {"entity": _eid(uid_map, f"reef_{p['id']}_drift"),
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
