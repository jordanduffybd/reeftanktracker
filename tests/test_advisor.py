"""Pure-algorithm tests for the alk dosing advisor.

No HA harness required — `advisor.py` is HA-free. These tests cover the
stability rules Jordan stated as hard requirements:
  1. Never react to a single reading (median over window).
  2. Trends only — must be persistently outside band.
  3. Cooldown after ack/dismiss.
  4. ±step-cap on suggested change.
  5. Confidence gate when observed efficiency drifts from spec.
Plus demand-change learning mode and the calibration-overdue caveat.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reeftanktracker.advisor import (
    Acknowledgment,
    AdvisorConfig,
    DemandChange,
    Dismissal,
    Recommendation,
    Snapshot,
    WaterChange,
    compute_recommendation,
)
from reeftanktracker import alk_advisor


UTC = timezone.utc


def _now() -> datetime:
    return datetime(2026, 5, 6, 23, 55, tzinfo=UTC)


def _stable(now: datetime, kh: float, dose: float, days: int) -> list[Snapshot]:
    """Generate `days` consecutive end-of-day snapshots."""
    return [
        Snapshot(at=now - timedelta(days=days - 1 - i), kh=kh, dose_mL=dose)
        for i in range(days)
    ]


# ---------------------------------------------------------------------------
# Insufficient data
# ---------------------------------------------------------------------------
def test_insufficient_samples_returns_unavailable():
    cfg = AdvisorConfig()
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.7, dose=3.0, days=2),
        current_dose_mL=3.0,
        cfg=cfg,
    )
    assert rec.state is None
    assert rec.confidence == "insufficient"
    assert "Only 2" in rec.reason


# ---------------------------------------------------------------------------
# In-band → hold
# ---------------------------------------------------------------------------
def test_kh_in_band_holds_current_dose():
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.7, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    assert rec.state == 3.0
    assert rec.suggested_dose_mL == 3.0
    assert rec.change_mL == 0.0
    assert rec.confidence == "high"
    assert "within target band" in rec.reason


def test_kh_just_outside_band_within_hysteresis_holds():
    """8.5–8.9 band ± 0.1 hysteresis → 8.4 still holds."""
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.4, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(hysteresis_dkh=0.1),
    )
    assert rec.suggested_dose_mL == 3.0
    assert rec.change_mL == 0.0


# ---------------------------------------------------------------------------
# Stability rule 1 — single bad reading must NOT swing the recommendation
# ---------------------------------------------------------------------------
def test_single_spike_is_suppressed_by_median():
    """Six in-band days plus one wild outlier — median stays in band."""
    now = _now()
    snaps = _stable(now, kh=8.7, dose=3.0, days=6)
    snaps.append(Snapshot(at=now, kh=12.0, dose_mL=3.0))  # bad titration
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0, cfg=AdvisorConfig(),
    )
    assert rec.suggested_dose_mL == 3.0
    assert rec.change_mL == 0.0


# ---------------------------------------------------------------------------
# Stability rule 2 — must be persistently outside band
# ---------------------------------------------------------------------------
def test_only_recent_drift_below_trend_threshold_holds():
    """KH dropped today and yesterday but most days are in-band — trend
    rule says wait for more days before adjusting.
    """
    now = _now()
    # Construct a window where the median is below band but only the
    # last 2 days are below — need 3 by default.
    days = [8.7, 8.7, 8.7, 8.7, 8.0, 8.0, 8.0]   # median = 8.7 (in band) — won't trigger anyway
    # Force median outside band by skewing more days low
    days = [8.7, 8.0, 8.0, 8.0, 8.0, 8.0, 8.7]   # median 8.0
    # But trend tail is only 1 day (last is 8.7)
    snaps = [
        Snapshot(at=now - timedelta(days=6 - i), kh=v, dose_mL=3.0)
        for i, v in enumerate(days)
    ]
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0, cfg=AdvisorConfig(),
    )
    # Median 8.0 is below the 8.5 band, but the most recent day is 8.7
    # (in-band) so trend tail = 0 → must hold.
    assert rec.suggested_dose_mL == 3.0
    assert "trending" in rec.reason


def test_persistent_drift_below_band_suggests_increase():
    """7 days steadily at 8.0 dKH — clearly below band, suggest more dose."""
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.0, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    # Suggested dose should be higher than 3.0 (need to raise KH).
    # delta = 0.7 dKH, raw +1.18 mL/day, capped at +10% = +0.30 → 3.30 mL.
    assert rec.suggested_dose_mL == pytest.approx(3.3, abs=0.01)
    assert rec.change_mL > 0
    assert rec.confidence == "high"


def test_persistent_drift_above_band_suggests_decrease():
    """7 days at 9.5 dKH (Jordan's current state, ish) — suggest less dose."""
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=9.5, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    assert rec.suggested_dose_mL is not None
    assert rec.suggested_dose_mL < 3.0
    assert rec.change_mL < 0


# ---------------------------------------------------------------------------
# Stability rule 3 — cooldown after ack
# ---------------------------------------------------------------------------
def test_cooldown_after_ack_blocks_new_suggestion():
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.0, dose=3.0, days=7),  # below band
        current_dose_mL=3.0,
        cfg=AdvisorConfig(cooldown_days=5),
        acknowledgments=[Acknowledgment(
            at=now - timedelta(days=2),
            applied_value_mL=3.5, prev_value_mL=3.0,
        )],
    )
    # Suggestion should be held during cooldown
    assert rec.suggested_dose_mL == 3.0
    assert rec.change_mL == 0.0
    assert "cooldown" in rec.reason.lower()
    assert rec.cooldown_until is not None


def test_cooldown_expired_allows_new_suggestion():
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.0, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(cooldown_days=5),
        acknowledgments=[Acknowledgment(
            at=now - timedelta(days=10),
            applied_value_mL=3.5, prev_value_mL=3.0,
        )],
    )
    # Cooldown long expired → free to suggest
    assert rec.suggested_dose_mL is not None
    assert rec.suggested_dose_mL > 3.0


def test_dismiss_uses_shorter_cooldown():
    now = _now()
    cfg = AdvisorConfig(cooldown_days=5, dismiss_cooldown_days=2)
    # 3 days after dismissal — dismiss cooldown (2d) expired but ack (5d) wouldn't be
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.0, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=cfg,
        dismissals=[Dismissal(
            at=now - timedelta(days=3), suggested_value_mL=3.5,
        )],
    )
    assert rec.suggested_dose_mL is not None
    assert rec.suggested_dose_mL > 3.0


# ---------------------------------------------------------------------------
# Stability rule 4 — step cap
# ---------------------------------------------------------------------------
def test_change_capped_at_step_cap_pct():
    """KH catastrophically low — raw suggestion would double the dose,
    but step cap holds it to +10%."""
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=5.0, dose=3.0, days=7),  # 3.7 dKH below midpoint
        current_dose_mL=3.0,
        cfg=AdvisorConfig(step_cap_pct=10),
    )
    # +10% of 3.0 = +0.3 → suggested ≈ 3.3
    assert rec.suggested_dose_mL == pytest.approx(3.3, abs=0.01)
    assert "clipped" in rec.reason or "step cap" in rec.reason


# ---------------------------------------------------------------------------
# Diagnostics — observed slope/dose are reported but not used in the math
# ---------------------------------------------------------------------------
def test_diagnostics_report_observed_slope_and_dose():
    """Window with rising KH at constant dose — slope and dose median
    are surfaced as attributes for the user's show-your-work view."""
    now = _now()
    snaps = [
        Snapshot(at=now - timedelta(days=6 - i),
                 kh=7.5 + 0.1 * i, dose_mL=3.0)
        for i in range(7)
    ]
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0, cfg=AdvisorConfig(),
    )
    assert rec.observed_slope_dkh_per_day == pytest.approx(0.1, abs=0.001)
    assert rec.observed_dose_median_mL == pytest.approx(3.0, abs=0.001)


def test_correction_uses_spec_potency_with_spread():
    """KH 9.08 (Jordan's actual reading), dose 3 mL/day, spec 0.1 per 100L
    (Red Sea Foundation B vendor-published).

    For 425L: spec_eff = 0.1 * 100/425 = 0.0235 dKH/mL.
    delta_kh = 8.7 - 9.08 = -0.38 dKH.
    raw_change = -0.38 / (0.0235 * 7) ≈ -2.31 mL/day.
    capped at -10% of 3 = -0.30 → suggested = 2.7 mL/day.
    """
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=9.08, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    # Above band, persistent trend, step-capped
    assert rec.suggested_dose_mL == pytest.approx(2.7, abs=0.01)
    assert rec.change_mL == pytest.approx(-0.3, abs=0.01)
    assert rec.confidence == "high"


# ---------------------------------------------------------------------------
# Demand-change events
# ---------------------------------------------------------------------------
def test_demand_change_truncates_window_and_holds_during_learning():
    """Just added corals → window truncates, learning mode active."""
    now = _now()
    snaps = _stable(now, kh=8.7, dose=3.0, days=7)  # 7 days pre-event
    # Demand change yesterday — only 1 day post-event, less than min_samples_after_event=3
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0,
        cfg=AdvisorConfig(),
        demand_changes=[DemandChange(
            at=now - timedelta(days=1),
            reason="added 3 SPS frags",
            expected_direction="increase",
        )],
    )
    assert rec.suggested_dose_mL == 3.0
    assert rec.change_mL == 0.0
    assert rec.confidence == "low"
    assert "Learning mode" in rec.reason


def test_demand_change_clears_after_learning_period():
    """Demand-change >3 days ago → no learning mode, normal evaluation."""
    now = _now()
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.0, dose=3.0, days=7),  # below band
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
        demand_changes=[DemandChange(
            at=now - timedelta(days=4),  # past learning period
            reason="added frags",
        )],
    )
    # Should evaluate normally and suggest increase
    assert rec.suggested_dose_mL is not None
    assert rec.suggested_dose_mL > 3.0


def test_demand_change_truncates_pre_event_data():
    """Snapshots from before the demand event must NOT be used."""
    now = _now()
    # Pre-event: 4 daily snapshots at 8.7 (in band), well before the event.
    pre = [
        Snapshot(at=now - timedelta(days=10 - i), kh=8.7, dose_mL=3.0)
        for i in range(4)
    ]   # → days 10, 9, 8, 7 ago
    # Demand event 5 days ago.
    event_at = now - timedelta(days=5)
    # Post-event: 5 snapshots at 8.0 (well below band), starting 4 days ago.
    post = [
        Snapshot(at=now - timedelta(days=4 - i), kh=8.0, dose_mL=3.0)
        for i in range(5)
    ]   # → days 4, 3, 2, 1, 0 ago
    snaps = pre + post
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0,
        cfg=AdvisorConfig(min_samples_after_event=3, window_days=14),
        demand_changes=[DemandChange(at=event_at, reason="livestock change")],
    )
    # Pre-event snapshots (8.7) must NOT contribute. Median should be 8.0,
    # not the mixed 8.4 it would be if pre-event leaked in.
    assert rec.samples_used == 5
    assert rec.kh_median == pytest.approx(8.0, abs=0.05)


# ---------------------------------------------------------------------------
# Calibration-overdue caveat
# ---------------------------------------------------------------------------
def test_calibration_warning_downgrades_confidence_within_band():
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.7, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
        calibration_warning=True,
    )
    assert rec.calibration_warning is True
    assert rec.confidence == "low"
    assert "calibration overdue" in rec.reason.lower()


def test_calibration_warning_downgrades_confidence_with_change():
    """Even with a real change suggestion, confidence is forced to low."""
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.0, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
        calibration_warning=True,
    )
    assert rec.calibration_warning is True
    assert rec.confidence == "low"
    assert rec.suggested_dose_mL is not None
    assert "calibration" in rec.reason.lower()


# ---------------------------------------------------------------------------
# Zero current dose
# ---------------------------------------------------------------------------
def test_zero_current_dose_returns_unavailable():
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.0, dose=0.0, days=7),  # below band, no dose
        current_dose_mL=0.0,
        cfg=AdvisorConfig(),
    )
    assert rec.state is None
    assert rec.confidence == "insufficient"
    # Updated message: "doser unreachable or zero" replaces the older
    # "zero or unknown — cannot compute". Both old and new should still
    # convey "doser is missing / zero".
    assert "unreachable" in rec.reason.lower() or "zero" in rec.reason.lower()


def test_none_current_dose_reports_doser_unreachable_not_learning_mode():
    """When the alk doser is unavailable AND we're inside the
    demand-change learning window, the advisor must report the
    DOSER issue, not a stale "learning mode" message. Without this
    short-circuit, a transient ReefBeat outage during learning mode
    leaves the user staring at "Learning mode after demand change..."
    while the actual cause is the doser sensor being unreachable.

    Regression for the prod issue observed 2026-05-08 where the user
    turned off ReefBeat for maintenance and saw the advisor stuck on
    `state=unknown` with a misleading learning-mode reason."""
    # Demand change 1 day ago + insufficient time to exit learning mode
    now = _now()
    cfg = AdvisorConfig(min_samples_after_event=3)
    rec = compute_recommendation(
        now=now,
        snapshots=_stable(now, kh=8.7, dose=3.0, days=2),
        current_dose_mL=None,  # ← ReefBeat doser sensor unreachable
        cfg=cfg,
        demand_changes=[DemandChange(
            at=now - timedelta(days=1),
            reason="New frags added",
            expected_direction="increase",
        )],
    )
    assert rec.state is None
    assert rec.confidence == "insufficient"
    # The user must see the actionable cause, NOT the stale demand-change message
    assert "unreachable" in rec.reason.lower() or "doser" in rec.reason.lower()
    assert "learning mode" not in rec.reason.lower()


# ---------------------------------------------------------------------------
# Water-change events
# ---------------------------------------------------------------------------
def test_water_change_filters_settling_snapshots():
    """A spurious post-WC snapshot must NOT contribute to the median."""
    now = _now()
    # Pre-WC snapshots at 8.7 (well within band)
    snaps = [
        Snapshot(at=now - timedelta(days=6 - i, hours=2), kh=8.7, dose_mL=3.0)
        for i in range(6)
    ]
    # WC 12h ago
    wc_at = now - timedelta(hours=12)
    # A post-WC snapshot 6h ago at a wildly off value — must be excluded
    # by the 24h settling filter.
    snaps.append(Snapshot(at=now - timedelta(hours=6), kh=12.0, dose_mL=3.0))
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0,
        cfg=AdvisorConfig(min_samples=3),
        water_changes=[WaterChange(at=wc_at, percent=10.0)],
    )
    # The high-KH within-settling snapshot must NOT contribute to the median.
    assert rec.kh_median == pytest.approx(8.7, abs=0.05)
    assert rec.samples_excluded_for_wc == 1
    assert "Excluded" in rec.reason
    assert rec.last_water_change_at == wc_at
    assert rec.last_water_change_percent == 10.0


def test_water_change_does_not_truncate_window():
    """Window stays at now - window_days, NOT the WC timestamp.
    Contrast with demand-change which DOES truncate."""
    now = _now()
    cfg = AdvisorConfig()
    snaps = _stable(now, kh=8.7, dose=3.0, days=7)
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0,
        cfg=cfg,
        # WC 3 days ago — settling filter excludes the snapshots that
        # land within 24h after it (now-3d and now-2d), but window_start
        # itself is unchanged.
        water_changes=[WaterChange(at=now - timedelta(days=3), percent=10.0)],
    )
    expected_start = now - timedelta(days=cfg.window_days)
    assert rec.window_start == expected_start


def test_water_change_overlapping_demand_change():
    """Demand change truncates window; WC settling filters on top — both
    layered, no crash, sensible result."""
    now = _now()
    cfg = AdvisorConfig(min_samples_after_event=3, min_samples=3)
    # Demand event 5 days ago → window truncated
    demand_at = now - timedelta(days=5)
    # WC 6h ago → settling filter removes the latest snapshot
    wc_at = now - timedelta(hours=6)
    snaps = [
        Snapshot(at=now - timedelta(days=4 - i), kh=8.7, dose_mL=3.0)
        for i in range(4)
    ] + [
        Snapshot(at=now - timedelta(hours=3), kh=11.0, dose_mL=3.0),  # within settling
    ]
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0, cfg=cfg,
        demand_changes=[DemandChange(at=demand_at, reason="frags")],
        water_changes=[WaterChange(at=wc_at, percent=10.0)],
    )
    # Demand-change truncates window to demand_at
    assert rec.window_start == demand_at
    # Settling filter excludes the within-settling snapshot
    assert rec.samples_excluded_for_wc == 1
    # KH median doesn't include the spurious 11.0
    assert rec.kh_median == pytest.approx(8.7, abs=0.05)


def test_water_change_attributes_serialize_to_iso():
    """`as_attributes` covers the new water-change fields."""
    now = _now()
    snaps = _stable(now, kh=8.7, dose=3.0, days=7)
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.0,
        cfg=AdvisorConfig(wc_settling_hours=0.0),  # settling disabled for this test
        water_changes=[WaterChange(at=now - timedelta(days=2), percent=15.0)],
    )
    attrs = rec.as_attributes()
    assert attrs["last_water_change_percent"] == 15.0
    assert isinstance(attrs["last_water_change_at"], str)
    assert "T" in attrs["last_water_change_at"]
    assert attrs["samples_excluded_for_wc"] == 0
    assert attrs["days_since_water_change"] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# Empirical potency from acknowledged dose changes
# ---------------------------------------------------------------------------
def _ack(at: datetime, applied: float, prev: float) -> Acknowledgment:
    return Acknowledgment(at=at, applied_value_mL=applied, prev_value_mL=prev)


def test_empirical_potency_matches_spec_when_reality_matches():
    """Synthesize: KH stable around 8.7 at 3 mL/day, then dose jumps to
    3.5 mL/day, post-ack KH rises at the rate spec predicts.

    spec_eff for 425 L = 0.1 * 100/425 = 0.0235 dKH/mL.
    Δdose = 0.5 mL/day → expected Δslope = 0.5 * 0.0235 = 0.01175 dKH/day.
    Pre slope ≈ 0; post slope ≈ 0.01175.
    Empirical potency = (0.01175 - 0) / 0.5 = 0.0235 ≈ spec.
    """
    ack_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    now = ack_at + timedelta(days=7)
    # Pre: 7 daily snapshots at exactly 8.7 (slope = 0)
    pre_snaps = [
        Snapshot(at=ack_at - timedelta(days=7 - i), kh=8.7, dose_mL=3.0)
        for i in range(7)
    ]
    # Post: 7 snapshots starting at 8.7, rising 0.01175 dKH/day
    post_snaps = [
        Snapshot(
            at=ack_at + timedelta(days=i + 1),
            kh=8.7 + 0.01175 * (i + 1),
            dose_mL=3.5,
        )
        for i in range(7)
    ]
    rec = compute_recommendation(
        now=now,
        snapshots=pre_snaps + post_snaps,
        current_dose_mL=3.5,
        cfg=AdvisorConfig(),
        acknowledgments=[_ack(ack_at, applied=3.5, prev=3.0)],
    )
    # Empirical ≈ spec → no drift warning
    spec_eff = 0.1 * 100.0 / 425.0
    assert rec.empirical_potency_dkh_per_mL is not None
    assert abs(rec.empirical_potency_dkh_per_mL - spec_eff) < 0.005
    assert abs(rec.empirical_to_spec_ratio - 1.0) < 0.2
    assert rec.spec_drift_warning is False
    assert "ack at" in rec.empirical_potency_basis


def test_empirical_potency_diverges_from_spec_triggers_warning():
    """Same setup but post-ack slope is double what spec predicts → ratio≈2."""
    ack_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    now = ack_at + timedelta(days=7)
    pre_snaps = [
        Snapshot(at=ack_at - timedelta(days=7 - i), kh=8.7, dose_mL=3.0)
        for i in range(7)
    ]
    # Post-slope is 2× the spec-predicted 0.01175
    post_snaps = [
        Snapshot(
            at=ack_at + timedelta(days=i + 1),
            kh=8.7 + 0.0235 * (i + 1),
            dose_mL=3.5,
        )
        for i in range(7)
    ]
    rec = compute_recommendation(
        now=now,
        snapshots=pre_snaps + post_snaps,
        current_dose_mL=3.5,
        cfg=AdvisorConfig(),
        acknowledgments=[_ack(ack_at, applied=3.5, prev=3.0)],
    )
    assert rec.empirical_potency_dkh_per_mL is not None
    assert abs(rec.empirical_to_spec_ratio - 2.0) < 0.3
    assert rec.spec_drift_warning is True
    assert "observed potency" in rec.reason
    assert "drift band" in rec.reason


def test_empirical_potency_skipped_for_small_dose_change():
    ack_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    now = ack_at + timedelta(days=7)
    snaps = _stable(now, kh=8.7, dose=3.0, days=14)
    rec = compute_recommendation(
        now=now, snapshots=snaps, current_dose_mL=3.05,
        cfg=AdvisorConfig(),
        acknowledgments=[_ack(ack_at, applied=3.05, prev=3.0)],   # Δ=0.05 < 0.1
    )
    assert rec.empirical_potency_dkh_per_mL is None
    assert "below" in rec.empirical_potency_basis
    assert "threshold" in rec.empirical_potency_basis


def test_empirical_potency_skipped_when_no_acks():
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.7, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    assert rec.empirical_potency_dkh_per_mL is None
    assert "no acknowledgment" in rec.empirical_potency_basis


def test_empirical_potency_skipped_when_insufficient_post_ack():
    """Ack 1 day ago — not enough post-ack snapshots (need min_samples=5)."""
    ack_at = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
    now = ack_at + timedelta(days=1)
    pre_snaps = [
        Snapshot(at=ack_at - timedelta(days=7 - i), kh=8.7, dose_mL=3.0)
        for i in range(7)
    ]
    post_snaps = [
        Snapshot(at=ack_at + timedelta(hours=12), kh=8.7, dose_mL=3.5),
    ]
    rec = compute_recommendation(
        now=now, snapshots=pre_snaps + post_snaps, current_dose_mL=3.5,
        cfg=AdvisorConfig(),
        acknowledgments=[_ack(ack_at, applied=3.5, prev=3.0)],
    )
    assert rec.empirical_potency_dkh_per_mL is None
    assert "insufficient data" in rec.empirical_potency_basis


# ---------------------------------------------------------------------------
# Supplement profile / auto-detection
# ---------------------------------------------------------------------------
def test_supplement_entity_derived_from_daily_dose_id():
    eid = "sensor.rsdose4_1881531676_head_2_daily_dose"
    assert (
        alk_advisor._supplement_entity_for_head(eid)
        == "sensor.rsdose4_1881531676_head_2_supplement"
    )


def test_supplement_entity_returns_none_for_non_dose_entity():
    assert alk_advisor._supplement_entity_for_head("sensor.kh_keeper_kh") is None


def test_label_match_red_sea_foundation_b():
    """The actual state Jordan's doser reports."""
    assert (
        alk_advisor._label_match_profile(
            "KH/Alkalinity (Foundation B)",
            alk_advisor.BUILTIN_LABEL_PATTERNS,
        )
        == "red_sea_foundation_b"
    )


def test_label_match_returns_none_for_unknown():
    pats = alk_advisor.BUILTIN_LABEL_PATTERNS
    assert alk_advisor._label_match_profile("Some unknown supplement", pats) is None
    assert alk_advisor._label_match_profile("", pats) is None
    assert alk_advisor._label_match_profile(None, pats) is None


def test_red_sea_foundation_b_default_is_0_1_dkh():
    """Vendor-published value: 1 mL / 100 L raises 0.1 dKH."""
    prof = alk_advisor.BUILTIN_PROFILES["red_sea_foundation_b"]
    assert prof["eff_dkh_per_mL_per_100L"] == 0.1


class _StubCoord:
    """Just enough of ReefDataCoordinator for `_resolve_spec_efficiency`
    and the all_profiles / all_label_patterns helpers."""
    def __init__(self, options: dict, profiles: list[dict] | None = None):
        self._options = options
        self.supplement_profiles = list(profiles or [])
    def get_advisor_config(self) -> dict:
        return self._options
    def supplement_profiles_for(self, param_id: str) -> list[dict]:
        # Mirrors ReefDataCoordinator.supplement_profiles_for: filters
        # by param_id, treating profiles missing the field as "kh" for
        # back-compat with profiles created before 0.4.4.
        return [
            p for p in self.supplement_profiles
            if p.get("param_id", "kh") == param_id
        ]


def test_resolve_spec_eff_auto_with_detected_profile():
    """Profile=auto + detected match → use the matched preset's value."""
    coord = _StubCoord({alk_advisor.OPT_SUPPLEMENT_PROFILE: "auto"})
    val, src = alk_advisor._resolve_spec_efficiency(
        coord, detected_profile="red_sea_foundation_b",
    )
    assert val == 0.1
    assert "auto-detected" in src
    assert "Foundation B" in src


def test_resolve_spec_eff_auto_no_detection_falls_back_to_manual():
    """Profile=auto + no detected match → use manual OPT_SUPPLEMENT_EFF."""
    coord = _StubCoord({
        alk_advisor.OPT_SUPPLEMENT_PROFILE: "auto",
        alk_advisor.OPT_SUPPLEMENT_EFF: 0.143,
    })
    val, src = alk_advisor._resolve_spec_efficiency(coord, detected_profile=None)
    assert val == 0.143
    assert "custom" in src.lower()


def test_resolve_spec_eff_explicit_profile_ignores_detection():
    """Profile explicitly set → ignore auto-detect."""
    coord = _StubCoord({
        alk_advisor.OPT_SUPPLEMENT_PROFILE: "tropic_marin_alkalinity_plus",
    })
    val, src = alk_advisor._resolve_spec_efficiency(
        coord, detected_profile="red_sea_foundation_b",
    )
    assert val == 0.1
    assert "Tropic Marin" in src


def test_resolve_spec_eff_custom_uses_manual_value():
    coord = _StubCoord({
        alk_advisor.OPT_SUPPLEMENT_PROFILE: "custom",
        alk_advisor.OPT_SUPPLEMENT_EFF: 0.42,
    })
    val, src = alk_advisor._resolve_spec_efficiency(coord, detected_profile=None)
    assert val == 0.42
    assert "custom" in src.lower()


def test_all_profiles_merges_builtins_and_user():
    coord = _StubCoord({}, profiles=[{
        "id": "brightwell_alkalin8_3",
        "label": "Brightwell Alkalin8.3",
        "eff_dkh_per_mL_per_100L": 0.083,
        "label_patterns": ["alkalin8"],
        # No param_id — legacy / kh-implicit profile (back-compat)
    }])
    merged = alk_advisor.all_profiles(coord)
    assert "auto" in merged
    assert "red_sea_foundation_b" in merged
    assert "tropic_marin_alkalinity_plus" in merged
    assert "custom" in merged
    assert "brightwell_alkalin8_3" in merged
    assert merged["brightwell_alkalin8_3"]["eff_dkh_per_mL_per_100L"] == 0.083


def test_all_profiles_excludes_non_kh_supplements():
    """Profiles with param_id != "kh" must NOT appear in the alk
    advisor's dropdown — those target other parameters and would
    confuse the user (and potentially the auto-detect, if patterns
    matched a non-alk supplement label on the alk doser).

    Regression for the 0.4.4 enhancement that added param_id."""
    coord = _StubCoord({}, profiles=[
        {
            "id": "foundation_b_custom",
            "label": "Foundation B Custom",
            "eff_dkh_per_mL_per_100L": 0.1,
            "param_id": "kh",
            "label_patterns": [],
        },
        {
            "id": "quantum_ar_phosphate",
            "label": "Quantum AR Phosphate",
            "eff_dkh_per_mL_per_100L": None,
            "param_id": "phosphate",
            "label_patterns": [],
        },
        {
            "id": "quantum_hr_nitrate",
            "label": "Quantum HR Nitrate",
            "eff_dkh_per_mL_per_100L": None,
            "param_id": "nitrate",
            "label_patterns": [],
        },
    ])
    merged = alk_advisor.all_profiles(coord)
    # KH profile shows up
    assert "foundation_b_custom" in merged
    # Non-KH profiles are FILTERED OUT
    assert "quantum_ar_phosphate" not in merged
    assert "quantum_hr_nitrate" not in merged


def test_all_label_patterns_excludes_non_kh_patterns():
    """Auto-detect on the alk doser's _supplement state must not
    match a phosphate / nitrate supplement's pattern even if the
    user happened to register one with `label_patterns`."""
    coord = _StubCoord({}, profiles=[
        {
            "id": "kh_with_pattern",
            "label": "KH supplement with pattern",
            "eff_dkh_per_mL_per_100L": 0.1,
            "param_id": "kh",
            "label_patterns": ["my_alk_label"],
        },
        {
            "id": "po4_with_pattern",
            "label": "PO4 supplement with pattern",
            "eff_dkh_per_mL_per_100L": None,
            "param_id": "phosphate",
            "label_patterns": ["should_not_match"],
        },
    ])
    patterns = alk_advisor.all_label_patterns(coord)
    pattern_ids = {pid for pid, _ in patterns}
    assert "kh_with_pattern" in pattern_ids
    # Non-KH patterns are excluded
    assert "po4_with_pattern" not in pattern_ids


def test_all_label_patterns_appends_user_after_builtin():
    coord = _StubCoord({}, profiles=[{
        "id": "brightwell_alkalin8_3",
        "label": "Brightwell Alkalin8.3",
        "eff_dkh_per_mL_per_100L": 0.083,
        "label_patterns": ["alkalin8", "alkalin 8"],
    }])
    pats = alk_advisor.all_label_patterns(coord)
    # Builtins come first
    assert pats[0][0] == "red_sea_foundation_b"
    # User entry appended at end
    assert pats[-1] == ("brightwell_alkalin8_3", ["alkalin8", "alkalin 8"])


def test_label_match_uses_user_pattern():
    coord = _StubCoord({}, profiles=[{
        "id": "brightwell_alkalin8_3",
        "label": "Brightwell Alkalin8.3",
        "eff_dkh_per_mL_per_100L": 0.083,
        "label_patterns": ["alkalin8"],
    }])
    pats = alk_advisor.all_label_patterns(coord)
    matched = alk_advisor._label_match_profile("Brightwell Alkalin8.3", pats)
    assert matched == "brightwell_alkalin8_3"


def test_resolve_spec_eff_uses_user_profile_when_selected():
    coord = _StubCoord(
        {alk_advisor.OPT_SUPPLEMENT_PROFILE: "brightwell_alkalin8_3"},
        profiles=[{
            "id": "brightwell_alkalin8_3",
            "label": "Brightwell Alkalin8.3",
            "eff_dkh_per_mL_per_100L": 0.083,
            "label_patterns": [],
        }],
    )
    val, src = alk_advisor._resolve_spec_efficiency(coord, detected_profile=None)
    assert val == 0.083
    assert "Brightwell" in src


def test_resolve_spec_eff_auto_detects_user_profile():
    coord = _StubCoord(
        {alk_advisor.OPT_SUPPLEMENT_PROFILE: "auto"},
        profiles=[{
            "id": "brightwell_alkalin8_3",
            "label": "Brightwell Alkalin8.3",
            "eff_dkh_per_mL_per_100L": 0.083,
            "label_patterns": ["alkalin8"],
        }],
    )
    val, src = alk_advisor._resolve_spec_efficiency(
        coord, detected_profile="brightwell_alkalin8_3",
    )
    assert val == 0.083
    assert src.startswith("auto-detected:")
    assert "Brightwell" in src


# ---------------------------------------------------------------------------
# as_attributes() shape
# ---------------------------------------------------------------------------
def test_as_attributes_serializes_datetimes_to_iso():
    rec = compute_recommendation(
        now=_now(),
        snapshots=_stable(_now(), kh=8.7, dose=3.0, days=7),
        current_dose_mL=3.0,
        cfg=AdvisorConfig(),
    )
    attrs = rec.as_attributes()
    # Datetime fields are strings in attributes
    assert isinstance(attrs["window_end"], str)
    assert isinstance(attrs["window_start"], str)
    assert "T" in attrs["window_end"]
    # None datetime fields stay None
    assert attrs["last_acknowledged_at"] is None
    # Confidence and reason present
    assert "confidence" in attrs
    assert "reason" in attrs
