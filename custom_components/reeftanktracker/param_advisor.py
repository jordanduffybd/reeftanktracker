"""Generalized per-parameter dosing advisor.

Wraps the parameter-agnostic `advisor.compute_recommendation` algorithm
in a config + state-resolution layer that handles non-KH parameters
(Calcium, Magnesium, eventually Nitrate/Phosphate).

Why this is separate from `alk_advisor.py`:
- The alk advisor is the canonical KH implementation, in production for
  weeks. Refactoring it carries breakage risk.
- This module instead handles the new per-element advisors. The alk
  advisor stays unchanged for now; a future cleanup can collapse the
  duplication once the per-element work is proven.

Per-parameter defaults are sourced from research (see
`internal/docs/pending-improvements.md` and the dosing-research memory).
Different parameters need different cooldowns (Mg moves slower than Ca)
and different hysteresis (KH uses 0.1 dKH; Ca needs ~5 ppm).

Auto-source vs manual-only: KH has a real-time sensor (KH Keeper) that
the daily snapshotter polls. Ca/Mg currently have no auto-source — the
snapshot path uses the user's manual `record_reading` entries (the
listener wires up in `coordinator.async_record_reading`).
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State

from .advisor import (
    Acknowledgment,
    AdvisorConfig,
    DemandChange,
    Dismissal,
    Recommendation,
    Snapshot,
    WaterChange,
    compute_recommendation,
)
from .coordinator import ReefDataCoordinator

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-parameter defaults — algorithm constants tuned per-element from
# research consensus (Holmes-Farley, BRStv, Red Sea, Quantum). See
# `~/.claude/projects/.../memory/reference_reef_dosing_research.md` for
# justification.
# ---------------------------------------------------------------------------
PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "calcium": {
        # Holmes-Farley: 380–450 fine for any reef; SPS 420–440 is the
        # tight target. Default to the SPS-friendly band; user can widen
        # via Options if they're LPS-dominant.
        "target_min": 420.0,
        "target_max": 440.0,
        # Research: hysteresis ~5 ppm — within typical test-kit noise
        # (Salifert / Hanna ULR / Red Sea ±10–20 ppm). 5 ppm catches real
        # drifts without chasing kit jitter.
        "hysteresis": 5.0,
        # Same step cap as alk — 10% is conservative across all elements.
        "step_cap_pct": 10.0,
        # 5 days — Holmes-Farley says 3–5 days minimum to observe a Ca
        # dose change result.
        "cooldown_days": 5.0,
        "dismiss_cooldown_days": 2.0,
        "window_days": 7,
        "min_samples": 5,
        "min_trend_days": 3,
        "min_samples_after_event": 3,
        "correction_period_days": 7.0,
        "empirical_drift_pct": 50.0,
        "wc_settling_hours": 24.0,
        # Foundation A: 1 mL per 100L raises Ca by 2 ppm.
        "default_eff_per_mL_per_100L": 2.0,
        # ppm — used in `Reason` strings.
        "value_unit": "ppm",
    },
    # 0.5.1 will add "magnesium" with its own (slower) cooldown.
}


# ---------------------------------------------------------------------------
# Per-parameter OPT-key naming — flat key strings stored in entry.options.
# Convention: `advisor_<param>_<setting>`. The kh advisor uses unprefixed
# legacy names (e.g. `advisor_enabled`, `advisor_target_min`) — kept
# unchanged for back-compat; new params use the prefixed form.
# ---------------------------------------------------------------------------
def opt_key(param_id: str, setting: str) -> str:
    """Build the OPT key for `(param_id, setting)`.

    `setting` should be a stable suffix like "enabled", "heads",
    "source", "supplement_profile", "spec_efficiency",
    "target_min", "target_max", and the algorithm tunables. The
    full key is `advisor_<param>_<setting>`. The old KH keys
    (`advisor_enabled`, `advisor_target_min`) live alongside the
    new prefixed scheme — readers that want the alk advisor's
    settings still go through `alk_advisor.OPT_*` directly.
    """
    return f"advisor_{param_id}_{setting}"


# ---------------------------------------------------------------------------
# State resolution helpers (mirror of alk_advisor's `_read_float_state`
# / `_sum_dose_mL` / `_is_calibration_warning_on` — duplicated for
# isolation; will collapse if/when alk_advisor.py is migrated to use
# this module).
# ---------------------------------------------------------------------------
def _read_float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state: State | None = hass.states.get(entity_id)
    if state is None or state.state in (
        "unknown", "unavailable", "none", "", None,
    ):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _sum_dose_mL(
    hass: HomeAssistant, entity_ids: list[str],
) -> float | None:
    """Sum the daily-dose state across configured doser heads.

    Returns None if no head returns a valid number — avoids the
    "fabricate 0 mL/day" trap when ReefBeat is briefly offline.
    """
    if not entity_ids:
        return None
    total: float | None = None
    for eid in entity_ids:
        v = _read_float_state(hass, eid)
        if v is None:
            continue
        total = (total or 0.0) + v
    return total


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------
def _opt(coordinator: ReefDataCoordinator, key: str, default: Any = None) -> Any:
    """Read a value from advisor_config (entry.options) or fall back to
    `default`. Returns the configured value verbatim — caller is responsible
    for type coercion."""
    return coordinator.get_advisor_config().get(key, default)


def _resolve_supplement_potency(
    coordinator: ReefDataCoordinator, param_id: str,
) -> tuple[float | None, str]:
    """Pick the supplement potency to use for `param_id`.

    Reads `advisor_<param>_supplement_profile` from options. Resolution:
    - Specific profile id → look up that user profile's
      `eff_per_mL_per_100L` (or `eff_dkh_per_mL_per_100L` for
      back-compat with KH-targeting profiles when called for KH).
    - "custom" → use the manual `advisor_<param>_spec_efficiency`
      override from options.
    - Missing / unknown profile → use the param-level default from
      `PARAM_DEFAULTS[param_id]["default_eff_per_mL_per_100L"]`.

    Returns `(value, source_label)` where `source_label` describes the
    resolution path for diagnostics. `None` value means "not enough info
    to compute" — caller's responsibility to handle (typically returns
    a Recommendation with state=None and a clear reason).
    """
    profile_id = _opt(coordinator, opt_key(param_id, "supplement_profile"))
    defaults = PARAM_DEFAULTS.get(param_id, {})

    # Manual override takes precedence over a "custom" profile selection
    if profile_id == "custom" or profile_id is None:
        manual = _opt(coordinator, opt_key(param_id, "spec_efficiency"))
        if manual is not None:
            try:
                return (float(manual), "custom (manual entry)")
            except (TypeError, ValueError):
                pass
        # Fall through to default if manual is missing/invalid
        fallback = defaults.get("default_eff_per_mL_per_100L")
        if fallback is not None:
            return (float(fallback), f"default ({param_id} parameter built-in)")
        return (None, "no potency configured")

    # Look up a specific user-profile id
    for p in coordinator.supplement_profiles_for(param_id):
        if p["id"] == profile_id:
            # `eff_per_mL_per_100L` is the per-element field (added 0.5.0+).
            # `eff_dkh_per_mL_per_100L` is the legacy KH field. For non-KH
            # params the user typically registered the profile in 0.4.4
            # without a potency value — fall back to the param default.
            eff = p.get("eff_per_mL_per_100L")
            if eff is None:
                eff = p.get("eff_dkh_per_mL_per_100L")
            if eff is not None:
                return (float(eff), f"profile: {p['label']}")
            # Profile selected but has no potency — fall through to default
            fallback = defaults.get("default_eff_per_mL_per_100L")
            if fallback is not None:
                return (
                    float(fallback),
                    f"default ({p['label']} has no potency; using built-in)",
                )
            return (None, f"profile: {p['label']} (no potency)")

    # Profile id unknown (e.g. removed) — fall back to param default
    fallback = defaults.get("default_eff_per_mL_per_100L")
    if fallback is not None:
        return (
            float(fallback),
            f"default ({param_id} parameter built-in; profile {profile_id} not found)",
        )
    return (None, f"profile {profile_id} not found and no default")


def _build_config(
    coordinator: ReefDataCoordinator, param_id: str, *, spec_eff: float | None,
) -> AdvisorConfig:
    """Build an AdvisorConfig for `param_id` from options + defaults."""
    defaults = PARAM_DEFAULTS.get(param_id, {})
    def cfg_get(key: str, dflt: Any) -> Any:
        return _opt(coordinator, opt_key(param_id, key), dflt)

    return AdvisorConfig(
        target_min=float(cfg_get("target_min", defaults.get("target_min", 0.0))),
        target_max=float(cfg_get("target_max", defaults.get("target_max", 0.0))),
        window_days=int(cfg_get("window_days", defaults.get("window_days", 7))),
        min_samples=int(cfg_get("min_samples", defaults.get("min_samples", 5))),
        min_trend_days=int(
            cfg_get("min_trend_days", defaults.get("min_trend_days", 3))
        ),
        cooldown_days=float(
            cfg_get("cooldown_days", defaults.get("cooldown_days", 5.0))
        ),
        dismiss_cooldown_days=float(
            cfg_get(
                "dismiss_cooldown_days",
                defaults.get("dismiss_cooldown_days", 2.0),
            )
        ),
        step_cap_pct=float(
            cfg_get("step_cap_pct", defaults.get("step_cap_pct", 10.0))
        ),
        hysteresis_dkh=float(
            cfg_get("hysteresis", defaults.get("hysteresis", 0.0))
        ),
        min_samples_after_event=int(
            cfg_get(
                "min_samples_after_event",
                defaults.get("min_samples_after_event", 3),
            )
        ),
        correction_period_days=float(
            cfg_get(
                "correction_period_days",
                defaults.get("correction_period_days", 7.0),
            )
        ),
        empirical_drift_pct=float(
            cfg_get(
                "empirical_drift_pct",
                defaults.get("empirical_drift_pct", 50.0),
            )
        ),
        # Algorithm sees this as "spec_efficiency_dkh_per_mL_per_100L"
        # but the unit is param-specific (ppm/mL/100L for Ca, etc.).
        # The dataclass field name carries over from the alk advisor
        # for code reuse; the math is unit-agnostic.
        spec_efficiency_dkh_per_mL_per_100L=float(spec_eff) if spec_eff else 0.0,
        tank_volume_L=float(_opt(coordinator, "advisor_tank_volume_l", 425.0)),
        wc_settling_hours=float(
            cfg_get(
                "wc_settling_hours",
                defaults.get("wc_settling_hours", 24.0),
            )
        ),
        # Display vocabulary for reason text — the algorithm reads
        # these to substitute into format strings ("Calcium median X
        # ppm" instead of the alk-default "KH median X dKH").
        param_label=param_id.capitalize(),
        value_unit=str(defaults.get("value_unit", "ppm")),
    )


# ---------------------------------------------------------------------------
# Per-entity compute — main entrypoint for sensors
# ---------------------------------------------------------------------------
def compute_for_param(
    hass: HomeAssistant, coordinator: ReefDataCoordinator, param_id: str,
) -> Recommendation | None:
    """Build inputs from coordinator + hass state and run the algorithm
    for `param_id`. Returns None if the advisor for this parameter is
    disabled in Options.

    Mirror of `alk_advisor.compute_for_entity` but parameter-aware. The
    alk advisor stays on its own code path for now (back-compat); this
    handles Calcium and (in 0.5.1) Magnesium.
    """
    if param_id not in PARAM_DEFAULTS:
        # Defensive: prevent typos / future bugs from silently producing
        # nonsense recommendations against an unknown parameter.
        _LOGGER.warning(
            "compute_for_param called with unknown param_id=%r — "
            "no per-element advisor configured. Add to PARAM_DEFAULTS "
            "to enable.", param_id,
        )
        return None

    if not _opt(coordinator, opt_key(param_id, "enabled"), False):
        return None

    heads = list(_opt(coordinator, opt_key(param_id, "heads"), []) or [])
    spec_eff_value, spec_eff_source = _resolve_supplement_potency(
        coordinator, param_id,
    )

    cfg = _build_config(coordinator, param_id, spec_eff=spec_eff_value)

    snaps_raw = coordinator.advisor_snapshots(param_id)
    snaps: list[Snapshot] = []
    for s in snaps_raw:
        try:
            at = datetime.fromisoformat(s["at"])
        except (KeyError, ValueError, TypeError):
            continue
        # Storage uses `kh` as the value-field name regardless of param
        # — see coordinator.async_advisor_record_snapshot. The
        # Snapshot dataclass keeps `kh` for the same reason; the
        # algorithm is unit-agnostic.
        snaps.append(Snapshot(
            at=at,
            kh=s.get("kh") if isinstance(s.get("kh"), (int, float)) else None,
            dose_mL=(
                s.get("dose_mL")
                if isinstance(s.get("dose_mL"), (int, float)) else None
            ),
        ))

    acks = [
        Acknowledgment(
            at=datetime.fromisoformat(a["at"]),
            applied_value_mL=float(a["applied_value_mL"]),
            prev_value_mL=float(a.get("prev_value_mL", 0.0)),
        )
        for a in coordinator.advisor_acknowledgments(param_id)
        if "at" in a and "applied_value_mL" in a
    ]
    dismisses = [
        Dismissal(
            at=datetime.fromisoformat(d["at"]),
            suggested_value_mL=float(d["suggested_value_mL"]),
        )
        for d in coordinator.advisor_dismissals(param_id)
        if "at" in d and "suggested_value_mL" in d
    ]
    demands = [
        DemandChange(
            at=datetime.fromisoformat(d["at"]),
            reason=d.get("reason", ""),
            expected_direction=d.get("expected_direction", "unknown"),
            magnitude_hint_pct=d.get("magnitude_hint_pct"),
        )
        for d in coordinator.advisor_demand_changes(param_id)
        if "at" in d
    ]
    wcs = [
        WaterChange(
            at=datetime.fromisoformat(w["at"]),
            percent=float(w.get("percent", 0.0)),
            salt_mix_kh=w.get("salt_mix_kh"),
            notes=w.get("notes"),
        )
        for w in coordinator.advisor_water_changes(param_id)
        if "at" in w
    ]

    current_dose = _sum_dose_mL(hass, heads)

    rec = compute_recommendation(
        now=datetime.now(timezone.utc).astimezone(),
        snapshots=snaps,
        current_dose_mL=current_dose,
        cfg=cfg,
        acknowledgments=acks,
        dismissals=dismisses,
        demand_changes=demands,
        water_changes=wcs,
    )
    # Annotate the source of the spec efficiency value for diagnostic
    # surface (the dashboard's "Show your work" card reads this).
    rec.spec_efficiency_source = spec_eff_source
    rec.detected_supplement_label = None  # no auto-detect for non-KH yet
    rec.detected_supplement_profile = None
    return rec
