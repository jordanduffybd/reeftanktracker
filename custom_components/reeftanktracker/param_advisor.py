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
        # Defaults tuned for VERY SPARSE manual testing — typical
        # cadence is 1-2 Ca readings per month, max 4. Auto-tester
        # users (future Mastertronic Essential) can shrink the
        # window via Options for faster response cycles.
        #
        # 90-day rolling window: accumulates 3-6 readings at typical
        # cadence. min_samples=2 means the advisor activates as soon
        # as you have ONE comparison reading; min_trend_days=2 then
        # requires 2 consecutive out-of-band readings before
        # suggesting a change. So a single noisy reading can't trigger
        # action — we always need at least one confirmation.
        "window_days": 90,
        "min_samples": 2,
        "min_trend_days": 2,
        # 1 post-event reading is enough to exit learning mode at
        # this cadence (waiting 3 months would be absurd).
        "min_samples_after_event": 1,
        # 30-day cooldown matches typical monthly testing cadence —
        # one full testing cycle between recommendations.
        "cooldown_days": 30.0,
        "dismiss_cooldown_days": 14.0,
        # Spread corrections over a month → gentle daily dose changes
        # appropriate for the slow-feedback manual-test loop.
        "correction_period_days": 30.0,
        "empirical_drift_pct": 50.0,
        "wc_settling_hours": 24.0,
        # Foundation A: 1 mL per 100L raises Ca by 2 ppm.
        "default_eff_per_mL_per_100L": 2.0,
        # ppm — used in `Reason` strings.
        "value_unit": "ppm",
        # Substring-match patterns (case-insensitive) used to
        # auto-detect doser heads. The detector reads each RSDose
        # head's `_supplement` sensor state and prefills this
        # parameter's `heads` field with the matching head's
        # `_daily_dose` entity. User can override on the Options page.
        "head_label_patterns": [
            "foundation a", "calcium", "ca plus", "ca+", "ca-up",
        ],
    },
    "magnesium": {
        # Holmes-Farley: 1250–1350 ppm broadly. SPS-leaning prefer
        # 1300–1350; LPS tolerant down to 1200. NSW ~1280. Default
        # to the SPS-friendly band.
        "target_min": 1300.0,
        "target_max": 1350.0,
        # Mg test kits read in coarser units (typically ±20-30 ppm
        # noise). 20 ppm hysteresis catches real drifts without
        # chasing kit jitter on the wider scale.
        "hysteresis": 20.0,
        "step_cap_pct": 10.0,
        # Same sparse-cadence defaults as Calcium — Mg is also
        # tested manually (~1-2x/month). Mg is more forgiving than
        # Ca (50-100 ppm/day shifts barely register), so the same
        # cooldown/correction periods are appropriate.
        "window_days": 90,
        "min_samples": 2,
        "min_trend_days": 2,
        "min_samples_after_event": 1,
        "cooldown_days": 30.0,
        "dismiss_cooldown_days": 14.0,
        "correction_period_days": 30.0,
        "empirical_drift_pct": 50.0,
        "wc_settling_hours": 24.0,
        # Foundation C: 1 mL per 100L raises Mg by 1 ppm.
        "default_eff_per_mL_per_100L": 1.0,
        "value_unit": "ppm",
        "head_label_patterns": [
            "foundation c", "magnesium", "mg plus", "mg+", "mg-up",
        ],
    },
    "nitrate": {
        # Modern reef consensus (2018+): 1–10 ppm. The old "ULNS / 0
        # ppm" approach is abandoned — too-low NO3 starves zoox and
        # triggers dinoflagellates. SPS-leaning 1–5; mixed 2–10; LPS
        # tolerant 5–20.
        "target_min": 1.0,
        "target_max": 10.0,
        # Salifert Profi-Test NO3 reads in ~0.5-1 ppm increments.
        "hysteresis": 0.5,
        # Carbon-dose response is non-linear; conservative step cap.
        "step_cap_pct": 10.0,
        # Same sparse-cadence defaults as Ca/Mg.
        "window_days": 90,
        "min_samples": 2,
        "min_trend_days": 2,
        "min_samples_after_event": 1,
        "cooldown_days": 30.0,
        "dismiss_cooldown_days": 14.0,
        "correction_period_days": 30.0,
        "empirical_drift_pct": 50.0,
        "wc_settling_hours": 24.0,
        # NEGATIVE potency — NO3 supplements are REMOVERS. Raising
        # dose REDUCES NO3. The algorithm handles signed eff
        # naturally (delta / (signed_eff × days) → signed change_mL).
        # -0.5 is a rough NPX/HR-Nitrate average; bacterial response
        # varies wildly with population size. User can override via
        # Custom profile + manual spec_efficiency.
        "default_eff_per_mL_per_100L": -0.5,
        "value_unit": "ppm",
        # Floor below which the floor guard refuses to recommend
        # ANY further removal-dose increase (regardless of what the
        # algorithm computes). Stripping NO3 below 0.5 ppm is the
        # canonical dinoflagellate-outbreak setup per BRStv 52 Weeks.
        "floor_value": 0.5,
        # NPX is multi-target (NO3 + PO4) so the same head appears
        # in both NO3 and PO4 advisor prefills — fine, both should
        # be configured identically anyway.
        "head_label_patterns": [
            "npx", "no3:po4-x", "no3po4x", "no3 po4 x",
            "nitrate", "carbon dose",
        ],
    },
    "phosphate": {
        # 0.03–0.10 ppm consensus. Below 0.02 ppm → dinoflagellate
        # outbreak risk. SPS 0.03–0.06; mixed/LPS 0.05–0.10.
        "target_min": 0.03,
        "target_max": 0.10,
        # Hanna ULR PO4 reads in 0.001 ppm; 0.01 ppm hysteresis
        # catches drift without chasing kit jitter.
        "hysteresis": 0.01,
        "step_cap_pct": 10.0,
        # Same sparse-cadence defaults as Ca/Mg/NO3.
        "window_days": 90,
        "min_samples": 2,
        "min_trend_days": 2,
        "min_samples_after_event": 1,
        "cooldown_days": 30.0,
        "dismiss_cooldown_days": 14.0,
        "correction_period_days": 30.0,
        "empirical_drift_pct": 50.0,
        "wc_settling_hours": 24.0,
        # NEGATIVE potency — phosphate removers (lanthanum-based,
        # carbon dosing) reduce PO4. Lanthanum is roughly
        # stoichiometric: ~3.6 mg PO4 bound per mL of typical
        # solution → ~0.04 ppm PO4 reduction per mL per 100L.
        "default_eff_per_mL_per_100L": -0.04,
        "value_unit": "ppm",
        # Floor: refuse increase when PO4 ≤ 0.03. Stripping below
        # this is dinoflagellate territory.
        "floor_value": 0.03,
        "head_label_patterns": [
            "phosphate", "ar phosphate", "lanthanum",
            "npx", "no3:po4-x", "no3po4x", "no3 po4 x",
        ],
    },
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


def detect_default_heads(
    hass: HomeAssistant, param_id: str,
) -> list[str]:
    """Auto-detect doser heads for `param_id` by inspecting per-head
    `_supplement` sensors and matching their state against this
    parameter's `head_label_patterns`.

    Reefbeat exposes one entity per RSDose head:
      `sensor.rsdose4_<serial>_head_<N>_supplement`
    whose state is the supplement label set in ReefBeat (e.g.
    "Foundation A" or "NPX"). The corresponding daily-dose entity
    sits at the same prefix with `_supplement` swapped for
    `_daily_dose`. We discover heads by reading every
    `*_supplement` sensor's state, substring-matching against this
    param's patterns, and emitting the matching `_daily_dose`
    entity_ids.

    Returns an empty list when no patterns are configured for the
    param or no head matches. Caller (config_flow) uses this to
    pre-fill the `heads` field on the per-element advisor Options
    page — user can override.

    Generic across doser brands: any sensor with the
    `_supplement` / `_daily_dose` naming pair is matched. Tested
    against RSDose 4 in prod; should also work with future
    Reefbeat dosers and any community-contributed integration that
    follows the same convention.
    """
    patterns = PARAM_DEFAULTS.get(param_id, {}).get(
        "head_label_patterns", [],
    )
    if not patterns:
        return []
    patterns_lower = [p.lower() for p in patterns]

    matches: list[str] = []
    seen: set[str] = set()
    for state in hass.states.async_all("sensor"):
        eid = state.entity_id
        if not eid.endswith("_supplement"):
            continue
        label = (state.state or "").lower()
        if not label or label in ("unknown", "unavailable", "none"):
            continue
        if not any(p in label for p in patterns_lower):
            continue
        dose_eid = eid[: -len("_supplement")] + "_daily_dose"
        if dose_eid in seen:
            continue
        # Verify the daily-dose entity actually exists — guards
        # against a stale supplement sensor whose head was removed.
        if hass.states.get(dose_eid) is not None:
            matches.append(dose_eid)
            seen.add(dose_eid)
    return matches


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

    # Cross-parameter safety guards (0.5.2) — applied AFTER the
    # algorithm produces a recommendation. Currently only the
    # snowstorm guard for Calcium up-recommendations.
    rec = _apply_safety_guards(coordinator, param_id, rec)
    return rec


def _latest_snapshot_value(
    coordinator: ReefDataCoordinator, param_id: str,
) -> float | None:
    """Most-recent snapshot value for `param_id`, or None if no
    snapshots exist yet. Used by cross-parameter safety guards
    (e.g. Ca's snowstorm check needs the latest alk + Mg readings).
    """
    snaps = coordinator.advisor_snapshots(param_id)
    if not snaps:
        return None
    # Snapshots are appended chronologically; last is most recent
    last = snaps[-1]
    v = last.get("kh")
    return float(v) if isinstance(v, (int, float)) else None


def _apply_safety_guards(
    coordinator: ReefDataCoordinator,
    param_id: str,
    rec: Recommendation,
) -> Recommendation:
    """Cross-parameter safety guards. Applied AFTER the standard
    compute path so the algorithm output reflects the user's
    explicit safety settings.

    Snowstorm guard (Ca): refuse to recommend RAISING Calcium dose
    when alkalinity is high (>10 dKH) OR magnesium is low (<1200
    ppm). Both conditions create supersaturation risk: high alk +
    high Ca → CaCO3 precipitates as cloudy "snowstorm"; low Mg
    fails to inhibit that precipitation. Holmes-Farley + BRStv
    consensus is "fix Mg first, then alk + Ca settle."

    The guard ONLY suppresses dose INCREASES — it never blocks a
    decrease (lowering Ca dose can't trigger snowstorm). Holding
    or decreasing recommendations pass through unchanged.

    See `~/.claude/projects/.../memory/reference_reef_dosing_research.md`
    for the full reasoning.
    """
    if param_id == "calcium":
        return _apply_snowstorm_guard(coordinator, rec)
    if param_id in ("nitrate", "phosphate"):
        rec = _apply_floor_guard(coordinator, param_id, rec)
        rec = _apply_redfield_warning(coordinator, param_id, rec)
        return rec
    return rec


def _apply_snowstorm_guard(
    coordinator: ReefDataCoordinator,
    rec: Recommendation,
) -> Recommendation:
    """Snowstorm guard for calcium. Refuses to recommend RAISING
    Calcium dose when alk>10 OR Mg<1200. See module docstring."""
    if (
        rec.current_dose_mL is None
        or rec.suggested_dose_mL is None
        or rec.suggested_dose_mL <= rec.current_dose_mL
    ):
        return rec

    latest_kh = _latest_snapshot_value(coordinator, "kh")
    latest_mg = _latest_snapshot_value(coordinator, "magnesium")

    triggered: list[str] = []
    if latest_kh is not None and latest_kh > 10.0:
        triggered.append(
            f"alkalinity {latest_kh:.2f} dKH > 10 (precipitation risk)"
        )
    if latest_mg is not None and latest_mg < 1200.0:
        triggered.append(
            f"magnesium {latest_mg:.0f} ppm < 1200 (low-Mg fails to "
            f"inhibit Ca/alk precipitation)"
        )

    if not triggered:
        return rec

    rec.suggested_dose_mL = rec.current_dose_mL
    rec.state = rec.current_dose_mL
    rec.change_mL = 0.0
    rec.change_pct = 0.0
    rec.confidence = "low"
    rec.reason = (
        f"⚠ Snowstorm guard suppressed Ca up-recommendation: "
        f"{'; '.join(triggered)}. Address alk/Mg first — Holmes-Farley "
        f"consensus is fix Mg, then alk + Ca settle. Original advisor "
        f"reasoning: {rec.reason}"
    )
    return rec


def _apply_floor_guard(
    coordinator: ReefDataCoordinator,
    param_id: str,
    rec: Recommendation,
) -> Recommendation:
    """Floor guard for nutrient REMOVERS (NO3, PO4).

    Refuses to recommend INCREASING removal dose when the parameter is
    already at/below the floor value. Stripping NO3 below ~0.5 ppm or
    PO4 below ~0.03 ppm is the canonical dinoflagellate-outbreak
    setup (BRStv 52 Weeks of Reefing, modern reef consensus).

    For remover supplements, "increase dose" = "remove more" =
    "lower the value further" — exactly what we want to PREVENT
    when the value is already at floor.

    Defensive: if no median is available (advisor still warming up,
    no snapshots), pass through unchanged. The user sees a regular
    "insufficient samples" recommendation in that case, no false
    floor-guard alarm.
    """
    floor = PARAM_DEFAULTS.get(param_id, {}).get("floor_value")
    if floor is None:
        return rec
    if (
        rec.current_dose_mL is None
        or rec.suggested_dose_mL is None
        or rec.suggested_dose_mL <= rec.current_dose_mL
    ):
        return rec  # not an increase — pass through
    median = rec.kh_median
    if median is None:
        return rec  # no data — defensive pass-through
    if median > floor:
        return rec  # safely above floor

    rec.suggested_dose_mL = rec.current_dose_mL
    rec.state = rec.current_dose_mL
    rec.change_mL = 0.0
    rec.change_pct = 0.0
    rec.confidence = "low"
    label = param_id.capitalize()
    unit = PARAM_DEFAULTS[param_id].get("value_unit", "ppm")
    rec.reason = (
        f"⚠ Floor guard suppressed {label} removal-dose increase: "
        f"{label} median {median:.3f} {unit} ≤ floor {floor:.2f} {unit}. "
        f"Stripping below this risks dinoflagellate outbreak — let the "
        f"value recover before increasing removal. Original advisor "
        f"reasoning: {rec.reason}"
    )
    return rec


# Redfield-ratio thresholds (NO3:PO4 by mass). NSW ratio is ~100:1
# by mass (16:1 molar). Reef tanks tolerate a wide band, but extremes
# trigger nuisance algae:
# - Below 50:1 (PO4 high relative to NO3) → cyanobacteria
# - Above 200:1 (NO3 high relative to PO4) → dinoflagellates
# Source: BRStv 52 Weeks Ep. 17, Triton lab guidance.
REDFIELD_LOW = 50.0
REDFIELD_HIGH = 200.0


def _apply_redfield_warning(
    coordinator: ReefDataCoordinator,
    param_id: str,
    rec: Recommendation,
) -> Recommendation:
    """Soft warning for NO3/PO4 imbalance.

    Reads the latest NO3 and PO4 medians and computes the mass ratio.
    Outside [50:1, 200:1] sets `redfield_warning=True` and prepends
    a warning to the reason text. NOT a hard block — the algorithm
    still produces a recommendation. The user sees the imbalance flag
    and can decide whether to address it.

    Surfaces on both NO3 and PO4 advisors (each can fire the warning
    independently when its own snapshots update).
    """
    latest_no3 = _latest_snapshot_value(coordinator, "nitrate")
    latest_po4 = _latest_snapshot_value(coordinator, "phosphate")
    if latest_no3 is None or latest_po4 is None or latest_po4 <= 0.0:
        return rec
    ratio = latest_no3 / latest_po4
    in_band = REDFIELD_LOW <= ratio <= REDFIELD_HIGH
    # Always set the attribute so the dashboard can show "ratio: X
    # (in/out of band)" — this is diagnostic data even when in band.
    rec.redfield_ratio = ratio
    rec.redfield_warning = not in_band
    if in_band:
        return rec
    if ratio < REDFIELD_LOW:
        warn = (
            f"⚠ Redfield imbalance: NO3:PO4 = {ratio:.0f}:1 < "
            f"{REDFIELD_LOW:.0f}:1 (PO4 too high relative to NO3 — "
            f"cyanobacteria risk)."
        )
    else:
        warn = (
            f"⚠ Redfield imbalance: NO3:PO4 = {ratio:.0f}:1 > "
            f"{REDFIELD_HIGH:.0f}:1 (NO3 too high relative to PO4 — "
            f"dinoflagellate risk)."
        )
    rec.reason = f"{warn} {rec.reason}"
    return rec
