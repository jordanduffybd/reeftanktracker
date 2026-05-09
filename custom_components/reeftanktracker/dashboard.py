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
    views = [
        _build_test_session_view(uid_map),
        _build_overview_view(uid_map),
        _build_advisor_view(uid_map),
    ]
    # Per-element advisor views — one per param_id in PARAM_DEFAULTS.
    # Currently calcium + magnesium (0.5.2). Nitrate + phosphate
    # follow in 0.5.3 with multi-supplement-aware variants.
    from . import param_advisor
    for param_id in param_advisor.PARAM_DEFAULTS:
        views.append(_build_param_advisor_view(uid_map, param_id))
    views.extend([
        _build_dosing_plan_view(uid_map),
        _build_diagnostics_view(uid_map),
    ])
    return {
        "title": "Reef Tank",
        "views": views,
    }


def _build_param_advisor_view(
    uid_map: dict[str, str], param_id: str,
) -> dict[str, Any]:
    """Per-element advisor view (Calcium / Magnesium today, NO3+PO4
    in 0.5.3). Mirrors the alk advisor view's headline + show-your-work
    surface but parameterised on the per-element advisor sensor.

    Read-only for now — per-element ack/dismiss services don't exist
    yet. Action buttons (acknowledge / dismiss) ship in 0.5.3 alongside
    the multi-supplement work where they become more nuanced (which
    primary supplement to adjust, etc.).
    """
    from . import param_advisor
    advisor_eid = _eid(uid_map, f"reef_{param_id}_advisor_recommendation")
    title = param_id.capitalize()
    defaults = param_advisor.PARAM_DEFAULTS.get(param_id, {})
    unit = defaults.get("value_unit", "ppm")

    # Headline: same conditional-banner-or-tile as the alk advisor view.
    headline_card = {
        "type": "vertical-stack",
        "cards": [
            {
                "type": "conditional",
                "conditions": [
                    {"condition": "state", "entity": advisor_eid,
                     "state": ["unknown", "unavailable"]},
                ],
                "card": {
                    "type": "markdown",
                    "content": (
                        "## ⏸️ " + title + " advisor paused\n\n"
                        "{{ state_attr('" + advisor_eid + "', 'reason') "
                        "or 'Advisor not yet enabled.' }}"
                    ),
                },
            },
            {
                "type": "conditional",
                "conditions": [
                    {"condition": "state", "entity": advisor_eid,
                     "state_not": "unknown"},
                    {"condition": "state", "entity": advisor_eid,
                     "state_not": "unavailable"},
                ],
                "card": {
                    "type": "tile",
                    "entity": advisor_eid,
                    "name": f"Suggested daily dose ({title})",
                    "icon": "mdi:test-tube",
                    "vertical": True,
                    "state_content": ["state", "last-changed"],
                },
            },
        ],
    }

    # Show-your-work card — markdown so we can hide rows that are
    # null when the advisor doesn't have data yet. Uses the same
    # `kh_median` field name as alk (carryover for code reuse —
    # the field stores whatever the parameter's value is).
    attributes_card = {
        "type": "markdown",
        "content": (
            "{% set ent = '" + advisor_eid + "' %}"
            "{% set s = states(ent) %}"
            "{% set known = s not in ('unknown','unavailable','none','') %}"
            "### Show your work\n\n"
            "{% if not known %}"
            f"_Most computed values are unavailable while the {title} "
            "advisor is paused — see banner above. The configured "
            "target band and supplement spec are still listed below._\n\n"
            "{% endif %}"
            "| | |\n|---|---|\n"
            f"| Target band | "
            "{{ state_attr(ent, 'target_min') }} – "
            "{{ state_attr(ent, 'target_max') }}"
            f" {unit} |\n"
            f"| Spec efficiency ({unit}/mL) | "
            "{{ state_attr(ent, 'spec_efficiency_dkh_per_mL') or '—' }} |\n"
            "| Spec efficiency source | "
            "{{ state_attr(ent, 'spec_efficiency_source') or '—' }} |\n"
            "| Confidence | "
            "{{ state_attr(ent, 'confidence') or '—' }} |\n"
            "| Reason | "
            "{{ state_attr(ent, 'reason') or '—' }} |\n"
            "{% macro row(label, attr, suffix='') -%}"
            "{% set v = state_attr(ent, attr) %}"
            "{% if v is not none and v not in ('unknown','unavailable') %}"
            "| {{ label }} | {{ v }}{{ suffix }} |\n"
            "{% endif %}"
            "{%- endmacro %}"
            f"{{{{ row('{title} median', 'kh_median', ' {unit}') }}}}"
            "{{ row('Current dose (mL/day)', 'current_dose_mL') }}"
            "{{ row('Suggested dose (mL/day)', 'suggested_dose_mL') }}"
            "{{ row('Change (mL)', 'change_mL') }}"
            "{{ row('Change (%)', 'change_pct') }}"
            f"{{{{ row('Observed slope ({unit}/day)', 'observed_slope_dkh_per_day') }}}}"
            "{{ row('Samples used', 'samples_used') }}"
            "{{ row('Cooldown until', 'cooldown_until') }}"
            "{{ row('Last demand change', 'last_demand_change_at') }}"
            "{{ row('Days since demand change', 'days_since_demand_change') }}"
            "{{ row('Last water change', 'last_water_change_at') }}"
            "{{ row('Days since water change', 'days_since_water_change') }}"
            "{{ row('NO3:PO4 ratio', 'redfield_ratio') }}"
            "{{ row('Redfield warning', 'redfield_warning') }}"
        ),
    }

    help_card = {
        "type": "markdown",
        "content": (
            f"**Configure:** Settings → Devices → Reef Tank Tracker → "
            f"Configure → \"{title} dosing advisor\".\n\n"
            f"**Add a reading:** Test Session view → enter a "
            f"{title} value. The advisor auto-captures a snapshot "
            f"and recomputes.\n\n"
            f"**Recommendation-only:** never writes to your doser. "
            f"Apply changes in ReefBeat manually.\n\n"
            f"**Defaults are tuned for sparse manual testing** "
            f"(1-2 readings/month). If you have an auto-tester, shrink "
            f"`window_days` and bump `min_samples` via the Configure form."
        ),
    }

    # Reading history graph — surfaces the actual `_latest` sensor
    # values over the advisor's window so the user can SEE the trend
    # the algorithm is reading. 90 days matches the default
    # window_days for sparse-cadence params (Ca/Mg/NO3/PO4); the
    # native HA history-graph just plots the points/line directly.
    # No target-band overlay (history-graph doesn't support it
    # natively); the band shows up in the show-your-work card below.
    latest_eid = _eid(uid_map, f"reef_{param_id}_latest")
    history_graph_card = {
        "type": "history-graph",
        "title": f"{title} readings (last 90 days, {unit})",
        "hours_to_show": 24 * 90,
        "entities": [
            {"entity": latest_eid, "name": title},
        ],
    }

    # Activity log: filtered to this param's advisor sensor + the
    # KH Keeper calibration entities (unchanged across params — the
    # advisor surface is shared). Per-element ack/dismiss arrives in
    # 0.5.3 along with multi-supplement work.
    activity_log = {
        "type": "logbook",
        "title": f"Recent {title} activity",
        "hours_to_show": 168 * 4,  # 4 weeks for sparse-cadence params
        "entities": [advisor_eid],
    }

    return {
        "title": f"{title} Advisor",
        "path": f"{param_id}-advisor",
        "icon": "mdi:test-tube",
        "type": "sections",
        "max_columns": 3,
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading",
                     "heading": f"{title} dosing advisor",
                     "icon": "mdi:test-tube"},
                    headline_card,
                ],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading",
                     "heading": f"{title} reading history",
                     "icon": "mdi:chart-line"},
                    history_graph_card,
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
                "cards": [help_card],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Activity log",
                     "icon": "mdi:history"},
                    activity_log,
                ],
            },
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
    # Tank-level tiles. The selects are the single source of truth for
    # habitat/problem/method — tappable to change right from the dashboard.
    habitat_e = _eid(uid_map, "reef_tank_habitat_select", "select")
    problem_e = _eid(uid_map, "reef_tank_problem_select", "select")
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
    kh_latest_eid = _eid(uid_map, "reef_kh_latest")
    # Alk gets a 7-day window — the KH Keeper polls every ~12h so the
    # graph shows daily drift clearly. Per-element advisors use 90
    # days because their data is sparse manual readings.
    kh_history_card = {
        "type": "history-graph",
        "title": "KH readings (last 7 days, dKH)",
        "hours_to_show": 168,
        "entities": [
            {"entity": kh_latest_eid, "name": "KH"},
        ],
    }

    # Headline card. Two conditional variants stacked: a markdown
    # "advisor paused" banner when state is unknown/unavailable (e.g.
    # ReefBeat doser offline for maintenance), and the regular tile
    # showing the suggested-dose number when state is a real value.
    # This stops the prod dashboard from showing "Suggested daily
    # dose: Unknown" with no context when devices are off — instead
    # the user sees the actionable reason text large + prominent.
    headline_card = {
        "type": "vertical-stack",
        "cards": [
            {
                "type": "conditional",
                "conditions": [
                    {"condition": "state", "entity": advisor_eid,
                     "state": ["unknown", "unavailable"]},
                ],
                "card": {
                    "type": "markdown",
                    "content": (
                        "## ⏸️ Advisor paused\n\n"
                        "{{ state_attr('" + advisor_eid + "', 'reason') "
                        "or 'No reason recorded.' }}\n\n"
                        "_Reason last updated "
                        "{{ relative_time(states['" + advisor_eid + "'].last_changed) "
                        "if states['" + advisor_eid + "'] else 'unknown' }}._"
                    ),
                },
            },
            {
                "type": "conditional",
                "conditions": [
                    {"condition": "state", "entity": advisor_eid,
                     "state_not": "unknown"},
                    {"condition": "state", "entity": advisor_eid,
                     "state_not": "unavailable"},
                ],
                "card": {
                    "type": "tile",
                    "entity": advisor_eid,
                    "name": "Suggested daily dose",
                    "icon": "mdi:test-tube",
                    "vertical": True,
                    "state_content": ["state", "last-changed"],
                },
            },
        ],
    }

    # Show-your-work card. Renders attribute rows ONLY when the
    # underlying value is meaningful (not None / "unknown" / empty).
    # When ReefBeat is offline the algorithm returns None for most
    # values; previously this card showed ~20 rows of "Unknown" which
    # looked broken. The markdown template skips empty rows so the
    # card naturally collapses to whatever IS known.
    #
    # We render the full breakdown when state is a number AND the
    # actionable reason when state is unknown — same content surface,
    # different shape. Constants stay always-visible (target band,
    # spec efficiency source, etc.) since those are config not
    # computed.
    attributes_card = {
        "type": "markdown",
        "content": (
            "{% set ent = '" + advisor_eid + "' %}"
            "{% set s = states(ent) %}"
            "{% set known = s not in ('unknown','unavailable','none','') %}"
            "### Show your work\n\n"
            "{% if not known %}"
            "_Most computed values are unavailable while the advisor "
            "is paused — see banner above. The configured target band "
            "and supplement spec are still listed below._\n\n"
            "{% endif %}"
            "| | |\n|---|---|\n"
            # Always-shown rows (config, not computed)
            "| Target band | "
            "{{ state_attr(ent, 'target_min') }} – "
            "{{ state_attr(ent, 'target_max') }} dKH |\n"
            "| Spec efficiency (dKH/mL) | "
            "{{ state_attr(ent, 'spec_efficiency_dkh_per_mL') or '—' }} |\n"
            "| Spec efficiency source | "
            "{{ state_attr(ent, 'spec_efficiency_source') or '—' }} |\n"
            "| Calibration warning | "
            "{{ state_attr(ent, 'calibration_warning') }} |\n"
            "| Confidence | "
            "{{ state_attr(ent, 'confidence') or '—' }} |\n"
            "| Reason | "
            "{{ state_attr(ent, 'reason') or '—' }} |\n"
            # Conditionally-shown computed rows — only when the value
            # is non-null and not "Unknown"
            "{% macro row(label, attr, suffix='') -%}"
            "{% set v = state_attr(ent, attr) %}"
            "{% if v is not none and v not in ('unknown','unavailable') %}"
            "| {{ label }} | {{ v }}{{ suffix }} |\n"
            "{% endif %}"
            "{%- endmacro %}"
            "{{ row('KH median', 'kh_median', ' dKH') }}"
            "{{ row('Current dose (mL/day)', 'current_dose_mL') }}"
            "{{ row('Suggested dose (mL/day)', 'suggested_dose_mL') }}"
            "{{ row('Change (mL)', 'change_mL') }}"
            "{{ row('Change (%)', 'change_pct') }}"
            "{{ row('Observed slope (dKH/day)', 'observed_slope_dkh_per_day') }}"
            "{{ row('Samples used', 'samples_used') }}"
            "{{ row('Cooldown until', 'cooldown_until') }}"
            "{{ row('Supplement (auto-detected)', 'detected_supplement_label') }}"
            "{{ row('Empirical potency (dKH/mL)', 'empirical_potency_dkh_per_mL') }}"
            "{{ row('Empirical / spec ratio', 'empirical_to_spec_ratio') }}"
            "{{ row('Empirical basis', 'empirical_potency_basis') }}"
            "{{ row('Spec drift warning', 'spec_drift_warning') }}"
            "{{ row('Last demand change', 'last_demand_change_at') }}"
            "{{ row('Days since demand change', 'days_since_demand_change') }}"
            "{{ row('Last water change', 'last_water_change_at') }}"
            "{{ row('Days since water change', 'days_since_water_change') }}"
            "{{ row('Samples excluded (WC settling)', 'samples_excluded_for_wc') }}"
        ),
    }

    # Action buttons — call services. We don't pre-fill the values; the
    # user enters their own in the developer-tools-style call dialog
    # the button selector opens.
    # Only Acknowledge / Dismiss are inline buttons — they auto-fill
    # from the current recommendation so a no-payload click works.
    # Log demand change / Log water change need free-text + numeric
    # input that HA's `call-service` entity row can't capture, so those
    # are surfaced as links into Developer Tools → Services where the
    # user gets a real form. See `help_card` below.
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
        ],
    }

    # Inline form: log a water change. The form fields are real entities
    # owned by this integration (number/text), so the user types into them
    # right on the dashboard. The submit button calls the service with
    # values resolved from those entities at click time via templates.
    wc_percent_eid = _eid(uid_map, "reef_advisor_form_wc_percent", "number")
    wc_salt_kh_eid = _eid(uid_map, "reef_advisor_form_wc_salt_mix_kh", "number")
    wc_notes_eid = _eid(uid_map, "reef_advisor_form_wc_notes", "text")
    water_change_form = {
        "type": "entities",
        "title": "Log water change",
        "show_header_toggle": False,
        "entities": [
            {"entity": wc_percent_eid, "name": "Percent volume changed (%)"},
            {"entity": wc_salt_kh_eid, "name": "Salt mix KH (dKH, optional)"},
            {"entity": wc_notes_eid, "name": "Notes (optional)"},
            {
                # The form-submit service reads each form entity's
                # state on the server side and forwards to
                # log_water_change. We can't put templates in
                # service_data here because HA's `entities` card
                # `call-service` row doesn't render them.
                "type": "call-service",
                "name": "Submit water change",
                "icon": "mdi:water-sync",
                "action_name": "Log",
                "service": "reeftanktracker.submit_water_change_form",
                "service_data": {},
            },
        ],
    }

    # Inline form: log a demand change.
    demand_reason_eid = _eid(uid_map, "reef_advisor_form_demand_reason", "text")
    demand_dir_eid = _eid(uid_map, "reef_advisor_form_demand_direction", "select")
    demand_mag_eid = _eid(uid_map, "reef_advisor_form_demand_magnitude_pct", "number")
    demand_change_form = {
        "type": "entities",
        "title": "Log demand change",
        "show_header_toggle": False,
        "entities": [
            {"entity": demand_reason_eid, "name": "Reason (e.g. added 3 SPS frags)"},
            {"entity": demand_dir_eid, "name": "Expected direction"},
            {"entity": demand_mag_eid, "name": "Magnitude hint (%, optional)"},
            {
                "type": "call-service",
                "name": "Submit demand change",
                "icon": "mdi:swap-vertical",
                "action_name": "Log",
                "service": "reeftanktracker.submit_demand_change_form",
                "service_data": {},
            },
        ],
    }

    # Admin-only ops (capture snapshot, supplement profile CRUD) stay in
    # Developer Tools — they're rare and varied enough that the dev-tools
    # form is fine. Markdown card explains.
    help_card = {
        "type": "markdown",
        "content": (
            "**Custom supplements** and **manual snapshots** are managed "
            "from Settings → Developer Tools → Actions. Search for "
            "`reeftanktracker` to find:\n\n"
            "- `add_supplement_profile` / `list_supplement_profiles` / "
            "`remove_supplement_profile`\n"
            "- `capture_snapshot_now` (manual baseline, e.g. after "
            "calibrating the KH Keeper)\n\n"
            "**Observed vs spec:** when you acknowledge a dose change, "
            "the advisor estimates the supplement's actual potency from "
            "before/after slope. If it drifts more than the configured "
            "threshold from spec, you'll see `spec_drift_warning: true` "
            "and a note in the reason — consider switching to a Custom "
            "profile with the observed value if it persists."
        ),
    }

    # Event log card. The integration fires `logbook_entry` events
    # tagged to the advisor sensor's entity_id for every user action
    # (acknowledge, dismiss, water change, demand change, manual
    # snapshot). Tracking the advisor sensor catches them all.
    #
    # KH Keeper calibration is external (kh-keeper-bridge MQTT-
    # discovered), so we add the calibrate input + adjustment sensor
    # explicitly. State changes on `number.kh_keeper_calibrate_kh_from_
    # drop_test` and `sensor.kh_keeper_kh_adjustment` show up as
    # standard state-change logbook entries.
    log_entities = [
        advisor_eid,
        "number.kh_keeper_calibrate_kh_from_drop_test",
        "sensor.kh_keeper_kh_adjustment",
    ]
    event_log_card = {
        "type": "logbook",
        "title": "Recent activity",
        "hours_to_show": 168,  # 7 days
        "entities": log_entities,
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
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "KH reading history",
                     "icon": "mdi:chart-line"},
                    kh_history_card,
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
                "column_span": 1,
                "cards": [
                    {"type": "heading", "heading": "Water change",
                     "icon": "mdi:water-sync"},
                    water_change_form,
                ],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [
                    {"type": "heading", "heading": "Demand change",
                     "icon": "mdi:swap-vertical"},
                    demand_change_form,
                ],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [help_card],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Activity log",
                     "icon": "mdi:history"},
                    event_log_card,
                ],
            },
        ],
    }


def _build_dosing_plan_view(uid_map: dict[str, str]) -> dict[str, Any]:
    """Surfaces the most-recent ICP test's habitat-aware dose plan so the
    user can program the high-volume supplements into their RSDOSE4 heads
    manually. The plan is sorted by importance — most-important first.

    The view:
      - Shows the active habitat/problem the plan was computed for
      - Lists the dose recommendations + corrective dose strings
      - Provides a re-import action so the user can change habitat/problem
        and pull a fresh plan from Triton without re-typing the URL
    """
    plan_eid = _eid(uid_map, "reef_active_dosing_plan")

    # Headline tile: the sensor IS a TIMESTAMP (sample-collection date),
    # so HA renders it as "X days ago" / a relative-time label — exactly
    # what we want for "this plan reflects a sample taken N days ago".
    headline_tile = {
        "type": "tile",
        "entity": plan_eid,
        "name": "Last test taken",
        "icon": "mdi:beaker-plus-outline",
        "vertical": True,
        "state_content": ["state"],
    }

    # Active scenario as a markdown card — kills the repeated icon row +
    # label truncation that came from `entities`/`attribute` rows.
    summary_card = {
        "type": "markdown",
        "content": (
            "{% set s = states('" + plan_eid + "') %}"
            "{% if s in ('unknown', 'unavailable', 'none') %}"
            "_No ICP test imported yet._"
            "{% else %}"
            "### Active scenario\n\n"
            "| | |\n|---|---|\n"
            "| Test ID | `{{ state_attr('" + plan_eid + "', 'test_id') }}` |\n"
            "| Sample date | {{ state_attr('" + plan_eid + "', 'sample_date') }} |\n"
            "| Imported | {{ state_attr('" + plan_eid + "', 'imported_at') }} |\n"
            "| Active habitat | **{{ state_attr('" + plan_eid + "', 'active_habitat') }}** |\n"
            "| Active problem | **{{ state_attr('" + plan_eid + "', 'active_problem') }}** |\n"
            "| Originally rendered for | "
            "{{ state_attr('" + plan_eid + "', 'rendered_for_habitat') }} / "
            "{{ state_attr('" + plan_eid + "', 'rendered_for_problem') }} |\n"
            "| Source | [View on Triton]({{ state_attr('" + plan_eid + "', 'url') }}) |\n"
            "{% endif %}"
        ),
    }

    # Dose plan as a markdown card. We use `\n\n` between recommendations
    # to force fresh top-level bullets in the rendered list — the prior
    # version had trailing whitespace that made every element after Ca
    # render as a sub-bullet of Ca.
    plan_card = {
        "type": "markdown",
        "content": (
            "{% set recs = state_attr('" + plan_eid + "', 'recommendations') %}"
            "{% if recs %}"
            "### Dose plan — sorted by importance\n\n"
            "{% for r in recs %}"
            "**{{ r.symbol }}** — {{ '★' * (r.importance_stars or 0) }} "
            "{{ r.importance_label or '' }}\n\n"
            "{{ r.corrective_dose or '_No corrective dose suggested._' }}"
            "{% if r.product_reference %}\n\n"
            "*Product: {{ r.product_reference }}*"
            "{% endif %}\n\n---\n\n"
            "{% endfor %}"
            "{% else %}"
            "_No ICP test imported yet. Call_ "
            "`reeftanktracker.import_triton_url` _from Developer Tools "
            "→ Actions with your Triton showroom URL plus the habitat "
            "and problem you want plan guidance for._"
            "{% endif %}"
        ),
    }

    help_card = {
        "type": "markdown",
        "content": (
            "**Importing / re-importing:** fill in the form on the right "
            "(URL is required; habitat + problem default to your current "
            "tank state; sample date defaults to today if blank). Click "
            "Import → the plan re-renders below with the dose "
            "recommendations recomputed for that habitat. Re-running "
            "with different inputs overwrites the active plan — useful "
            "if you're changing tank direction (e.g. Mixed Reef → SPS) "
            "or have a transient issue (e.g. Cyanobacteria).\n\n"
            "**Manual programming:** for high-volume supplements, take "
            "the corrective-dose string above and split it across the "
            "RSDOSE4 head schedule (e.g. \"17.12 ml for 1 day\" → 17 ml "
            "spread over the day). The advisor view handles alkalinity "
            "trend-based daily-dose tuning separately."
        ),
    }

    # Inline import form. Mirrors the water-change / demand-change form
    # pattern from the alk advisor view: form fields are real entities
    # (text/select) owned by this integration, the user fills them in,
    # the Submit button calls submit_icp_import_form which reads each
    # entity's state server-side and forwards to import_triton_url.
    icp_url_eid = _eid(uid_map, "reef_advisor_form_icp_url", "text")
    icp_habitat_eid = _eid(
        uid_map, "reef_advisor_form_icp_habitat", "select",
    )
    icp_problem_eid = _eid(
        uid_map, "reef_advisor_form_icp_problem", "select",
    )
    icp_sample_date_eid = _eid(
        uid_map, "reef_advisor_form_icp_sample_date", "text",
    )
    import_form = {
        "type": "entities",
        "title": "Import a Triton ICP test",
        "show_header_toggle": False,
        "entities": [
            {"entity": icp_url_eid,
             "name": "Triton URL (e.g. https://www.triton-lab.de/en/showroom/icp-oes/229019)"},
            {"entity": icp_habitat_eid, "name": "Habitat"},
            {"entity": icp_problem_eid, "name": "Problem"},
            {"entity": icp_sample_date_eid,
             "name": "Sample date (YYYY-MM-DD, blank = today)"},
            {
                "type": "call-service",
                "name": "Import — fetch + parse + recompute doses",
                "icon": "mdi:cloud-download",
                "action_name": "Import",
                "service": "reeftanktracker.submit_icp_import_form",
                "service_data": {},
            },
        ],
    }

    return {
        "title": "Dosing Plan",
        "path": "dosing-plan",
        "icon": "mdi:beaker-plus-outline",
        "type": "sections",
        "max_columns": 3,
        "sections": [
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading",
                     "heading": "Active dosing plan from latest ICP test",
                     "icon": "mdi:beaker-plus-outline"},
                    headline_tile,
                ],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [
                    {"type": "heading", "heading": "Import a new ICP",
                     "icon": "mdi:cloud-download"},
                    import_form,
                ],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [
                    {"type": "heading", "heading": "Active scenario",
                     "icon": "mdi:beaker-outline"},
                    summary_card,
                ],
            },
            {
                "type": "grid",
                "column_span": 1,
                "cards": [help_card],
            },
            {
                "type": "grid",
                "column_span": 3,
                "cards": [
                    {"type": "heading", "heading": "Dose plan",
                     "icon": "mdi:beaker-plus-outline"},
                    plan_card,
                ],
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
