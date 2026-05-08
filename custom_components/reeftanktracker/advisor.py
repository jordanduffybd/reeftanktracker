"""Alkalinity dosing advisor — pure algorithm.

No Home Assistant imports. Takes a list of daily snapshots (one per day:
KH median + total alk dose mL) plus user state (target band, ack/dismiss
history, demand-change events, tunables) and returns a Recommendation
describing what the user should change in their Reefbeat dose.

The recommendation is advisory only — v1 never writes back to the doser.
The user manually applies the suggested daily_dose, then calls the
acknowledge service which seeds the cooldown.

Stability rules (Jordan-stated, hard requirements):
  1. Never react to a single reading — uses median over `window_days`.
  2. Trends only — median must drift outside the target band for at
     least `min_trend_days` of the window before any change is suggested.
  3. Cooldown — no new suggestion within `cooldown_days` of an ack/dismiss.
  4. Step cap — change capped at ±`step_cap_pct` of the current dose.
  5. Confidence — calibration-overdue caveat downgrades to "low" and
     surfaces a warning. (No "observed-efficiency vs spec" gate in v1 —
     see Math model below.)

Math model (v1 — pragmatic, transparent):

  We model alkalinity as a steady-state balance between dosing and
  consumption. We do NOT attempt to infer the supplement's true potency
  from a single window of data — slope/dose is not potency, it's
  (dose × potency − consumption), which doesn't decompose without a
  controlled experiment. So:

  - Use the manufacturer spec (e.g. Foundation B 0.36 dKH/mL/100 L) as
    the assumed potency, rescaled to the configured tank volume.
  - Compute the deficit: delta_KH = target_midpoint − kh_median.
  - One-time correction in mL: delta_KH / spec_potency.
  - Spread that over `correction_period_days` (default 7) — small
    daily-dose change, applied steadily, until the next evaluation.
  - Cap at ±`step_cap_pct` of the current dose (rule 4).

  Observed slope/dose is computed as a DIAGNOSTIC attribute only — useful
  for the user to see in show-your-work view, but not used in the math.

Demand-change events truncate the rolling window: pre-event data is
discarded, and a learning-mode period of `min_samples_after_event` days
must pass before the algorithm will issue a new suggestion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------
@dataclass
class Snapshot:
    """One end-of-day snapshot of tank state.

    `kh` is the daily KH median (or None on a missed titration day).
    `dose_mL` is the total mL of alk supplement dosed across all selected
    heads on that day (auto + manual). Captured at ~23:55 local before
    the doser's midnight reset.
    """
    at: datetime         # end-of-day timestamp (tz-aware)
    kh: float | None
    dose_mL: float | None


@dataclass
class DemandChange:
    at: datetime
    reason: str
    expected_direction: str = "unknown"   # increase | decrease | unknown
    magnitude_hint_pct: float | None = None


@dataclass
class Acknowledgment:
    at: datetime
    applied_value_mL: float
    prev_value_mL: float


@dataclass
class Dismissal:
    at: datetime
    suggested_value_mL: float


@dataclass
class WaterChange:
    at: datetime
    percent: float
    salt_mix_kh: float | None = None
    notes: str | None = None


@dataclass
class AdvisorConfig:
    target_min: float = 8.5
    target_max: float = 8.9

    window_days: int = 7
    min_samples: int = 5
    min_trend_days: int = 3
    cooldown_days: float = 5.0
    dismiss_cooldown_days: float = 2.0
    step_cap_pct: float = 10.0
    hysteresis_dkh: float = 0.1
    min_samples_after_event: int = 3

    # How long the user is expected to apply the change before the next
    # evaluation. Larger value → smaller daily-dose change (gentler).
    correction_period_days: float = 7.0

    # Empirical-potency drift threshold (% deviation from spec). When
    # observed potency differs from spec by more than this, the
    # recommendation surfaces a `spec_drift_warning`.
    empirical_drift_pct: float = 50.0
    # Smallest dose change (mL/day, absolute) the empirical estimator
    # will use as a denominator. Below this, the estimate is skipped to
    # avoid amplifying noise on tiny dose adjustments.
    empirical_min_dose_change_mL: float = 0.1

    # Hours after a logged water change during which snapshots are
    # excluded from the slope/median calculation. The window itself is
    # NOT truncated — pre-WC and post-WC steady-state are both kept.
    wc_settling_hours: float = 24.0

    # Manufacturer prior for the alkalinity supplement.
    # Default: Red Sea Reef Foundation B — vendor-published 0.1 dKH per mL
    # per 100 L (= 0.036 meq/L). The Options-flow `supplement_profile`
    # selector lets the user pick a different preset or auto-detect from
    # the doser's `_supplement` state.
    spec_efficiency_dkh_per_mL_per_100L: float = 0.1

    # Tank volume in litres (used to rescale spec efficiency to this tank).
    tank_volume_L: float = 425.0

    # Display vocabulary for reason text + log messages. The algorithm
    # is unit-agnostic; these labels just shape the user-facing strings.
    # Defaults match the original alk advisor behaviour for back-compat
    # — alk_advisor.py keeps using AdvisorConfig() without setting them.
    # Per-element advisors (param_advisor.py) override these from
    # `PARAM_DEFAULTS`. e.g. for calcium: param_label="Calcium",
    # value_unit="ppm".
    param_label: str = "KH"
    value_unit: str = "dKH"


@dataclass
class Recommendation:
    """Algorithm output. `state` is the headline number for the sensor.

    Possible shapes:
      - confident change:    state=<suggested_mL>, change_mL nonzero
      - within band / hold:  state=<current_mL>, change_mL=0
      - cooldown / learning: state=<current_mL>, change_mL=0, reason explains
      - insufficient data:   state=None,         confidence="insufficient"
      - calibration warning: confidence="low", warning attached, suggestion still given
    """
    state: float | None                          # mL/day (None = unavailable)
    current_dose_mL: float | None
    suggested_dose_mL: float | None
    change_mL: float
    change_pct: float
    confidence: str                              # high | medium | low | insufficient
    reason: str
    kh_median: float | None
    delta_dkh: float | None
    target_min: float
    target_max: float
    target_midpoint: float
    observed_slope_dkh_per_day: float | None     # diagnostic only
    observed_dose_median_mL: float | None        # diagnostic only
    spec_efficiency_dkh_per_mL: float
    samples_used: int
    window_start: datetime | None
    window_end: datetime
    cooldown_until: datetime | None
    last_acknowledged_at: datetime | None
    last_acknowledged_value_mL: float | None
    last_demand_change_at: datetime | None
    last_demand_change_reason: str | None
    days_since_demand_change: float | None
    calibration_warning: bool
    # Diagnostic fields populated by `alk_advisor.compute_for_entity`
    # (left as None when the algorithm is invoked directly by tests).
    detected_supplement_label: str | None = None
    detected_supplement_profile: str | None = None
    spec_efficiency_source: str | None = None
    # Observed-vs-spec efficiency diagnostics — populated by
    # compute_recommendation when the most recent ack pair has enough
    # pre/post data and a meaningful dose change. Math still uses
    # spec for the suggestion; these are visibility-only.
    empirical_potency_dkh_per_mL: float | None = None
    empirical_to_spec_ratio: float | None = None
    empirical_potency_basis: str | None = None
    spec_drift_warning: bool = False
    # Water-change diagnostics
    last_water_change_at: datetime | None = None
    last_water_change_percent: float | None = None
    days_since_water_change: float | None = None
    samples_excluded_for_wc: int = 0

    def as_attributes(self) -> dict[str, Any]:
        """Serialize for Home Assistant `extra_state_attributes`."""
        def _iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if isinstance(dt, datetime) else None

        return {
            "current_dose_mL": self.current_dose_mL,
            "suggested_dose_mL": self.suggested_dose_mL,
            "change_mL": self.change_mL,
            "change_pct": self.change_pct,
            "confidence": self.confidence,
            "reason": self.reason,
            "kh_median": self.kh_median,
            "delta_dkh": self.delta_dkh,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "target_midpoint": self.target_midpoint,
            "observed_slope_dkh_per_day": self.observed_slope_dkh_per_day,
            "observed_dose_median_mL": self.observed_dose_median_mL,
            "spec_efficiency_dkh_per_mL": self.spec_efficiency_dkh_per_mL,
            "samples_used": self.samples_used,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "cooldown_until": _iso(self.cooldown_until),
            "last_acknowledged_at": _iso(self.last_acknowledged_at),
            "last_acknowledged_value_mL": self.last_acknowledged_value_mL,
            "last_demand_change_at": _iso(self.last_demand_change_at),
            "last_demand_change_reason": self.last_demand_change_reason,
            "days_since_demand_change": self.days_since_demand_change,
            "calibration_warning": self.calibration_warning,
            "detected_supplement_label": self.detected_supplement_label,
            "detected_supplement_profile": self.detected_supplement_profile,
            "spec_efficiency_source": self.spec_efficiency_source,
            "empirical_potency_dkh_per_mL": self.empirical_potency_dkh_per_mL,
            "empirical_to_spec_ratio": self.empirical_to_spec_ratio,
            "empirical_potency_basis": self.empirical_potency_basis,
            "spec_drift_warning": self.spec_drift_warning,
            "last_water_change_at": _iso(self.last_water_change_at),
            "last_water_change_percent": self.last_water_change_percent,
            "days_since_water_change": self.days_since_water_change,
            "samples_excluded_for_wc": self.samples_excluded_for_wc,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _spec_eff_for_tank(cfg: AdvisorConfig) -> float:
    """Manufacturer spec rescaled to this tank's volume.

    Spec is given as dKH per mL per 100 L. For a 425 L tank, the same
    1 mL only moves KH by spec * (100 / 425).
    """
    return cfg.spec_efficiency_dkh_per_mL_per_100L * (100.0 / cfg.tank_volume_L)


def _slope_per_day(snaps: list[Snapshot]) -> float | None:
    """Linear slope of KH vs day index, in dKH per day.

    Returns None if fewer than 2 KH-bearing samples. Drops snapshots
    where KH is None.
    """
    points = [(i, s.kh) for i, s in enumerate(snaps) if s.kh is not None]
    if len(points) < 2:
        return None
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def _trend_days_outside_band(
    snaps: list[Snapshot], target_min: float, target_max: float
) -> int:
    """Count consecutive most-recent days where KH median is outside the band.

    Used by the trend rule: only act if at least `min_trend_days` of the
    window's tail are persistently outside the target band. A single
    in-band day at the tail resets the count to zero.
    """
    count = 0
    for s in reversed(snaps):
        if s.kh is None:
            continue
        if s.kh < target_min or s.kh > target_max:
            count += 1
        else:
            break
    return count


def _within(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def _is_within_wc_settling(
    snap_at: datetime,
    water_changes: list[WaterChange],
    settling_hours: float,
) -> bool:
    """True if any water-change event occurred ≤ settling_hours before
    the snapshot timestamp."""
    if settling_hours <= 0:
        return False
    cutoff = settling_hours * 3600.0
    for wc in water_changes:
        delta = (snap_at - wc.at).total_seconds()
        if 0 <= delta <= cutoff:
            return True
    return False


def _slope_in_window(
    snaps: list[Snapshot], start: datetime, end: datetime,
) -> tuple[float | None, int]:
    """Slope (dKH/day) over a time-bounded subset of snapshots, plus
    the count of KH-bearing samples used. Returns (None, count) when
    fewer than 2 KH-bearing samples land in the window."""
    subset = [s for s in snaps if start <= s.at <= end and s.kh is not None]
    if len(subset) < 2:
        return None, len(subset)
    return _slope_per_day(subset), len(subset)


def empirical_potency_from_acks(
    snapshots: list[Snapshot],
    acks: list[Acknowledgment],
    now: datetime,
    window_days: int,
    min_samples: int,
    min_dose_change_mL: float,
) -> tuple[float | None, str]:
    """Estimate dKH per mL from the most recent acknowledged dose change.

    Across a clean dose change, consumption cancels:
        slope_after − slope_before = (dose_after − dose_before) × potency
    Returns (potency, basis). `potency` is None when:
      - there are no acks
      - the dose change was too small (< min_dose_change_mL)
      - either side of the ack lacks `min_samples` KH-bearing snapshots
      - either slope can't be computed
    The basis string explains which case applied (suitable for surfacing
    as the `empirical_potency_basis` attribute).
    """
    if not acks:
        return None, "no acknowledgment recorded yet"
    last = max(acks, key=lambda a: a.at)

    dose_change = last.applied_value_mL - last.prev_value_mL
    if abs(dose_change) < min_dose_change_mL:
        return None, (
            f"dose change {dose_change:+.3f} mL/day below "
            f"empirical threshold {min_dose_change_mL} mL"
        )

    pre_start = last.at - timedelta(days=window_days)
    pre_end = last.at
    post_start = last.at
    post_end = min(now, last.at + timedelta(days=window_days))

    slope_before, n_before = _slope_in_window(snapshots, pre_start, pre_end)
    slope_after, n_after = _slope_in_window(snapshots, post_start, post_end)

    if n_before < min_samples or n_after < min_samples:
        return None, (
            f"insufficient data around ack at {last.at.isoformat()} "
            f"(pre={n_before}, post={n_after}, need {min_samples} each)"
        )
    if slope_before is None or slope_after is None:
        return None, (
            f"slope undefined around ack at {last.at.isoformat()}"
        )

    potency = (slope_after - slope_before) / dose_change
    if potency <= 0:
        return None, (
            f"computed potency {potency:.4f} dKH/mL is non-positive — "
            f"dose change in opposite direction to slope change "
            f"(pre slope {slope_before:+.3f}, post slope {slope_after:+.3f}, "
            f"dose change {dose_change:+.2f} mL)"
        )

    return potency, (
        f"derived from ack at {last.at.isoformat()}: "
        f"pre slope {slope_before:+.3f}, post slope {slope_after:+.3f}, "
        f"dose change {dose_change:+.2f} mL → {potency:.4f} dKH/mL "
        f"(pre samples={n_before}, post samples={n_after})"
    )


# ---------------------------------------------------------------------------
# Main algorithm
# ---------------------------------------------------------------------------
def compute_recommendation(
    *,
    now: datetime,
    snapshots: Iterable[Snapshot],
    current_dose_mL: float | None,
    cfg: AdvisorConfig,
    acknowledgments: Iterable[Acknowledgment] = (),
    dismissals: Iterable[Dismissal] = (),
    demand_changes: Iterable[DemandChange] = (),
    water_changes: Iterable[WaterChange] = (),
    calibration_warning: bool = False,
) -> Recommendation:
    """Run the advisor algorithm.

    All inputs are values, not HA state — this is unit-testable by giving
    it a list of synthetic snapshots. The caller (the sensor entity) is
    responsible for assembling these from Home Assistant state.
    """
    midpoint = (cfg.target_min + cfg.target_max) / 2.0
    spec_eff = _spec_eff_for_tank(cfg)

    # ------------------------------------------------------------------
    # Window & demand-change handling
    # ------------------------------------------------------------------
    window_start = now - timedelta(days=cfg.window_days)
    last_demand: DemandChange | None = None
    demand_changes = list(demand_changes)
    if demand_changes:
        last_demand = max(demand_changes, key=lambda d: d.at)
        if last_demand.at > window_start:
            window_start = last_demand.at

    days_since_event = (
        (now - last_demand.at).total_seconds() / 86400.0
        if last_demand is not None else None
    )

    # Materialize snapshots once and apply both the window filter and the
    # water-change settling filter. Snapshots within the settling window
    # of any logged water change are excluded from the slope/median
    # calculation, but the rolling window is NOT truncated (unlike demand
    # changes — see plan §"Why this is correct vs treating it like a demand
    # change").
    wc_list = list(water_changes)
    snaps_in_window = [s for s in snapshots if window_start <= s.at <= now]
    samples_excluded_for_wc = sum(
        1 for s in snaps_in_window
        if _is_within_wc_settling(s.at, wc_list, cfg.wc_settling_hours)
    )
    snaps = sorted(
        (s for s in snaps_in_window
         if not _is_within_wc_settling(s.at, wc_list, cfg.wc_settling_hours)),
        key=lambda s: s.at,
    )
    kh_values = [s.kh for s in snaps if s.kh is not None]
    samples_used = len(kh_values)
    kh_median: float | None = median(kh_values) if kh_values else None

    # Latest water change for diagnostics
    last_wc = max(wc_list, key=lambda w: w.at) if wc_list else None
    days_since_wc = (
        (now - last_wc.at).total_seconds() / 86400.0
        if last_wc is not None else None
    )

    # Diagnostic observations (not used in math)
    obs_slope = _slope_per_day(snaps)
    dose_values = [s.dose_mL for s in snaps if s.dose_mL is not None]
    obs_dose_median = median(dose_values) if dose_values else None

    # Materialize acknowledgments once (caller may pass a generator).
    acks_list = list(acknowledgments)

    # Observed-vs-spec efficiency (diagnostic). Uses the most recent ack
    # event as a clean before/after boundary — only valid place to back
    # out potency from a single tank's data.
    snaps_full = sorted(snapshots, key=lambda s: s.at)
    empirical_potency, empirical_basis = empirical_potency_from_acks(
        snaps_full,
        acks_list,
        now=now,
        window_days=cfg.window_days,
        min_samples=cfg.min_samples,
        min_dose_change_mL=cfg.empirical_min_dose_change_mL,
    )

    # Latest ack / dismiss for cooldown tracking
    acks = sorted(acks_list, key=lambda a: a.at)
    last_ack = acks[-1] if acks else None
    dismisses = sorted(dismissals, key=lambda d: d.at)
    last_dismiss = dismisses[-1] if dismisses else None

    cooldown_until: datetime | None = None
    if last_ack is not None:
        cooldown_until = last_ack.at + timedelta(days=cfg.cooldown_days)
    if last_dismiss is not None:
        d_until = last_dismiss.at + timedelta(days=cfg.dismiss_cooldown_days)
        if cooldown_until is None or d_until > cooldown_until:
            cooldown_until = d_until

    in_cooldown = cooldown_until is not None and now < cooldown_until

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------
    # Empirical-vs-spec ratio + drift flag (computed once, passed to all
    # _build calls so every exit path surfaces the diagnostic).
    empirical_to_spec_ratio: float | None = None
    spec_drift_warning = False
    if empirical_potency is not None and spec_eff > 0:
        empirical_to_spec_ratio = empirical_potency / spec_eff
        band = cfg.empirical_drift_pct / 100.0
        if not _within(empirical_to_spec_ratio, 1 - band, 1 + band):
            spec_drift_warning = True

    def _build(
        *, state: float | None, suggested: float | None,
        confidence: str, reason: str,
    ) -> Recommendation:
        change_mL = 0.0
        change_pct = 0.0
        if (suggested is not None
                and current_dose_mL is not None
                and current_dose_mL > 0):
            change_mL = suggested - current_dose_mL
            change_pct = (change_mL / current_dose_mL) * 100.0
        # Append drift warning to the reason text if applicable
        full_reason = reason
        if spec_drift_warning and empirical_to_spec_ratio is not None:
            full_reason += (
                f" Note: observed potency is {empirical_to_spec_ratio:.2f}× "
                f"spec — exceeds the ±{cfg.empirical_drift_pct:.0f}% drift "
                f"band. Consider switching to a Custom profile with the "
                f"observed value if this persists."
            )
        if samples_excluded_for_wc > 0:
            full_reason += (
                f" Excluded {samples_excluded_for_wc} snapshot(s) within "
                f"{cfg.wc_settling_hours:.0f}h of recent water change."
            )
        return Recommendation(
            state=state,
            current_dose_mL=current_dose_mL,
            suggested_dose_mL=suggested,
            change_mL=round(change_mL, 3),
            change_pct=round(change_pct, 2),
            confidence=confidence,
            reason=full_reason,
            kh_median=round(kh_median, 3) if kh_median is not None else None,
            delta_dkh=(
                round(midpoint - kh_median, 3) if kh_median is not None else None
            ),
            target_min=cfg.target_min,
            target_max=cfg.target_max,
            target_midpoint=midpoint,
            observed_slope_dkh_per_day=(
                round(obs_slope, 4) if obs_slope is not None else None
            ),
            observed_dose_median_mL=obs_dose_median,
            spec_efficiency_dkh_per_mL=round(spec_eff, 6),
            samples_used=samples_used,
            window_start=window_start,
            window_end=now,
            cooldown_until=cooldown_until,
            last_acknowledged_at=last_ack.at if last_ack else None,
            last_acknowledged_value_mL=(
                last_ack.applied_value_mL if last_ack else None
            ),
            last_demand_change_at=last_demand.at if last_demand else None,
            last_demand_change_reason=last_demand.reason if last_demand else None,
            days_since_demand_change=(
                round(days_since_event, 2)
                if days_since_event is not None else None
            ),
            calibration_warning=calibration_warning,
            empirical_potency_dkh_per_mL=(
                round(empirical_potency, 6) if empirical_potency is not None else None
            ),
            empirical_to_spec_ratio=(
                round(empirical_to_spec_ratio, 4)
                if empirical_to_spec_ratio is not None else None
            ),
            empirical_potency_basis=empirical_basis,
            spec_drift_warning=spec_drift_warning,
            last_water_change_at=last_wc.at if last_wc else None,
            last_water_change_percent=(
                last_wc.percent if last_wc else None
            ),
            days_since_water_change=(
                round(days_since_wc, 2) if days_since_wc is not None else None
            ),
            samples_excluded_for_wc=samples_excluded_for_wc,
        )

    # ------------------------------------------------------------------
    # Exit paths
    # ------------------------------------------------------------------
    # 0) Doser unreachable — short-circuit BEFORE any other branch so we
    # always show the same actionable message regardless of where in the
    # algorithm we'd otherwise land. Without this, a transient ReefBeat
    # outage can make the advisor sensor go to "unknown" with a stale
    # "learning mode" reason that doesn't tell the user the real cause.
    if current_dose_mL is None or current_dose_mL <= 0:
        return _build(
            state=None, suggested=None,
            confidence="insufficient",
            reason=(
                "Doser daily-dose sensor is unreachable or zero "
                "(check ReefBeat / doser connectivity). Advisor will resume "
                "automatically when the sensor returns."
            ),
        )

    # 1) Demand-change learning mode (checked before insufficient-data
    # so a recent stocking change shows the right reason — the window
    # truncation will normally leave too few samples anyway).
    if (last_demand is not None
            and days_since_event is not None
            and days_since_event < cfg.min_samples_after_event):
        return _build(
            state=current_dose_mL, suggested=current_dose_mL,
            confidence="low",
            reason=(
                f"Learning mode after demand change "
                f"\"{last_demand.reason}\" "
                f"({days_since_event:.1f} of {cfg.min_samples_after_event} "
                f"days elapsed)."
            ),
        )

    # 2) Insufficient data (general case — no recent demand event)
    if samples_used < cfg.min_samples:
        return _build(
            state=None, suggested=None,
            confidence="insufficient",
            reason=(
                f"Only {samples_used} of {cfg.min_samples} required KH "
                f"snapshots in the {cfg.window_days}-day window."
            ),
        )

    # 3) Cooldown
    if in_cooldown and cooldown_until is not None:
        return _build(
            state=current_dose_mL, suggested=current_dose_mL,
            confidence="medium",
            reason=(
                f"In cooldown until {cooldown_until.isoformat()} after "
                f"recent acknowledgment/dismissal."
            ),
        )

    # 4) Hysteresis — value median inside the target band ± hysteresis = no action
    band_low = cfg.target_min - cfg.hysteresis_dkh
    band_high = cfg.target_max + cfg.hysteresis_dkh
    assert kh_median is not None  # min_samples gate ensures this
    if _within(kh_median, band_low, band_high):
        conf = "low" if calibration_warning else "high"
        suffix = (
            " (calibration overdue — input reliability questionable)"
            if calibration_warning else ""
        )
        return _build(
            state=current_dose_mL, suggested=current_dose_mL,
            confidence=conf,
            reason=(
                f"{cfg.param_label} median {kh_median:.2f} {cfg.value_unit} "
                f"is within target band "
                f"{cfg.target_min}–{cfg.target_max} (±{cfg.hysteresis_dkh} "
                f"hysteresis); holding current dose.{suffix}"
            ),
        )

    # 5) Trend rule — must be persistently outside the band
    trend_days = _trend_days_outside_band(snaps, cfg.target_min, cfg.target_max)
    if trend_days < cfg.min_trend_days:
        return _build(
            state=current_dose_mL, suggested=current_dose_mL,
            confidence="medium",
            reason=(
                f"{cfg.param_label} median {kh_median:.2f} {cfg.value_unit} "
                f"outside target band, but only {trend_days} "
                f"consecutive day(s) trending — "
                f"need {cfg.min_trend_days} before adjusting."
            ),
        )

    # 6) Compute suggestion using spec potency, spread over correction_period_days
    # (current_dose check moved to the top — see branch 0).
    delta_kh = midpoint - kh_median
    raw_change_mL = delta_kh / (spec_eff * cfg.correction_period_days)
    cap = cfg.step_cap_pct / 100.0
    capped_change_mL = max(
        -current_dose_mL * cap, min(current_dose_mL * cap, raw_change_mL)
    )
    suggested_mL = round(current_dose_mL + capped_change_mL, 2)

    # Confidence: high in normal flow; calibration warning forces low.
    confidence = "low" if calibration_warning else "high"

    capped_note = ""
    if abs(raw_change_mL - capped_change_mL) > 0.01:
        capped_note = (
            f" (raw {raw_change_mL:+.2f} mL clipped to "
            f"±{cfg.step_cap_pct:.0f}% step cap)"
        )
    cal_note = (
        " — calibration overdue, treat as advisory only"
        if calibration_warning else ""
    )

    obs_note = ""
    if obs_slope is not None and obs_dose_median is not None:
        obs_note = (
            f" Observed: {cfg.param_label} slope {obs_slope:+.3f} "
            f"{cfg.value_unit}/day at {obs_dose_median:.2f} mL/day."
        )

    reason = (
        f"{cfg.param_label} median {kh_median:.2f} {cfg.value_unit} "
        f"{'below' if delta_kh > 0 else 'above'} target midpoint "
        f"{midpoint:.2f} (delta {-delta_kh:+.2f}); spec potency "
        f"{spec_eff:.4f} {cfg.value_unit}/mL spread over "
        f"{cfg.correction_period_days:.0f} days → "
        f"{capped_change_mL:+.2f} mL/day{capped_note}.{obs_note}{cal_note}"
    )

    return _build(
        state=suggested_mL, suggested=suggested_mL,
        confidence=confidence, reason=reason,
    )
