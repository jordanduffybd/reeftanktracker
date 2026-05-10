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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HABITATS,
    PROBLEMS,
    INVENTORY_CATEGORIES,
    SERVICE_ADD_INVENTORY,
    SERVICE_ADD_SUPPLEMENT_PROFILE,
    SERVICE_CAPTURE_SNAPSHOT,
    SERVICE_IMPORT_ICP,
    SERVICE_IMPORT_TRITON_URL,
    SERVICE_LIST_SUPPLEMENT_PROFILES,
    SERVICE_LOG_WATER_CHANGE,
    SERVICE_RECORD_READING,
    SERVICE_REMOVE_INVENTORY,
    SERVICE_REMOVE_SUPPLEMENT_PROFILE,
    SERVICE_UPDATE_SUPPLEMENT_PROFILE,
    SERVICE_SET_HABITAT,
    SERVICE_SUBMIT_DEMAND_FORM,
    SERVICE_SUBMIT_ICP_FORM,
    SERVICE_SUBMIT_WC_FORM,
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
# Generic per-element ack/dismiss — accept a `parameter` arg so the
# same service handles kh / calcium / magnesium / nitrate / phosphate.
# The alk-specific services stay registered for back-compat but new
# dashboards (per-element views from 0.5.2+) call these instead.
SERVICE_ACK_ADVISOR = "acknowledge_advisor"
SERVICE_DISMISS_ADVISOR = "dismiss_advisor"
SERVICE_LOG_DEMAND_CHANGE = "log_demand_change"

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "number", "select", "text"]

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
    # Both optional so the dashboard "Acknowledge" button can fire with
    # no payload — handler infers `applied_value_mL` from the current
    # advisor recommendation, and `prev_value_mL` from the latest
    # snapshot's dose. Pass either explicitly to override.
    vol.Optional("applied_value_mL"): vol.Coerce(float),
    vol.Optional("prev_value_mL"): vol.Coerce(float),
})

DISMISS_ALK_SCHEMA = vol.Schema({
    vol.Optional("suggested_value_mL", default=0.0): vol.Coerce(float),
})

# Generic per-element ack/dismiss schemas. `parameter` is required —
# everything else is optional and inferred. Constrained to known
# advisor parameters so a typo can't silently create a new param_id
# bucket in storage.
_ADVISOR_PARAMETERS = vol.In([
    "kh", "calcium", "magnesium", "nitrate", "phosphate",
])

ACK_ADVISOR_SCHEMA = vol.Schema({
    vol.Required("parameter"): _ADVISOR_PARAMETERS,
    vol.Optional("applied_value_mL"): vol.Coerce(float),
    vol.Optional("prev_value_mL"): vol.Coerce(float),
})

DISMISS_ADVISOR_SCHEMA = vol.Schema({
    vol.Required("parameter"): _ADVISOR_PARAMETERS,
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

def _validate_supplement_profile(value: dict[str, Any]) -> dict[str, Any]:
    """eff_dkh_per_mL_per_100L is required when the profile targets KH
    (the alk advisor needs a real potency to compute dose changes).

    For non-KH supplements (Ca / Mg / NO3 / PO4 / etc.) the field is
    optional — those parameters use different units and the per-element
    advisors arriving in 0.5.0 will introduce a parameter-aware
    potency field. For now these profiles just carry label + notes and
    serve as a registry the user can reference.

    Also normalizes `param_id` to a list (string input → 1-element list)
    so multi-target supplements like Red Sea NO3:PO4-X (which targets
    BOTH nitrate AND phosphate) can be registered as
    `param_id=["nitrate", "phosphate"]` and surface in BOTH per-element
    advisors. Internally the storage always holds a list; the singular
    form is just user-facing sugar.
    """
    pid = value.get("param_id", "kh")
    pids = [pid] if isinstance(pid, str) else list(pid)
    if not pids:
        raise vol.Invalid("param_id must be a non-empty string or list")
    value["param_id"] = pids
    if "kh" in pids and value.get("eff_dkh_per_mL_per_100L") is None:
        raise vol.Invalid(
            "eff_dkh_per_mL_per_100L is required when param_id includes "
            "'kh' — the alk advisor uses it to compute dose changes."
        )
    return value


ADD_SUPPLEMENT_PROFILE_SCHEMA = vol.All(
    vol.Schema({
        vol.Required("label"): cv.string,
        # Required-when-targeting-KH, optional-otherwise — see
        # _validate_supplement_profile. The vol.Any wrapper lets
        # None / absent through; the post-schema validator enforces
        # presence when "kh" is in param_id.
        vol.Optional("eff_dkh_per_mL_per_100L"): vol.Any(
            None,
            vol.All(vol.Coerce(float), vol.Range(min=0.001, max=5.0)),
        ),
        # Generic per-element potency. Units depend on `param_id`:
        # - calcium: ppm Ca per mL per 100L (Foundation A = 2.0)
        # - magnesium: ppm Mg per mL per 100L (Foundation C = 1.0)
        # - nitrate / phosphate: ppm reduction per mL per 100L (use
        #   negative values for removers — e.g. lanthanum-based
        #   phosphate removers)
        # The schema range is wide because different parameters span
        # different magnitudes (Ca jumps by ppm, alk by 0.1 dKH).
        vol.Optional("eff_per_mL_per_100L"): vol.Any(
            None,
            vol.All(vol.Coerce(float), vol.Range(min=-100.0, max=100.0)),
        ),
        # Which parameter(s) this supplement targets. Accepts either
        # a single string ("kh") or a list (["nitrate", "phosphate"]
        # for NO3:PO4-X-style multi-target supplements). Default "kh"
        # preserves back-compat — every profile created before this
        # field existed reads as a KH supplement, and the alk advisor's
        # dropdown filters to KH-targeting profiles. Per-element
        # advisors (0.5.0+) filter to their own param_id and a
        # multi-target profile surfaces in each one.
        vol.Optional("param_id", default="kh"): vol.Any(
            cv.string, vol.All(cv.ensure_list, [cv.string]),
        ),
        vol.Optional("label_patterns", default=[]): vol.All(
            cv.ensure_list, [cv.string],
        ),
        vol.Optional("notes"): cv.string,
    }),
    _validate_supplement_profile,
)

REMOVE_SUPPLEMENT_PROFILE_SCHEMA = vol.Schema({
    vol.Required("id"): cv.string,
})

# Update accepts the same shape as Add minus `label` (id can't change),
# and every field other than `id` is optional. Sentinel-style updates
# (only-pass-the-fields-you-want-to-change) are handled in
# coordinator.async_update_supplement_profile via the _UNSET pattern.
UPDATE_SUPPLEMENT_PROFILE_SCHEMA = vol.Schema({
    vol.Required("id"): cv.string,
    vol.Optional("eff_dkh_per_mL_per_100L"): vol.All(
        vol.Coerce(float), vol.Range(min=-5.0, max=5.0),
    ),
    vol.Optional("eff_per_mL_per_100L"): vol.All(
        vol.Coerce(float), vol.Range(min=-100.0, max=100.0),
    ),
    vol.Optional("param_id"): vol.Any(
        cv.string, vol.All(cv.ensure_list, [cv.string]),
    ),
    vol.Optional("label_patterns"): vol.All(
        cv.ensure_list, [cv.string],
    ),
    vol.Optional("notes"): cv.string,
})

LOG_WATER_CHANGE_SCHEMA = vol.Schema({
    vol.Optional("parameter", default="kh"): cv.string,
    vol.Required("percent"): vol.All(
        vol.Coerce(float), vol.Range(min=0.1, max=100.0),
    ),
    vol.Optional("salt_mix_kh"): vol.Coerce(float),
    vol.Optional("notes"): cv.string,
})

CAPTURE_SNAPSHOT_SCHEMA = vol.Schema({
    # All optional. With no args, reads current live state and stamps "now".
    # Override `at` to seed historical snapshots (useful for dev testing).
    vol.Optional("at"): cv.string,
    vol.Optional("kh"): vol.Coerce(float),
    vol.Optional("dose_mL"): vol.Coerce(float),
})

IMPORT_TRITON_URL_SCHEMA = vol.Schema({
    vol.Required("url"): cv.string,
    # Triton's public showroom HTML doesn't expose the sample date.
    # Pass it explicitly (ISO YYYY-MM-DD) for older imports; defaults
    # to today otherwise.
    vol.Optional("sample_date"): cv.string,
    # habitat + problem default to the tank's current state. Pass
    # explicitly to filter the test's recommendations to a different
    # scenario (no need to re-share the URL from Triton).
    vol.Optional("habitat"): vol.In(HABITATS),
    vol.Optional("problem"): vol.In(PROBLEMS),
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
    """Pull advisor-section + target-range options into a flat dict.

    Pass through:
    - Everything in ADVISOR_DEFAULTS (the alk advisor's tunables)
    - Any `advisor_<param_id>_*` per-element advisor key (Ca, Mg, NO3,
      PO4 — see param_advisor.PARAM_DEFAULTS). Picking up these by
      prefix means new per-element advisors don't need plumbing
      changes here.
    - Any `target_<param_id>_min` / `target_<param_id>_max` override
      from the Target Ranges Options page — these are read by
      `coordinator.get_target_range` to override the static defaults
      in parameters.py

    Empty values fall back to defaults (algorithm-side for advisor
    keys, parameters.py-side for target ranges).
    """
    out: dict[str, Any] = {}
    for key in ADVISOR_DEFAULTS:
        if key in entry.options:
            out[key] = entry.options[key]
    for key, value in entry.options.items():
        # Per-element advisor keys (advisor_calcium_enabled, etc.).
        # Skip anything already pulled in by ADVISOR_DEFAULTS to avoid
        # double-write.
        if key.startswith("advisor_") and key not in out:
            out[key] = value
        # Target-range overrides
        if key.startswith("target_") and (
            key.endswith("_min") or key.endswith("_max")
        ):
            out[key] = value
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
                SERVICE_ACK_ADVISOR,
                SERVICE_DISMISS_ADVISOR,
                SERVICE_LOG_DEMAND_CHANGE,
                SERVICE_ADD_SUPPLEMENT_PROFILE,
                SERVICE_REMOVE_SUPPLEMENT_PROFILE,
                SERVICE_UPDATE_SUPPLEMENT_PROFILE,
                SERVICE_LIST_SUPPLEMENT_PROFILES,
                SERVICE_LOG_WATER_CHANGE,
                SERVICE_CAPTURE_SNAPSHOT,
                SERVICE_SUBMIT_WC_FORM,
                SERVICE_SUBMIT_DEMAND_FORM,
                SERVICE_IMPORT_TRITON_URL,
                SERVICE_SUBMIT_ICP_FORM,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


def _resolve_advisor_entity_id(
    hass: HomeAssistant, advisor_unique_id: str,
) -> str | None:
    """Look up the advisor sensor's actual entity_id by unique_id.

    Used to set `entity_id` on logbook events so they show up in
    the dashboard logbook cards (which filter by entity_id).
    """
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.platform == DOMAIN and entry.unique_id == advisor_unique_id:
            return entry.entity_id
    return None


async def _show_action_feedback(
    hass: HomeAssistant,
    *,
    title: str,
    message: str,
    advisor_unique_id: str | None = None,
    notification_id_prefix: str = "reef_action",
    toast_seconds: float = 10.0,
) -> None:
    """Two-fold feedback for dashboard actions:

    1. Fire `logbook_entry` event with `entity_id` set to the advisor
       sensor — surfaces the action in the dashboard's logbook card.
       Without this, the logbook only catches state changes, so
       acknowledge / dismiss / water-change / demand-change actions
       (which mostly mutate attributes, not state) never appeared.

    2. Create a `persistent_notification` and auto-dismiss it after
       `toast_seconds` — gives the user a visible toast in the bell
       icon dropdown that the action took effect, instead of
       requiring them to read system logs.

    `advisor_unique_id` defaults to the alk advisor sensor; pass a
    different unique_id (e.g. `reef_calcium_advisor_recommendation`)
    for per-element actions when those handlers exist.
    """
    eid = None
    if advisor_unique_id is not None:
        eid = _resolve_advisor_entity_id(hass, advisor_unique_id)

    # 1. Logbook entry — populates the "Recent activity" card.
    # The HA logbook component listens for the `logbook_entry`
    # event and renders entries whose `entity_id` is in the card's
    # entity filter.
    payload: dict[str, Any] = {
        "name": title,
        "message": message,
        "domain": DOMAIN,
    }
    if eid:
        payload["entity_id"] = eid
    hass.bus.async_fire("logbook_entry", payload)

    # 2. Toast — auto-dismissing persistent notification.
    notif_id = (
        f"{notification_id_prefix}_"
        f"{int(dt_util.utcnow().timestamp() * 1000)}"
    )
    await hass.services.async_call(
        "persistent_notification", "create",
        {
            "notification_id": notif_id,
            "title": title,
            "message": message,
        },
        blocking=False,
    )

    async def _dismiss(_now: Any = None) -> None:
        try:
            await hass.services.async_call(
                "persistent_notification", "dismiss",
                {"notification_id": notif_id},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            # Notification may have been manually dismissed; not fatal.
            pass

    async_call_later(hass, toast_seconds, _dismiss)


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
        applied = call.data.get("applied_value_mL")
        prev = call.data.get("prev_value_mL")

        # Both values can be inferred when the user invokes this from
        # the dashboard's no-payload Acknowledge button. The intent is
        # "I applied the current suggestion", so:
        #   - applied = current `suggested_dose_mL`
        #   - prev    = the live programmed dose right now (sum of
        #               daily_dose across configured alk heads), since
        #               that's what the user just bumped FROM.
        from .alk_advisor import (
            OPT_ALK_HEADS, _opt, _sum_dose_mL, compute_for_entity,
        )

        if applied is None:
            rec = compute_for_entity(hass, coordinator)
            if rec is not None and rec.suggested_dose_mL is not None:
                applied = float(rec.suggested_dose_mL)
            else:
                _LOGGER.warning(
                    "acknowledge_alk_recommendation called with no "
                    "applied_value_mL and no current recommendation to "
                    "infer from — recording 0.0",
                )
                applied = 0.0

        if prev is None:
            live = _sum_dose_mL(
                hass, list(_opt(coordinator, OPT_ALK_HEADS) or []),
            )
            if live is not None:
                prev = float(live)
            else:
                # Last-resort fallback: latest snapshot dose. This is what
                # the previous version did. Only happens when the alk
                # heads aren't readable for some reason.
                for s in reversed(coordinator.advisor_snapshots("kh")):
                    if s.get("dose_mL") is not None:
                        prev = float(s["dose_mL"])
                        break

        await coordinator.async_record_advisor_acknowledgment(
            "kh",
            applied_value_mL=float(applied),
            prev_value_mL=float(prev) if prev is not None else 0.0,
        )
        _LOGGER.warning(
            "Acknowledged alk recommendation: applied=%s prev=%s",
            applied, prev,
        )
        await _show_action_feedback(
            hass,
            title="Alk recommendation acknowledged",
            message=(
                f"Applied {applied:.2f} mL/day "
                f"(was {float(prev) if prev is not None else 0.0:.2f}). "
                f"Advisor enters cooldown."
            ),
            advisor_unique_id="reef_alk_advisor_recommendation",
        )

    async def handle_dismiss_alk(call: ServiceCall) -> None:
        suggested = float(call.data.get("suggested_value_mL", 0.0))
        await coordinator.async_record_advisor_dismissal(
            "kh", suggested_value_mL=suggested,
        )
        await _show_action_feedback(
            hass,
            title="Alk recommendation dismissed",
            message=(
                f"Ignored suggested {suggested:.2f} mL/day. "
                f"Advisor enters dismiss-cooldown."
            ),
            advisor_unique_id="reef_alk_advisor_recommendation",
        )

    async def handle_acknowledge_advisor(call: ServiceCall) -> None:
        """Generic acknowledge for any advisor parameter.

        Mirrors the alk handler's no-payload UX (button click on the
        dashboard with no args fills in `applied`/`prev` from the
        current recommendation + live doser state) but routes through
        the param-aware code path. For kh this duplicates the alk
        handler's behaviour; for ca/mg/no3/po4 it uses
        `param_advisor.compute_for_param` and reads the per-element
        `_heads` option.
        """
        parameter = call.data["parameter"]
        applied = call.data.get("applied_value_mL")
        prev = call.data.get("prev_value_mL")

        # Resolve current recommendation + heads using the right code
        # path for this parameter.
        from . import param_advisor as pa
        from .alk_advisor import (
            OPT_ALK_HEADS,
            _opt as alk_opt,
            _sum_dose_mL as alk_sum,
            compute_for_entity as alk_compute,
        )

        if parameter == "kh":
            current_rec = alk_compute(hass, coordinator)
            heads = list(alk_opt(coordinator, OPT_ALK_HEADS) or [])
            live_dose = alk_sum(hass, heads)
        else:
            current_rec = pa.compute_for_param(hass, coordinator, parameter)
            heads = list(
                pa._opt(coordinator, pa.opt_key(parameter, "heads"), [])
                or []
            )
            live_dose = pa._sum_dose_mL(hass, heads)

        if applied is None:
            if current_rec is not None and current_rec.suggested_dose_mL is not None:
                applied = float(current_rec.suggested_dose_mL)
            else:
                _LOGGER.warning(
                    "acknowledge_advisor parameter=%s called with no "
                    "applied_value_mL and no current recommendation — "
                    "recording 0.0", parameter,
                )
                applied = 0.0

        if prev is None:
            if live_dose is not None:
                prev = float(live_dose)
            else:
                # Fallback to last snapshot's recorded dose. Same
                # last-resort path as the alk handler — only fires if
                # heads aren't readable.
                for s in reversed(coordinator.advisor_snapshots(parameter)):
                    if s.get("dose_mL") is not None:
                        prev = float(s["dose_mL"])
                        break

        await coordinator.async_record_advisor_acknowledgment(
            parameter,
            applied_value_mL=float(applied),
            prev_value_mL=float(prev) if prev is not None else 0.0,
        )
        _LOGGER.warning(
            "Acknowledged %s recommendation: applied=%s prev=%s",
            parameter, applied, prev,
        )
        # Per-param sensor unique_id pattern matches dashboard.py's
        # `reef_<param>_advisor_recommendation` convention. For kh the
        # alk advisor uses `reef_alk_advisor_recommendation` (legacy
        # naming — predates the per-element framework).
        unique_id = (
            "reef_alk_advisor_recommendation"
            if parameter == "kh"
            else f"reef_{parameter}_advisor_recommendation"
        )
        prev_str = (
            f"{float(prev):.2f}" if prev is not None else "0.00"
        )
        await _show_action_feedback(
            hass,
            title=f"{parameter.capitalize()} recommendation acknowledged",
            message=(
                f"Applied {applied:.2f} mL/day (was {prev_str}). "
                f"Advisor enters cooldown."
            ),
            advisor_unique_id=unique_id,
        )

    async def handle_dismiss_advisor(call: ServiceCall) -> None:
        """Generic dismiss for any advisor parameter."""
        parameter = call.data["parameter"]
        suggested = float(call.data.get("suggested_value_mL", 0.0))
        await coordinator.async_record_advisor_dismissal(
            parameter, suggested_value_mL=suggested,
        )
        unique_id = (
            "reef_alk_advisor_recommendation"
            if parameter == "kh"
            else f"reef_{parameter}_advisor_recommendation"
        )
        await _show_action_feedback(
            hass,
            title=f"{parameter.capitalize()} recommendation dismissed",
            message=(
                f"Ignored suggested {suggested:.2f} mL/day. "
                f"Advisor enters dismiss-cooldown."
            ),
            advisor_unique_id=unique_id,
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
            eff_dkh_per_mL_per_100L=call.data.get("eff_dkh_per_mL_per_100L"),
            eff_per_mL_per_100L=call.data.get("eff_per_mL_per_100L"),
            param_id=call.data.get("param_id", "kh"),
            label_patterns=call.data.get("label_patterns"),
            notes=call.data.get("notes"),
        )
        _LOGGER.warning(
            "Added supplement profile id=%s label=%r param_id=%s "
            "eff_dkh=%s eff=%s patterns=%s notes=%r",
            entry["id"], entry["label"], entry.get("param_id", "kh"),
            entry.get("eff_dkh_per_mL_per_100L"),
            entry.get("eff_per_mL_per_100L"),
            entry["label_patterns"], entry.get("notes"),
        )

    async def handle_remove_supplement_profile(call: ServiceCall) -> None:
        await coordinator.async_remove_supplement_profile(call.data["id"])

    async def handle_update_supplement_profile(call: ServiceCall) -> None:
        """Update fields on an existing supplement profile in place.

        Sentinel-style: only the fields you pass change. Existing
        fields not mentioned are preserved. Use case: profiles
        registered before per-element potency support (0.4.4 era)
        can have `eff_per_mL_per_100L` filled in without
        remove + re-add. The coordinator's _UNSET sentinel handles
        the missing-kwarg case automatically; this handler just
        passes through whichever kwargs the caller specified."""
        kwargs: dict[str, Any] = {}
        if "eff_dkh_per_mL_per_100L" in call.data:
            kwargs["eff_dkh_per_mL_per_100L"] = call.data["eff_dkh_per_mL_per_100L"]
        if "eff_per_mL_per_100L" in call.data:
            kwargs["eff_per_mL_per_100L"] = call.data["eff_per_mL_per_100L"]
        if "param_id" in call.data:
            kwargs["param_id"] = call.data["param_id"]
        if "label_patterns" in call.data:
            kwargs["label_patterns"] = call.data["label_patterns"]
        if "notes" in call.data:
            kwargs["notes"] = call.data["notes"]
        if not kwargs:
            _LOGGER.warning(
                "update_supplement_profile called with id=%s but no "
                "fields to update — pass at least one of "
                "eff_per_mL_per_100L / eff_dkh_per_mL_per_100L / "
                "param_id / label_patterns / notes",
                call.data["id"],
            )
            return
        try:
            entry = await coordinator.async_update_supplement_profile(
                call.data["id"], **kwargs,
            )
        except ValueError as exc:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(str(exc)) from exc
        _LOGGER.warning(
            "Updated supplement profile id=%s label=%r — applied: %s",
            entry["id"], entry["label"], list(kwargs),
        )
        await _show_action_feedback(
            hass,
            title="Supplement profile updated",
            message=(
                f"{entry['label']}: {', '.join(kwargs)} updated. "
                f"Per-element advisor will recompute on next snapshot."
            ),
            advisor_unique_id=None,
            notification_id_prefix="reef_supp_update",
        )
        _LOGGER.warning("Removed supplement profile id=%s", call.data["id"])

    async def handle_list_supplement_profiles(call: ServiceCall) -> None:
        # Local import to avoid hoisting alk_advisor's HA-helper imports
        # into the top of __init__.py.
        from .alk_advisor import BUILTIN_PROFILES
        # Group BOTH builtin (always KH) + user profiles by param_id so
        # the user can see the full registry at a glance — useful when
        # debugging "why doesn't the alk advisor see my new supplement"
        # (answer: param_id != "kh"). Multi-target user profiles
        # (e.g. NO3:PO4-X with param_id=["nitrate","phosphate"]) appear
        # in EACH of their target groups so per-element coverage is
        # visually obvious.
        by_param: dict[str, list[tuple[str, str, dict]]] = {}
        for pid, prof in BUILTIN_PROFILES.items():
            by_param.setdefault("kh", []).append(("BUILTIN", pid, prof))
        for u in coordinator.supplement_profiles:
            stored = u.get("param_id", "kh")
            target_pids = [stored] if isinstance(stored, str) else list(stored)
            for tp in target_pids:
                by_param.setdefault(tp, []).append(("USER", u["id"], u))

        lines = [
            "Supplement profiles (BUILTIN unless tagged USER), grouped by param_id:",
        ]
        # Builtin sentinels (auto, custom) have eff=None by design
        # — they're not "non-KH supplements", just sentinels. Don't
        # mislabel them.
        SENTINELS = {"auto", "custom"}
        for param_id in sorted(by_param):
            lines.append(f"\n  [{param_id}]")
            for tag, pid, prof in by_param[param_id]:
                eff = prof.get("eff_dkh_per_mL_per_100L")
                if pid in SENTINELS:
                    eff_s = "(sentinel — no fixed potency)"
                elif eff is not None and param_id == "kh":
                    eff_s = f"{eff} dKH/mL/100L"
                else:
                    eff_s = "(n/a — non-KH supplement)"
                line = f"    [{tag}] {pid} — {prof['label']} — {eff_s}"
                if tag == "USER":
                    pats = prof.get("label_patterns") or []
                    if pats:
                        line += f"  patterns={pats}"
                    # If multi-target, note the other params it also
                    # covers so the user sees the cross-param wiring.
                    stored = prof.get("param_id", "kh")
                    targets = [stored] if isinstance(stored, str) else list(stored)
                    others = [t for t in targets if t != param_id]
                    if others:
                        line += f"  also_targets={others}"
                    if prof.get("notes"):
                        line += f"  notes={prof['notes']!r}"
                lines.append(line)
        _LOGGER.warning("\n".join(lines))

    async def handle_log_water_change(call: ServiceCall) -> None:
        await coordinator.async_record_water_change(
            call.data.get("parameter", "kh"),
            percent=call.data["percent"],
            salt_mix_kh=call.data.get("salt_mix_kh"),
            notes=call.data.get("notes"),
        )

    async def handle_import_triton_url(call: ServiceCall) -> None:
        from .icp_importer import import_triton_url, ParserError
        url = call.data["url"]
        sample_date = call.data.get("sample_date")
        habitat = call.data.get("habitat")
        problem = call.data.get("problem")
        try:
            summary = await import_triton_url(
                hass, coordinator, url,
                sample_date=sample_date,
                habitat=habitat,
                problem=problem,
            )
        except ParserError as exc:
            _LOGGER.error(
                "Triton URL import failed: %s (debug bundle: %s)",
                exc, exc.debug_path,
            )
            # Re-raise as a HomeAssistantError so the service-call dialog
            # surfaces the message directly to the user. (vol-level
            # ServiceValidationError would also work but ties us to a
            # specific HA version's import path.)
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(str(exc)) from exc
        _LOGGER.warning("Triton URL imported: %s", summary)

    async def handle_submit_water_change_form(call: ServiceCall) -> None:
        """Read the dashboard's water-change form fields and call
        log_water_change. The dashboard's "Submit" button invokes this
        with no payload — HA's `entities` card `call-service` rows
        don't render templates in service_data, so the values have to
        be read here at call time."""
        percent = coordinator.advisor_form_value("wc_percent") or 10.0
        salt = coordinator.advisor_form_value("wc_salt_mix_kh")
        notes = coordinator.advisor_form_value("wc_notes") or None
        if isinstance(notes, str) and not notes.strip():
            notes = None
        await coordinator.async_record_water_change(
            "kh",
            percent=float(percent),
            salt_mix_kh=float(salt) if salt is not None else None,
            notes=notes,
        )
        _LOGGER.warning(
            "Submitted water change from form: percent=%s salt_mix_kh=%s notes=%r",
            percent, salt, notes,
        )
        salt_msg = (
            f", salt mix KH {float(salt):.1f}" if salt is not None else ""
        )
        notes_msg = f" — {notes}" if notes else ""
        await _show_action_feedback(
            hass,
            title="Water change logged",
            message=(
                f"{float(percent):.0f}% volume changed{salt_msg}{notes_msg}. "
                f"Snapshots in the settling window will be excluded."
            ),
            advisor_unique_id="reef_alk_advisor_recommendation",
        )

    async def handle_submit_demand_change_form(call: ServiceCall) -> None:
        """Read the demand-change form fields and call log_demand_change."""
        reason = coordinator.advisor_form_value("demand_reason") or ""
        direction = coordinator.advisor_form_value("demand_direction") or "unknown"
        if direction not in ("increase", "decrease", "unknown"):
            direction = "unknown"
        magnitude = coordinator.advisor_form_value("demand_magnitude_pct")
        if isinstance(reason, str) and not reason.strip():
            _LOGGER.warning(
                "submit_demand_change_form called with empty reason — "
                "recording with placeholder text",
            )
            reason = "(no reason provided)"
        await coordinator.async_record_advisor_demand_change(
            "kh",
            reason=str(reason),
            expected_direction=str(direction),
            magnitude_hint_pct=(
                float(magnitude) if magnitude is not None else None
            ),
        )
        _LOGGER.warning(
            "Submitted demand change from form: reason=%r direction=%s magnitude=%s",
            reason, direction, magnitude,
        )
        mag_msg = (
            f", ~{float(magnitude):.0f}% magnitude"
            if magnitude is not None else ""
        )
        await _show_action_feedback(
            hass,
            title="Demand change logged",
            message=(
                f"{str(direction).capitalize()}{mag_msg}: {reason}. "
                f"Advisor enters learning mode until "
                f"{ADVISOR_DEFAULTS.get('min_samples_after_event', 3)} "
                f"snapshots after now."
            ),
            advisor_unique_id="reef_alk_advisor_recommendation",
        )

    async def handle_submit_icp_import_form(call: ServiceCall) -> None:
        """Read the dashboard's ICP-import form fields and call
        import_triton_url. Mirror of submit_water_change_form — the
        dashboard's call-service row can't render templates in
        service_data, so we read the form entity states server-side.

        URL is required (raises HomeAssistantError if blank).
        Habitat / problem default to the current tank state if the form
        selects haven't been changed (those selects fall back to tank
        state in their `current_option`, so the form_value lookup here
        does the same — explicit fallback to coordinator.tank).
        Sample_date is optional; blank → service defaults to today UTC.
        """
        url = (coordinator.advisor_form_value("icp_url") or "").strip()
        if not url:
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(
                "ICP import URL is empty — paste a Triton showroom URL "
                "into the form field before submitting."
            )

        habitat = (
            coordinator.advisor_form_value("icp_habitat")
            or coordinator.tank.get("habitat")
        )
        problem = (
            coordinator.advisor_form_value("icp_problem")
            or coordinator.tank.get("problem")
        )
        sample_date = (
            coordinator.advisor_form_value("icp_sample_date") or ""
        ).strip() or None

        from .icp_importer import import_triton_url
        try:
            summary = await import_triton_url(
                hass, coordinator, url,
                habitat=habitat, problem=problem,
                sample_date=sample_date,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("submit_icp_import_form failed: %s", exc)
            from homeassistant.exceptions import HomeAssistantError
            raise HomeAssistantError(str(exc)) from exc
        _LOGGER.warning("ICP form import: %s", summary)
        # Toast the headline. The full plan rendering happens on the
        # Dosing Plan view; the toast just confirms import succeeded.
        await _show_action_feedback(
            hass,
            title="ICP test imported",
            message=(
                f"Habitat={habitat}, problem={problem}. "
                f"View the new dose plan on the Dosing Plan dashboard."
            ),
            advisor_unique_id=None,  # not advisor-specific
            notification_id_prefix="reef_icp",
        )

    async def handle_capture_snapshot(call: ServiceCall) -> None:
        """Capture an alk advisor snapshot.

        With no args: reads `kh_source` and sums dose across the
        configured alk heads, stamps with current time. Useful in prod
        for manual baselines (e.g. after calibrating the KH Keeper).

        With overrides: persists exactly what you pass. Useful in dev
        to seed synthetic history so the algorithm has enough data to
        produce a recommendation immediately.
        """
        from .alk_advisor import (
            OPT_ALK_HEADS, OPT_KH_SOURCE,
            _opt, _read_float_state, _sum_dose_mL,
        )
        from datetime import datetime, timezone

        at = call.data.get("at") or (
            datetime.now(timezone.utc).astimezone().isoformat()
        )

        if "kh" in call.data:
            kh: float | None = float(call.data["kh"])
        else:
            kh = _read_float_state(hass, _opt(coordinator, OPT_KH_SOURCE) or "")

        if "dose_mL" in call.data:
            dose_mL: float | None = float(call.data["dose_mL"])
        else:
            dose_mL = _sum_dose_mL(
                hass, list(_opt(coordinator, OPT_ALK_HEADS) or []),
            )

        await coordinator.async_record_advisor_snapshot(
            "kh", at=at, kh=kh, dose_mL=dose_mL,
        )
        _LOGGER.warning(
            "Captured alk snapshot: at=%s kh=%s dose_mL=%s",
            at, kh, dose_mL,
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
            DOMAIN, SERVICE_ACK_ADVISOR, handle_acknowledge_advisor,
            schema=ACK_ADVISOR_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_DISMISS_ADVISOR, handle_dismiss_advisor,
            schema=DISMISS_ADVISOR_SCHEMA,
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
            DOMAIN, SERVICE_UPDATE_SUPPLEMENT_PROFILE,
            handle_update_supplement_profile,
            schema=UPDATE_SUPPLEMENT_PROFILE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LIST_SUPPLEMENT_PROFILES, handle_list_supplement_profiles,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_LOG_WATER_CHANGE, handle_log_water_change,
            schema=LOG_WATER_CHANGE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_CAPTURE_SNAPSHOT, handle_capture_snapshot,
            schema=CAPTURE_SNAPSHOT_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SUBMIT_WC_FORM, handle_submit_water_change_form,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SUBMIT_DEMAND_FORM, handle_submit_demand_change_form,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_TRITON_URL, handle_import_triton_url,
            schema=IMPORT_TRITON_URL_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, SERVICE_SUBMIT_ICP_FORM, handle_submit_icp_import_form,
        )
