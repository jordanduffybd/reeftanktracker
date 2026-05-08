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
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
)

from . import alk_advisor
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
        """Landing page — pick which sub-section to edit.

        HA's translation system looks up `options.step.init.menu_options`
        regardless of what this step returns. Keep `init` as the menu
        step itself (rather than redirecting to a separate `menu` step)
        so the labels resolve.
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "sources",
                "targets",
                "advisor",
                "advisor_calcium",
                "advisor_magnesium",
                # 0.5.3+ will add: "advisor_nitrate", "advisor_phosphate"
                # — same pattern, different param_id, plus multi-supplement
                # coordination + remover semantics.
            ],
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Per-parameter auto-source sensor picker (was the original Options page)."""
        if user_input is not None:
            # Merge into existing options so this page doesn't wipe advisor settings.
            merged = dict(self._entry.options)
            # Strip empty entries from incoming form before merging.
            for k, v in user_input.items():
                if v:
                    merged[k] = v
                else:
                    merged.pop(k, None)
            return self.async_create_entry(title="", data=merged)

        schema_dict: dict[Any, Any] = {}
        suggested: dict[str, Any] = {}
        for p in INPUT_PARAMETERS:
            key = auto_source_key(p["id"])
            current = self._entry.options.get(key, p.get("auto_source"))
            schema_dict[vol.Optional(key)] = EntitySelector(
                EntitySelectorConfig(domain=["sensor", "input_number"])
            )
            if current:
                suggested[key] = current

        schema = self.add_suggested_values_to_schema(
            vol.Schema(schema_dict), suggested
        )
        return self.async_show_form(
            step_id="sources",
            data_schema=schema,
            description_placeholders={
                "param_count": str(len(INPUT_PARAMETERS)),
            },
        )

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Per-parameter target-range overrides.

        Defaults come from `parameters.py` (the static `default_target_min/max`).
        User-set values land in `entry.options` as `target_<param_id>_min` /
        `target_<param_id>_max` and are read by `coordinator.get_target_range`.
        Empty values fall back to the static default.

        Only `INPUT_PARAMETERS` (the home-testable subset) get rows —
        ICP-only params can be added later if needed.
        """
        if user_input is not None:
            merged = dict(self._entry.options)
            for k, v in user_input.items():
                if v in (None, ""):
                    merged.pop(k, None)
                else:
                    merged[k] = v
            return self.async_create_entry(title="", data=merged)

        opts = self._entry.options

        def _num(*, mn: float, mx: float, step: float) -> Any:
            return NumberSelector(NumberSelectorConfig(
                mode="box", min=mn, max=mx, step=step,
            ))

        schema_dict: dict[Any, Any] = {}
        suggested: dict[str, Any] = {}
        for p in INPUT_PARAMETERS:
            min_key = f"target_{p['id']}_min"
            max_key = f"target_{p['id']}_max"
            # Use the parameter's own min/max as the form bounds — keeps
            # the selector consistent with the entry-row constraints
            # users already see for that parameter.
            mn = float(p.get("min", 0))
            mx = float(p.get("max", 1000))
            step = float(p.get("step", 0.01))
            schema_dict[vol.Optional(min_key)] = _num(mn=mn, mx=mx, step=step)
            schema_dict[vol.Optional(max_key)] = _num(mn=mn, mx=mx, step=step)
            suggested[min_key] = opts.get(min_key, p.get("default_target_min"))
            suggested[max_key] = opts.get(max_key, p.get("default_target_max"))

        schema = self.add_suggested_values_to_schema(
            vol.Schema(schema_dict), suggested,
        )
        return self.async_show_form(
            step_id="targets",
            data_schema=schema,
        )

    async def async_step_advisor(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Alk advisor configuration page — enabled toggle, alk heads,
        KH source, calibration warning entity, target band, supplement
        spec, tank volume, and all algorithm tunables.

        The supplement_profile dropdown is rebuilt from the merged
        builtin + user-added profiles each time this form opens, so
        freshly-added profiles appear without a reload.
        """
        if user_input is not None:
            merged = dict(self._entry.options)
            for k, v in user_input.items():
                if v in (None, "", []):
                    merged.pop(k, None)
                else:
                    merged[k] = v
            return self.async_create_entry(title="", data=merged)

        opts = self._entry.options
        defaults = alk_advisor.DEFAULTS
        OPT = alk_advisor

        # Pull the coordinator so the dropdown reflects user-added profiles.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        profile_options = (
            alk_advisor.all_profiles(coordinator)
            if coordinator is not None else dict(alk_advisor.BUILTIN_PROFILES)
        )

        def _num(key: str, *, mn: float, mx: float, step: float) -> Any:
            return NumberSelector(NumberSelectorConfig(
                mode="box", min=mn, max=mx, step=step,
            ))

        schema_dict: dict[Any, Any] = {
            vol.Optional(OPT.OPT_ADVISOR_ENABLED): BooleanSelector(),
            # Alk-head daily-dose sensors are domain=sensor with
            # device_class=volume (Reefbeat publishes them with mL units
            # + volume class). Filtering keeps the dropdown to actual
            # dose-like sensors instead of hundreds of unrelated entities.
            vol.Optional(OPT.OPT_ALK_HEADS): EntitySelector(
                EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["volume"],
                    multiple=True,
                ),
            ),
            # KH Keeper publishes `sensor.kh_keeper_kh` with no specific
            # device_class. Best filter: numeric sensor — but HA's
            # selector doesn't have a "numeric" filter, so leave domain
            # = sensor + input_number (input_number is also valid for
            # users who want to back the advisor with a manually-set
            # helper).
            vol.Optional(OPT.OPT_KH_SOURCE): EntitySelector(
                EntitySelectorConfig(domain=["sensor", "input_number"]),
            ),
            # Calibration warning is a binary_sensor with
            # device_class=problem from KH Keeper.
            vol.Optional(OPT.OPT_CALIBRATION_WARNING_ENTITY): EntitySelector(
                EntitySelectorConfig(
                    domain=["binary_sensor"],
                    device_class=["problem"],
                ),
            ),
            vol.Optional(OPT.OPT_TANK_VOLUME_L): _num(
                OPT.OPT_TANK_VOLUME_L, mn=10, mx=10000, step=1,
            ),
            vol.Optional(OPT.OPT_SUPPLEMENT_PROFILE): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": pid, "label": prof["label"]}
                        for pid, prof in profile_options.items()
                    ],
                    mode="dropdown",
                ),
            ),
            vol.Optional(OPT.OPT_SUPPLEMENT_EFF): _num(
                OPT.OPT_SUPPLEMENT_EFF, mn=0.01, mx=5.0, step=0.001,
            ),
            vol.Optional(OPT.OPT_TARGET_MIN): _num(
                OPT.OPT_TARGET_MIN, mn=4.0, mx=14.0, step=0.05,
            ),
            vol.Optional(OPT.OPT_TARGET_MAX): _num(
                OPT.OPT_TARGET_MAX, mn=4.0, mx=14.0, step=0.05,
            ),
            vol.Optional(OPT.OPT_WINDOW_DAYS): _num(
                OPT.OPT_WINDOW_DAYS, mn=2, mx=30, step=1,
            ),
            vol.Optional(OPT.OPT_MIN_SAMPLES): _num(
                OPT.OPT_MIN_SAMPLES, mn=2, mx=30, step=1,
            ),
            vol.Optional(OPT.OPT_MIN_TREND_DAYS): _num(
                OPT.OPT_MIN_TREND_DAYS, mn=1, mx=14, step=1,
            ),
            vol.Optional(OPT.OPT_COOLDOWN_DAYS): _num(
                OPT.OPT_COOLDOWN_DAYS, mn=0, mx=30, step=0.5,
            ),
            vol.Optional(OPT.OPT_DISMISS_COOLDOWN_DAYS): _num(
                OPT.OPT_DISMISS_COOLDOWN_DAYS, mn=0, mx=30, step=0.5,
            ),
            vol.Optional(OPT.OPT_STEP_CAP_PCT): _num(
                OPT.OPT_STEP_CAP_PCT, mn=1, mx=100, step=1,
            ),
            vol.Optional(OPT.OPT_HYSTERESIS): _num(
                OPT.OPT_HYSTERESIS, mn=0, mx=2.0, step=0.01,
            ),
            vol.Optional(OPT.OPT_MIN_SAMPLES_AFTER_EVENT): _num(
                OPT.OPT_MIN_SAMPLES_AFTER_EVENT, mn=1, mx=14, step=1,
            ),
            vol.Optional(OPT.OPT_CORRECTION_PERIOD_DAYS): _num(
                OPT.OPT_CORRECTION_PERIOD_DAYS, mn=1, mx=30, step=0.5,
            ),
            vol.Optional(OPT.OPT_SNAPSHOT_HOUR): _num(
                OPT.OPT_SNAPSHOT_HOUR, mn=0, mx=23, step=1,
            ),
            vol.Optional(OPT.OPT_SNAPSHOT_MINUTE): _num(
                OPT.OPT_SNAPSHOT_MINUTE, mn=0, mx=59, step=1,
            ),
            vol.Optional(OPT.OPT_EMPIRICAL_DRIFT_PCT): _num(
                OPT.OPT_EMPIRICAL_DRIFT_PCT, mn=10, mx=200, step=5,
            ),
            vol.Optional(OPT.OPT_WC_SETTLING_HOURS): _num(
                OPT.OPT_WC_SETTLING_HOURS, mn=1, mx=168, step=1,
            ),
        }

        suggested: dict[str, Any] = {}
        for k, default in defaults.items():
            suggested[k] = opts.get(k, default)

        schema = self.add_suggested_values_to_schema(
            vol.Schema(schema_dict), suggested,
        )
        return self.async_show_form(
            step_id="advisor",
            data_schema=schema,
        )

    async def async_step_advisor_calcium(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Calcium dosing advisor configuration.

        Same shape as the alk advisor page but parameter-aware. The
        underlying compute is `param_advisor.compute_for_param(param_id="calcium")`.
        Per-element supplements are filtered to those registered with
        `param_id="calcium"` (see `coordinator.supplement_profiles_for`).
        """
        return await self._render_param_advisor_step(
            user_input, param_id="calcium",
        )

    async def async_step_advisor_magnesium(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Magnesium dosing advisor configuration. Same shape as Ca."""
        return await self._render_param_advisor_step(
            user_input, param_id="magnesium",
        )

    async def _render_param_advisor_step(
        self, user_input: dict[str, Any] | None, *, param_id: str,
    ) -> FlowResult:
        """Shared renderer for per-element advisor pages. Each per-param
        page is a thin wrapper over this helper — the page-specific
        details come from `param_advisor.PARAM_DEFAULTS[param_id]`."""
        from . import param_advisor

        if user_input is not None:
            merged = dict(self._entry.options)
            for k, v in user_input.items():
                if v in (None, "", []):
                    merged.pop(k, None)
                else:
                    merged[k] = v
            return self.async_create_entry(title="", data=merged)

        opts = self._entry.options
        defaults = param_advisor.PARAM_DEFAULTS.get(param_id, {})

        # Build the supplement profile dropdown from coordinator's
        # filtered profiles (only those tagged with this param_id).
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        profiles = (
            coordinator.supplement_profiles_for(param_id)
            if coordinator is not None else []
        )
        # Always offer "custom" so the user can fall back to a manual
        # spec efficiency without registering a profile.
        profile_options = [
            {"value": "custom", "label": "Custom (use the manual efficiency value below)"},
        ]
        for p in profiles:
            profile_options.append({"value": p["id"], "label": p["label"]})

        def _opt_key(suffix: str) -> str:
            return param_advisor.opt_key(param_id, suffix)

        def _num(*, mn: float, mx: float, step: float) -> Any:
            return NumberSelector(NumberSelectorConfig(
                mode="box", min=mn, max=mx, step=step,
            ))

        # Reasonable widget bounds per parameter — these are SCHEMA
        # bounds (form validation), not algorithm constraints. Set wide
        # so any plausible reef value is acceptable; the actual safety
        # guards live in the algorithm.
        SCHEMA_BOUNDS: dict[str, dict[str, tuple[float, float, float]]] = {
            "calcium": {
                "target": (300.0, 600.0, 1.0),
                "hysteresis": (0.0, 50.0, 0.5),
                "spec_eff": (0.1, 50.0, 0.1),  # ppm/mL/100L; FA = 2.0
            },
            "magnesium": {
                # 1100-1500 spans every plausible reef Mg target
                "target": (1100.0, 1500.0, 5.0),
                # Mg test kits are coarser than Ca (±20-30 ppm),
                # so the hysteresis bound goes up to 100.
                "hysteresis": (0.0, 100.0, 1.0),
                # Foundation C = 1.0 ppm Mg / mL / 100L; powder
                # supplements at higher concentrations exist.
                "spec_eff": (0.1, 50.0, 0.1),
            },
        }
        bounds = SCHEMA_BOUNDS.get(param_id, {
            "target": (0.0, 1000.0, 0.1),
            "hysteresis": (0.0, 100.0, 0.1),
            "spec_eff": (0.001, 100.0, 0.001),
        })

        schema_dict: dict[Any, Any] = {
            vol.Optional(_opt_key("enabled")): BooleanSelector(),
            # Doser heads: same EntitySelector filter as alk
            # — domain=sensor, device_class=volume (Reefbeat exposes
            # daily_dose entities with mL units + volume class).
            # The form description warns the user to pick `_daily_dose`
            # entities (NOT `_auto_dosed_today`) — both have
            # device_class=volume so HA can't distinguish them at the
            # selector level.
            vol.Optional(_opt_key("heads")): EntitySelector(
                EntitySelectorConfig(
                    domain=["sensor"],
                    device_class=["volume"],
                    multiple=True,
                ),
            ),
            # NOTE: "source" sensor field intentionally REMOVED in 0.5.1.
            # The non-KH advisors don't use a real-time source sensor —
            # they read snapshots from manual `record_reading` entries.
            # The field was confusing UX and unused. Will reintroduce
            # if/when a Ca/Mg/NO3/PO4 auto-tester (Mastertronic Essential)
            # arrives and we want to poll it.
            vol.Optional(_opt_key("supplement_profile")): SelectSelector(
                SelectSelectorConfig(
                    options=profile_options,
                    mode="dropdown",
                ),
            ),
            vol.Optional(_opt_key("spec_efficiency")): _num(
                mn=bounds["spec_eff"][0], mx=bounds["spec_eff"][1],
                step=bounds["spec_eff"][2],
            ),
            vol.Optional(_opt_key("target_min")): _num(
                mn=bounds["target"][0], mx=bounds["target"][1],
                step=bounds["target"][2],
            ),
            vol.Optional(_opt_key("target_max")): _num(
                mn=bounds["target"][0], mx=bounds["target"][1],
                step=bounds["target"][2],
            ),
            # Widened bounds (0.5.2) — the per-element advisors use
            # sparse-cadence defaults (window=90, cooldown=30,
            # correction=30 for Ca/Mg). The original alk-advisor
            # bounds (max=30) rejected those values. Bumped to
            # year-scale ceilings so any plausible reef-care cadence
            # fits.
            vol.Optional(_opt_key("window_days")): _num(mn=2, mx=365, step=1),
            vol.Optional(_opt_key("min_samples")): _num(mn=1, mx=60, step=1),
            vol.Optional(_opt_key("min_trend_days")): _num(mn=1, mx=30, step=1),
            vol.Optional(_opt_key("cooldown_days")): _num(mn=0, mx=180, step=0.5),
            vol.Optional(_opt_key("dismiss_cooldown_days")): _num(
                mn=0, mx=180, step=0.5,
            ),
            vol.Optional(_opt_key("step_cap_pct")): _num(mn=1, mx=100, step=1),
            vol.Optional(_opt_key("hysteresis")): _num(
                mn=bounds["hysteresis"][0], mx=bounds["hysteresis"][1],
                step=bounds["hysteresis"][2],
            ),
            vol.Optional(_opt_key("min_samples_after_event")): _num(
                mn=1, mx=30, step=1,
            ),
            vol.Optional(_opt_key("correction_period_days")): _num(
                mn=1, mx=180, step=0.5,
            ),
        }

        # Pre-fill suggested values from existing options OR the
        # parameter's defaults. Same pattern as the alk advisor.
        suggested: dict[str, Any] = {}
        for setting in (
            "enabled", "heads", "supplement_profile",
            "spec_efficiency", "target_min", "target_max", "window_days",
            "min_samples", "min_trend_days", "cooldown_days",
            "dismiss_cooldown_days", "step_cap_pct", "hysteresis",
            "min_samples_after_event", "correction_period_days",
        ):
            k = _opt_key(setting)
            if k in opts:
                suggested[k] = opts[k]
            elif setting in defaults:
                suggested[k] = defaults[setting]

        schema = self.add_suggested_values_to_schema(
            vol.Schema(schema_dict), suggested,
        )
        return self.async_show_form(
            step_id=f"advisor_{param_id}",
            data_schema=schema,
        )
