"""Alk advisor sensor + daily snapshotter.

Two things live here:

  - `AlkAdvisorSensor` — a SensorEntity that runs the advisor algorithm
    against persisted snapshots and exposes the recommendation as state
    plus show-your-work attributes. Recomputes on advisor-update signals
    (snapshot recorded, ack, dismiss, demand-change) — NOT on every
    upstream state change.

  - `AlkAdvisorSnapshotter` — registers a daily timer that captures one
    snapshot at the configured local time (default 23:55), reading the
    current KH from `kh_source` and the sum of programmed daily dose
    across the configured alk heads. Snapshots persist to the
    coordinator and are the only input the algorithm uses.

Snapshots are taken whenever alk heads are configured, regardless of
the `enabled` toggle — that way pre-existing history is ready when the
user finally enables the advisor (e.g. after a calibration drop test).
The sensor itself only outputs a recommendation when `enabled` is true.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_change

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

# Options-flow keys (advisor section).
OPT_ADVISOR_ENABLED = "advisor_enabled"
OPT_ALK_HEADS = "advisor_alk_head_entity_ids"     # multi-select; sensor.* IDs
OPT_KH_SOURCE = "advisor_kh_source"                # sensor.* (KH reading)
OPT_CALIBRATION_WARNING_ENTITY = "advisor_calibration_warning_entity"
OPT_TANK_VOLUME_L = "advisor_tank_volume_l"
OPT_SUPPLEMENT_PROFILE = "advisor_supplement_profile"  # see SUPPLEMENT_PROFILES
OPT_SUPPLEMENT_EFF = "advisor_spec_efficiency_dkh_per_mL_per_100L"
OPT_TARGET_MIN = "advisor_target_min"
OPT_TARGET_MAX = "advisor_target_max"
OPT_WINDOW_DAYS = "advisor_window_days"
OPT_MIN_SAMPLES = "advisor_min_samples"
OPT_MIN_TREND_DAYS = "advisor_min_trend_days"
OPT_COOLDOWN_DAYS = "advisor_cooldown_days"
OPT_DISMISS_COOLDOWN_DAYS = "advisor_dismiss_cooldown_days"
OPT_STEP_CAP_PCT = "advisor_step_cap_pct"
OPT_HYSTERESIS = "advisor_hysteresis_dkh"
OPT_MIN_SAMPLES_AFTER_EVENT = "advisor_min_samples_after_event"
OPT_CORRECTION_PERIOD_DAYS = "advisor_correction_period_days"
OPT_SNAPSHOT_HOUR = "advisor_snapshot_hour"        # 0-23, local time
OPT_SNAPSHOT_MINUTE = "advisor_snapshot_minute"    # 0-59
OPT_EMPIRICAL_DRIFT_PCT = "advisor_empirical_drift_pct"
OPT_WC_SETTLING_HOURS = "advisor_wc_settling_hours"

# Manufacturer-published efficiency values: dKH per mL per 100 L.
# Source = vendor labels / dosing instructions:
#   Red Sea Reef Foundation B: "1 mL per 100 L raises alkalinity by 0.1 dKH
#       (0.036 meq/L)" — Red Sea Foundation Care Pack instructions.
#   Tropic Marin All-For-Reef Alk: similar concentration, ~0.1 dKH/mL/100L.
# Add new BUILTIN profiles only with a verified vendor source. Users can
# add their own without a code release via `add_supplement_profile`
# (these merge on top at runtime — see `all_profiles`).
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto-detect from doser",
        "eff_dkh_per_mL_per_100L": None,   # resolved at runtime via state lookup
    },
    "red_sea_foundation_b": {
        "label": "Red Sea Reef Foundation B (Alkalinity)",
        "eff_dkh_per_mL_per_100L": 0.1,
    },
    "tropic_marin_alkalinity_plus": {
        "label": "Tropic Marin Alkalinity Plus",
        "eff_dkh_per_mL_per_100L": 0.1,
    },
    "custom": {
        "label": "Custom (use the manual efficiency value below)",
        "eff_dkh_per_mL_per_100L": None,   # falls back to OPT_SUPPLEMENT_EFF
    },
}

# Lower-cased substrings that label-match a supplement state to a profile.
# Matched against the state of `sensor.<doser>_head_<N>_supplement`.
BUILTIN_LABEL_PATTERNS: list[tuple[str, list[str]]] = [
    ("red_sea_foundation_b", [
        "foundation b", "reef foundation b", "kh/alkalinity", "kh / alkalinity",
    ]),
    ("tropic_marin_alkalinity_plus", [
        "alkalinity plus", "tropic marin alk",
    ]),
]


def all_profiles(coordinator: ReefDataCoordinator) -> dict[str, dict[str, Any]]:
    """Builtins ∪ user-added profiles whose target parameters include
    `kh`.

    Returns profiles that target the alk parameter — either solely
    (param_id="kh" or absent, for back-compat) or as one of several
    targets (multi-target supplements like NO3:PO4-X-style products
    with param_id=["nitrate", "phosphate"] would NOT include "kh"
    and so don't appear; an alk supplement that ALSO claimed
    param_id=["kh", "magnesium"] would appear).

    A profile that includes "kh" in its target list but doesn't have
    a real `eff_dkh_per_mL_per_100L` is excluded — without a potency
    the alk advisor can't compute dose changes, so showing it in the
    dropdown would just confuse the user.

    User entries are appended after builtins; the `user_`/slug-prefix
    scheme prevents collisions with builtin keys.
    """
    merged = dict(BUILTIN_PROFILES)
    for p in coordinator.supplement_profiles_for("kh"):
        if p.get("eff_dkh_per_mL_per_100L") is None:
            continue  # multi-target without a real KH potency — skip
        merged[p["id"]] = {
            "label": p["label"],
            "eff_dkh_per_mL_per_100L": p["eff_dkh_per_mL_per_100L"],
        }
    return merged


def all_label_patterns(
    coordinator: ReefDataCoordinator,
) -> list[tuple[str, list[str]]]:
    """Builtin patterns first (verified vendor labels win), then user
    patterns from KH-targeting profiles. Non-KH supplement patterns
    are excluded so auto-detect on the alk doser's `_supplement` state
    can't accidentally match a non-alk supplement label."""
    patterns = list(BUILTIN_LABEL_PATTERNS)
    for p in coordinator.supplement_profiles_for("kh"):
        if p.get("label_patterns"):
            patterns.append((p["id"], list(p["label_patterns"])))
    return patterns

DEFAULTS: dict[str, Any] = {
    OPT_ADVISOR_ENABLED: False,
    OPT_ALK_HEADS: [],
    OPT_KH_SOURCE: "",
    OPT_CALIBRATION_WARNING_ENTITY: "",
    OPT_TANK_VOLUME_L: 425.0,
    OPT_SUPPLEMENT_PROFILE: "auto",
    # Default if profile=custom and no other source — Red Sea Foundation B.
    OPT_SUPPLEMENT_EFF: 0.1,
    OPT_TARGET_MIN: 8.5,
    OPT_TARGET_MAX: 8.9,
    OPT_WINDOW_DAYS: 7,
    OPT_MIN_SAMPLES: 5,
    OPT_MIN_TREND_DAYS: 3,
    OPT_COOLDOWN_DAYS: 5.0,
    OPT_DISMISS_COOLDOWN_DAYS: 2.0,
    OPT_STEP_CAP_PCT: 10.0,
    OPT_HYSTERESIS: 0.1,
    OPT_MIN_SAMPLES_AFTER_EVENT: 3,
    OPT_CORRECTION_PERIOD_DAYS: 7.0,
    OPT_SNAPSHOT_HOUR: 23,
    OPT_SNAPSHOT_MINUTE: 55,
    OPT_EMPIRICAL_DRIFT_PCT: 50.0,
    OPT_WC_SETTLING_HOURS: 24.0,
}


def _opt(coordinator: ReefDataCoordinator, key: str) -> Any:
    """Read an advisor option, falling back to DEFAULTS[key]."""
    cfg = coordinator.get_advisor_config()
    if key in cfg and cfg[key] not in (None, ""):
        return cfg[key]
    return DEFAULTS.get(key)


def _supplement_entity_for_head(daily_dose_entity_id: str) -> str | None:
    """Derive the `_supplement` sensor from a `_daily_dose` entity_id.

    All RSDOSE4 head sensors follow `sensor.rsdose4_<unit>_head_<N>_<key>`
    so swapping `daily_dose` → `supplement` gives the entity that names
    the supplement (e.g. "KH/Alkalinity (Foundation B)").
    """
    if not daily_dose_entity_id.endswith("_daily_dose"):
        return None
    return daily_dose_entity_id[: -len("_daily_dose")] + "_supplement"


def _label_match_profile(
    label: str | None,
    patterns: list[tuple[str, list[str]]],
) -> str | None:
    """Match a doser supplement-state string to a profile id.

    `patterns` is the merged list (builtin + user) — caller-supplied so
    this function stays pure for unit testing.
    """
    if not label:
        return None
    needle = label.lower()
    for profile_id, pats in patterns:
        for p in pats:
            if p in needle:
                return profile_id
    return None


def detect_supplement_profile(
    hass: HomeAssistant,
    coordinator: ReefDataCoordinator,
    alk_head_entity_ids: list[str],
) -> tuple[str | None, str | None]:
    """Read the supplement label from the first alk head's `_supplement`
    sensor and match against known profiles (builtin + user).

    Returns (profile_id, raw_label). Both `None` if no head can be read.
    """
    patterns = all_label_patterns(coordinator)
    for daily_dose_eid in alk_head_entity_ids:
        supp_eid = _supplement_entity_for_head(daily_dose_eid)
        if not supp_eid:
            continue
        state: State | None = hass.states.get(supp_eid)
        if state is None or state.state in (
            "unknown", "unavailable", "none", "", None,
        ):
            continue
        return _label_match_profile(state.state, patterns), state.state
    return None, None


def _resolve_spec_efficiency(
    coordinator: ReefDataCoordinator,
    detected_profile: str | None = None,
) -> tuple[float, str]:
    """Pick the dKH/mL/100L value to use, returning (value, source_label).

    Resolution order:
      1. supplement_profile == "auto" → label-matched profile (builtin OR
         user-added). Falls through to (2) if no match.
      2. supplement_profile is a known preset (builtin OR user-added) →
         use that preset's value.
      3. supplement_profile == "custom" or unknown → use OPT_SUPPLEMENT_EFF.
    """
    profiles = all_profiles(coordinator)
    selected = str(_opt(coordinator, OPT_SUPPLEMENT_PROFILE) or "auto")
    if selected == "auto" and detected_profile:
        prof = profiles.get(detected_profile)
        if prof and prof["eff_dkh_per_mL_per_100L"] is not None:
            return (
                float(prof["eff_dkh_per_mL_per_100L"]),
                f"auto-detected: {prof['label']}",
            )
    if selected in profiles:
        prof = profiles[selected]
        if prof["eff_dkh_per_mL_per_100L"] is not None:
            return float(prof["eff_dkh_per_mL_per_100L"]), prof["label"]
    return float(_opt(coordinator, OPT_SUPPLEMENT_EFF)), "custom (manual entry)"


def _build_advisor_config(
    coordinator: ReefDataCoordinator,
    spec_eff: float | None = None,
) -> AdvisorConfig:
    """Build an AdvisorConfig. If `spec_eff` is provided it overrides the
    options-resolved value — used by `compute_for_entity` after running
    auto-detect. When called without `spec_eff`, uses OPT_SUPPLEMENT_EFF
    directly (e.g. from contexts with no hass state available).
    """
    if spec_eff is None:
        spec_eff = float(_opt(coordinator, OPT_SUPPLEMENT_EFF))
    return AdvisorConfig(
        target_min=float(_opt(coordinator, OPT_TARGET_MIN)),
        target_max=float(_opt(coordinator, OPT_TARGET_MAX)),
        window_days=int(_opt(coordinator, OPT_WINDOW_DAYS)),
        min_samples=int(_opt(coordinator, OPT_MIN_SAMPLES)),
        min_trend_days=int(_opt(coordinator, OPT_MIN_TREND_DAYS)),
        cooldown_days=float(_opt(coordinator, OPT_COOLDOWN_DAYS)),
        dismiss_cooldown_days=float(_opt(coordinator, OPT_DISMISS_COOLDOWN_DAYS)),
        step_cap_pct=float(_opt(coordinator, OPT_STEP_CAP_PCT)),
        hysteresis_dkh=float(_opt(coordinator, OPT_HYSTERESIS)),
        min_samples_after_event=int(_opt(coordinator, OPT_MIN_SAMPLES_AFTER_EVENT)),
        correction_period_days=float(_opt(coordinator, OPT_CORRECTION_PERIOD_DAYS)),
        empirical_drift_pct=float(_opt(coordinator, OPT_EMPIRICAL_DRIFT_PCT)),
        spec_efficiency_dkh_per_mL_per_100L=spec_eff,
        tank_volume_L=float(_opt(coordinator, OPT_TANK_VOLUME_L)),
        wc_settling_hours=float(_opt(coordinator, OPT_WC_SETTLING_HOURS)),
    )


def _read_float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state: State | None = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", "none", "", None):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _is_calibration_warning_on(hass: HomeAssistant, entity_id: str | None) -> bool:
    """Truthy when the configured calibration-warning binary sensor is on."""
    if not entity_id:
        return False
    state: State | None = hass.states.get(entity_id)
    if state is None:
        return False
    return state.state in ("on", "true", "1")


def _sum_dose_mL(hass: HomeAssistant, entity_ids: list[str]) -> float | None:
    """Sum the programmed daily dose across configured alk heads.

    `daily_dose` sensors report mL/day. We sum to support multi-head alk
    configurations. Returns None if no head returns a valid number — we
    don't want to fabricate a 0 mL/day total when sensors are temporarily
    unavailable.
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
# Daily snapshotter — captures one snapshot/day at the configured local time
# ---------------------------------------------------------------------------
class AlkAdvisorSnapshotter:
    """Schedule a daily snapshot capture.

    Lifecycle mirrors `AutoSourceListener` — one instance per ConfigEntry.
    Started from `__init__.py` after platforms are forwarded so the
    target entities exist.
    """

    PARAM_ID = "kh"

    def __init__(self, hass: HomeAssistant, coordinator: ReefDataCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._unsub: Any = None

    async def async_start(self) -> None:
        # Schedule the daily callback. Even with no alk heads configured
        # we still register — the callback no-ops cleanly until the user
        # finishes Options setup.
        hour = int(_opt(self._coordinator, OPT_SNAPSHOT_HOUR))
        minute = int(_opt(self._coordinator, OPT_SNAPSHOT_MINUTE))
        self._unsub = async_track_time_change(
            self._hass, self._capture, hour=hour, minute=minute, second=0,
        )
        _LOGGER.info(
            "Alk advisor snapshotter scheduled for %02d:%02d local",
            hour, minute,
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _capture(self, _now: datetime) -> None:
        """Read current KH + dose totals and persist as a snapshot."""
        alk_heads = list(_opt(self._coordinator, OPT_ALK_HEADS) or [])
        kh_source = _opt(self._coordinator, OPT_KH_SOURCE) or ""
        if not alk_heads or not kh_source:
            _LOGGER.debug("Snapshot skipped — alk heads or kh_source not configured")
            return

        kh = _read_float_state(self._hass, kh_source)
        dose_mL = _sum_dose_mL(self._hass, alk_heads)
        # We persist even when one side is None so the gap is visible in
        # storage; the algorithm tolerates None entries.
        await self._coordinator.async_record_advisor_snapshot(
            self.PARAM_ID,
            at=datetime.now(timezone.utc).astimezone().isoformat(),
            kh=kh, dose_mL=dose_mL,
        )
        _LOGGER.info(
            "Alk snapshot captured: kh=%s dose_mL=%s (heads=%d)",
            kh, dose_mL, len(alk_heads),
        )


# ---------------------------------------------------------------------------
# Algorithm runner — used by the sensor entity (which lives in sensor.py to
# keep HA-import surface there). Returns a Recommendation given the live
# coordinator + hass. None when the advisor is disabled in Options.
# ---------------------------------------------------------------------------
PARAM_ID = "kh"


def compute_for_entity(
    hass: HomeAssistant, coordinator: ReefDataCoordinator,
) -> Recommendation | None:
    """Build inputs from coordinator + hass state and run the algorithm."""
    if not _opt(coordinator, OPT_ADVISOR_ENABLED):
        return None

    alk_heads = list(_opt(coordinator, OPT_ALK_HEADS) or [])
    # Resolve which spec-efficiency value to use, based on the configured
    # supplement profile (auto-detect, preset, or custom). This is the
    # single point where we decide what supplement potency to assume.
    detected_profile, detected_label = detect_supplement_profile(
        hass, coordinator, alk_heads,
    )
    spec_eff_value, spec_eff_source = _resolve_spec_efficiency(
        coordinator, detected_profile=detected_profile,
    )
    cfg = _build_advisor_config(coordinator, spec_eff=spec_eff_value)

    snaps_raw = coordinator.advisor_snapshots(PARAM_ID)
    snaps: list[Snapshot] = []
    for s in snaps_raw:
        try:
            at = datetime.fromisoformat(s["at"])
        except (KeyError, ValueError, TypeError):
            continue
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
        for a in coordinator.advisor_acknowledgments(PARAM_ID)
        if "at" in a and "applied_value_mL" in a
    ]
    dismisses = [
        Dismissal(
            at=datetime.fromisoformat(d["at"]),
            suggested_value_mL=float(d["suggested_value_mL"]),
        )
        for d in coordinator.advisor_dismissals(PARAM_ID)
        if "at" in d and "suggested_value_mL" in d
    ]
    demands = [
        DemandChange(
            at=datetime.fromisoformat(d["at"]),
            reason=d.get("reason", ""),
            expected_direction=d.get("expected_direction", "unknown"),
            magnitude_hint_pct=d.get("magnitude_hint_pct"),
        )
        for d in coordinator.advisor_demand_changes(PARAM_ID)
        if "at" in d
    ]
    wcs = [
        WaterChange(
            at=datetime.fromisoformat(w["at"]),
            percent=float(w.get("percent", 0.0)),
            salt_mix_kh=w.get("salt_mix_kh"),
            notes=w.get("notes"),
        )
        for w in coordinator.advisor_water_changes(PARAM_ID)
        if "at" in w
    ]

    current_dose = _sum_dose_mL(hass, alk_heads)
    cal_warning = _is_calibration_warning_on(
        hass, _opt(coordinator, OPT_CALIBRATION_WARNING_ENTITY),
    )

    rec = compute_recommendation(
        now=datetime.now(timezone.utc).astimezone(),
        snapshots=snaps,
        current_dose_mL=current_dose,
        cfg=cfg,
        acknowledgments=acks,
        dismissals=dismisses,
        demand_changes=demands,
        water_changes=wcs,
        calibration_warning=cal_warning,
    )
    # Stash the supplement-resolution context on the recommendation so the
    # sensor can surface it. These are diagnostic-only and don't affect
    # the algorithm's math (cfg already contains the resolved spec_eff).
    rec.detected_supplement_label = detected_label
    rec.detected_supplement_profile = detected_profile
    rec.spec_efficiency_source = spec_eff_source
    return rec
