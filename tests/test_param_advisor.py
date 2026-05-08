"""Tests for `param_advisor.compute_for_param` — the generalized
per-element advisor compute path that wraps the parameter-agnostic
algorithm engine in a config + state-resolution layer.

These cover the 0.5.0 Calcium advisor specifically; the framework
generalizes to Mg / NO3 / PO4 in subsequent releases.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from reeftanktracker import param_advisor


class _StubCoord:
    """Minimal coordinator stub for `compute_for_param`. Mirrors the
    real coordinator's surface that param_advisor reads:
      - `get_advisor_config()` → entry.options-equivalent dict
      - `supplement_profiles_for(param_id)` → filtered profile list
      - `advisor_snapshots / acks / dismissals / demand_changes /
         water_changes(param_id)` → per-param state lists
    """
    def __init__(
        self, options: dict, profiles: list[dict] | None = None,
        snapshots: list[dict] | None = None,
    ):
        self._options = dict(options)
        self.supplement_profiles = list(profiles or [])
        self._snapshots = list(snapshots or [])

    def get_advisor_config(self) -> dict:
        return self._options

    def supplement_profiles_for(self, param_id: str) -> list[dict]:
        return [
            p for p in self.supplement_profiles
            if param_id in (
                [p["param_id"]] if isinstance(p.get("param_id"), str)
                else (p.get("param_id") or ["kh"])
            )
        ]

    def advisor_snapshots(self, param_id: str) -> list[dict]:
        return self._snapshots if param_id == "calcium" else []

    def advisor_acknowledgments(self, _param_id: str) -> list[dict]:
        return []

    def advisor_dismissals(self, _param_id: str) -> list[dict]:
        return []

    def advisor_demand_changes(self, _param_id: str) -> list[dict]:
        return []

    def advisor_water_changes(self, _param_id: str) -> list[dict]:
        return []


def _make_hass(states: dict[str, str | None] | None = None):
    """A barely-functional HomeAssistant stub that returns canned
    states for the doser-head reads."""
    states = states or {}
    hass = MagicMock()

    def _get(eid):
        if eid not in states:
            return None
        s = MagicMock()
        s.state = states[eid]
        return s

    hass.states.get.side_effect = _get
    return hass


# ---------------------------------------------------------------------------
# Disabled / unknown-param paths
# ---------------------------------------------------------------------------
def test_returns_none_when_advisor_disabled():
    """Per-element advisor sensor's `available` mirrors the enabled
    flag; when disabled, compute should return None so the sensor
    state is `unavailable` rather than fabricating a recommendation."""
    coord = _StubCoord({})
    rec = param_advisor.compute_for_param(_make_hass(), coord, "calcium")
    assert rec is None


def test_returns_none_for_unknown_param():
    """Defensive — typo / future bug should never produce nonsense
    recommendations. Unknown param_id returns None and logs a warning."""
    coord = _StubCoord({"advisor_strontium_enabled": True})
    rec = param_advisor.compute_for_param(_make_hass(), coord, "strontium")
    assert rec is None


# ---------------------------------------------------------------------------
# Spec-efficiency resolution
# ---------------------------------------------------------------------------
def test_spec_eff_falls_back_to_param_default_when_no_profile():
    """No supplement profile selected + no manual entry → use the
    per-parameter built-in default (Foundation A = 2 ppm/mL/100L for Ca)."""
    coord = _StubCoord({"advisor_calcium_enabled": True})
    eff, src = param_advisor._resolve_supplement_potency(coord, "calcium")
    assert eff == 2.0
    assert "default" in src.lower()
    assert "calcium" in src.lower()


def test_spec_eff_uses_manual_value_when_custom():
    """profile=custom + manual override → use the manual value verbatim."""
    coord = _StubCoord({
        "advisor_calcium_enabled": True,
        "advisor_calcium_supplement_profile": "custom",
        "advisor_calcium_spec_efficiency": 3.5,
    })
    eff, src = param_advisor._resolve_supplement_potency(coord, "calcium")
    assert eff == 3.5
    assert "manual" in src.lower()


def test_spec_eff_uses_profile_eff_per_mL_when_set():
    """A profile with `eff_per_mL_per_100L` populated wins over the default."""
    coord = _StubCoord(
        {
            "advisor_calcium_enabled": True,
            "advisor_calcium_supplement_profile": "user_ca_supp",
        },
        profiles=[{
            "id": "user_ca_supp",
            "label": "Some Ca Supplement",
            "param_id": ["calcium"],
            "eff_per_mL_per_100L": 1.5,  # weaker than Foundation A's 2.0
            "eff_dkh_per_mL_per_100L": None,
        }],
    )
    eff, src = param_advisor._resolve_supplement_potency(coord, "calcium")
    assert eff == 1.5
    assert "Some Ca Supplement" in src


def test_spec_eff_falls_back_when_profile_has_no_potency():
    """A profile registered without an `eff_per_mL_per_100L` value
    (e.g. the Foundation A profile registered via 0.4.4 add_supplement_profile)
    falls back to the parameter's default. The source label calls this
    out so the user knows their profile didn't contribute a value."""
    coord = _StubCoord(
        {
            "advisor_calcium_enabled": True,
            "advisor_calcium_supplement_profile": "foundation_a",
        },
        profiles=[{
            "id": "foundation_a",
            "label": "Red Sea Foundation A",
            "param_id": ["calcium"],
            "eff_per_mL_per_100L": None,
            "eff_dkh_per_mL_per_100L": None,
        }],
    )
    eff, src = param_advisor._resolve_supplement_potency(coord, "calcium")
    assert eff == 2.0  # param default
    assert "no potency" in src.lower() or "built-in" in src.lower()


# ---------------------------------------------------------------------------
# OPT key naming
# ---------------------------------------------------------------------------
def test_opt_key_format():
    """Per-param keys use the `advisor_<param>_<setting>` shape."""
    assert param_advisor.opt_key("calcium", "enabled") == "advisor_calcium_enabled"
    assert param_advisor.opt_key("calcium", "heads") == "advisor_calcium_heads"
    assert param_advisor.opt_key(
        "magnesium", "target_min",
    ) == "advisor_magnesium_target_min"


# ---------------------------------------------------------------------------
# PARAM_DEFAULTS sanity
# ---------------------------------------------------------------------------
def test_calcium_defaults_present_and_research_aligned():
    """Sanity-check the calcium defaults match the research consensus
    (see memory: reference_reef_dosing_research.md) AND the SPARSE
    manual-cadence assumption (1-2 readings per month, max 4)."""
    ca = param_advisor.PARAM_DEFAULTS["calcium"]
    # Target 420-440 (SPS-friendly per Holmes-Farley)
    assert ca["target_min"] == 420.0
    assert ca["target_max"] == 440.0
    # ±10% step cap, conservative across reef types
    assert ca["step_cap_pct"] == 10.0
    # 5 ppm hysteresis (within test-kit noise)
    assert ca["hysteresis"] == 5.0
    # Foundation A: 2 ppm Ca per mL per 100L
    assert ca["default_eff_per_mL_per_100L"] == 2.0
    assert ca["value_unit"] == "ppm"
    # Sparse-cadence defaults — typical user tests Ca 1-2x/month.
    # 90-day window accumulates 3-6 readings; min_samples=2 activates
    # the advisor as soon as you have one comparison reading;
    # min_trend_days=2 means a single noisy reading can't trigger
    # action (always need confirmation).
    assert ca["window_days"] == 90
    assert ca["min_samples"] == 2
    assert ca["min_trend_days"] == 2
    assert ca["min_samples_after_event"] == 1
    # 30-day cooldown matches typical testing cadence
    assert ca["cooldown_days"] == 30.0
    # Spread corrections over a month → gentle dose changes
    assert ca["correction_period_days"] == 30.0


def test_param_label_and_unit_propagate_to_advisor_config():
    """`compute_for_param` builds an AdvisorConfig with `param_label` +
    `value_unit` set for the parameter, so the algorithm's reason text
    says "Calcium median X ppm" instead of the alk-default "KH median X dKH"."""
    coord = _StubCoord({"advisor_calcium_enabled": True})
    cfg = param_advisor._build_config(coord, "calcium", spec_eff=2.0)
    assert cfg.param_label == "Calcium"
    assert cfg.value_unit == "ppm"


# ---------------------------------------------------------------------------
# Magnesium defaults (added 0.5.2)
# ---------------------------------------------------------------------------
def test_magnesium_defaults_present():
    """Mg should be registered in PARAM_DEFAULTS with target 1300-1350
    (SPS-friendly per Holmes-Farley) and Foundation C potency 1.0
    ppm/mL/100L."""
    mg = param_advisor.PARAM_DEFAULTS["magnesium"]
    assert mg["target_min"] == 1300.0
    assert mg["target_max"] == 1350.0
    # Mg test kits are coarser than Ca — 20 ppm hysteresis catches real
    # drifts within ±20-30 ppm typical kit noise.
    assert mg["hysteresis"] == 20.0
    # Foundation C: 1 mL per 100L raises Mg by 1 ppm
    assert mg["default_eff_per_mL_per_100L"] == 1.0
    assert mg["value_unit"] == "ppm"
    # Same sparse-cadence defaults as Ca — Mg is also tested manually
    assert mg["window_days"] == 90
    assert mg["min_samples"] == 2


def test_magnesium_param_label_and_unit():
    """Mg advisor should produce 'Magnesium median X ppm' reason text."""
    coord = _StubCoord({"advisor_magnesium_enabled": True})
    cfg = param_advisor._build_config(coord, "magnesium", spec_eff=1.0)
    assert cfg.param_label == "Magnesium"
    assert cfg.value_unit == "ppm"


# ---------------------------------------------------------------------------
# Snowstorm guard — cross-Ca-alk safety check (added 0.5.2)
# ---------------------------------------------------------------------------
def _make_rec(*, current_dose=3.0, suggested_dose=3.5, reason="raise it"):
    """Build a Recommendation that's recommending an INCREASE — what
    the snowstorm guard targets."""
    from reeftanktracker.advisor import Recommendation
    return Recommendation(
        state=suggested_dose,
        current_dose_mL=current_dose,
        suggested_dose_mL=suggested_dose,
        change_mL=suggested_dose - current_dose,
        change_pct=((suggested_dose - current_dose) / current_dose) * 100.0,
        confidence="high",
        reason=reason,
        kh_median=415.0,
        delta_dkh=10.0,
        target_min=420.0,
        target_max=440.0,
        target_midpoint=430.0,
        observed_slope_dkh_per_day=None,
        observed_dose_median_mL=None,
        spec_efficiency_dkh_per_mL=0.4706,
        samples_used=4,
        window_start=None,
        window_end=None,
        cooldown_until=None,
        last_acknowledged_at=None,
        last_acknowledged_value_mL=None,
        last_demand_change_at=None,
        last_demand_change_reason=None,
        days_since_demand_change=None,
        calibration_warning=False,
        detected_supplement_label=None,
        detected_supplement_profile=None,
        spec_efficiency_source="default",
        empirical_potency_dkh_per_mL=None,
        empirical_to_spec_ratio=None,
        empirical_potency_basis="not yet",
        spec_drift_warning=False,
        last_water_change_at=None,
        last_water_change_percent=None,
        days_since_water_change=None,
        samples_excluded_for_wc=0,
    )


class _CoordWithSnapshots(_StubCoord):
    """Stub that lets tests inject per-param snapshot values for
    cross-parameter safety guards (snowstorm reads alk + Mg)."""
    def __init__(self, options: dict, snapshots_by_param: dict[str, list]):
        super().__init__(options)
        self._snaps = snapshots_by_param

    def advisor_snapshots(self, param_id: str) -> list[dict]:
        return self._snaps.get(param_id, [])


def test_snowstorm_guard_suppresses_ca_up_when_alk_high():
    """Refuse to recommend RAISING Calcium when alk > 10 dKH —
    snowstorm precipitation risk. Algorithm output overridden to
    hold dose, reason explains why."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [{"at": "2026-05-08T08:00:00+00:00", "kh": 10.5,
                    "dose_mL": 3.0}],
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1310,
                           "dose_mL": None}],
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5)
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    assert out.suggested_dose_mL == 3.0  # held at current
    assert out.change_mL == 0.0
    assert out.confidence == "low"
    assert "snowstorm" in out.reason.lower()
    assert "alkalinity" in out.reason.lower()
    assert "10.50" in out.reason or "10.5" in out.reason


def test_snowstorm_guard_suppresses_ca_up_when_mg_low():
    """Refuse to recommend RAISING Calcium when Mg < 1200 — low Mg
    fails to inhibit Ca/alk precipitation."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [{"at": "2026-05-08T08:00:00+00:00", "kh": 8.6,
                    "dose_mL": 3.0}],  # alk OK
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1150,
                           "dose_mL": None}],  # Mg too low
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5)
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    assert out.suggested_dose_mL == 3.0
    assert out.change_mL == 0.0
    assert "snowstorm" in out.reason.lower()
    assert "magnesium" in out.reason.lower()
    assert "1150" in out.reason


def test_snowstorm_guard_passes_through_when_chemistry_ok():
    """When alk + Mg are both within safe ranges, guard is a no-op
    and the original recommendation passes through unchanged."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [{"at": "2026-05-08T08:00:00+00:00", "kh": 8.7,
                    "dose_mL": 3.0}],
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1320,
                           "dose_mL": None}],
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5,
                    reason="Calcium median 415 below band")
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    assert out.suggested_dose_mL == 3.5  # unchanged
    assert out.change_mL == 0.5
    assert "snowstorm" not in out.reason.lower()


def test_snowstorm_guard_only_applies_to_calcium():
    """The snowstorm guard is Ca-specific. Mg / KH / etc.
    recommendations pass through unchanged."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [{"at": "2026-05-08T08:00:00+00:00", "kh": 11.0,
                    "dose_mL": 3.0}],
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1100,
                           "dose_mL": None}],
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5)
    # Pass param_id="magnesium" — guard should pass through
    out = param_advisor._apply_safety_guards(coord, "magnesium", rec)
    assert out.suggested_dose_mL == 3.5  # unchanged


def test_snowstorm_guard_only_blocks_increases():
    """A recommendation to LOWER Ca dose passes through even when
    chemistry is risky — lowering can't trigger snowstorm."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [{"at": "2026-05-08T08:00:00+00:00", "kh": 11.0,
                    "dose_mL": 3.0}],  # high alk, would trigger guard
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1100,
                           "dose_mL": None}],
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=2.5)  # decrease
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    assert out.suggested_dose_mL == 2.5  # unchanged — decrease passes


def test_snowstorm_guard_passes_when_no_kh_snapshots():
    """If there's no alk advisor data yet, guard can't evaluate the
    alkalinity check — passes through (defensive: don't block on
    insufficient data, just on confirmed-bad data)."""
    coord = _CoordWithSnapshots(
        {},
        {
            "kh": [],  # no alk data
            "magnesium": [{"at": "2026-05-08T08:00:00+00:00", "kh": 1320,
                           "dose_mL": None}],
        },
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5)
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    assert out.suggested_dose_mL == 3.5  # passes through


# ---------------------------------------------------------------------------
# 0.5.3 — NO3 + PO4 defaults
# ---------------------------------------------------------------------------
def test_nitrate_defaults_present():
    """NO3 advisor defaults align with research: target 1-10 ppm,
    NEGATIVE potency (remover semantics), 0.5 ppm floor."""
    d = param_advisor.PARAM_DEFAULTS["nitrate"]
    assert d["target_min"] == 1.0
    assert d["target_max"] == 10.0
    assert d["default_eff_per_mL_per_100L"] < 0  # remover
    assert d["floor_value"] == 0.5
    assert d["value_unit"] == "ppm"
    # Sparse-cadence defaults match Ca/Mg
    assert d["window_days"] == 90
    assert d["min_samples"] == 2
    assert d["cooldown_days"] == 30.0


def test_phosphate_defaults_present():
    """PO4 advisor defaults: 0.03-0.10 ppm, NEGATIVE potency,
    0.03 ppm floor (below this triggers dinos)."""
    d = param_advisor.PARAM_DEFAULTS["phosphate"]
    assert d["target_min"] == 0.03
    assert d["target_max"] == 0.10
    assert d["default_eff_per_mL_per_100L"] < 0
    assert d["floor_value"] == 0.03


# ---------------------------------------------------------------------------
# Floor guard (0.5.3) — refuses removal-dose increase when value is
# at/below floor. Defensive against algorithm + user-override
# combinations that would suggest "remove more" when nutrients are
# already too low (dino-outbreak risk).
# ---------------------------------------------------------------------------
def _make_rec_for_remover(
    *, current_dose=3.0, suggested_dose=3.5, median=0.3, reason="lower it",
):
    """Recommendation for a NO3/PO4 advisor: median is the latest
    measured value (e.g. 0.3 ppm NO3 = below floor 0.5)."""
    from reeftanktracker.advisor import Recommendation
    return Recommendation(
        state=suggested_dose,
        current_dose_mL=current_dose,
        suggested_dose_mL=suggested_dose,
        change_mL=suggested_dose - current_dose,
        change_pct=((suggested_dose - current_dose) / current_dose) * 100.0,
        confidence="high",
        reason=reason,
        kh_median=median,
        delta_dkh=0.0,
        target_min=1.0,
        target_max=10.0,
        target_midpoint=5.5,
        observed_slope_dkh_per_day=None,
        observed_dose_median_mL=None,
        spec_efficiency_dkh_per_mL=-0.118,
        samples_used=4,
        window_start=None,
        window_end=None,
        cooldown_until=None,
        last_acknowledged_at=None,
        last_acknowledged_value_mL=None,
        last_demand_change_at=None,
        last_demand_change_reason=None,
        days_since_demand_change=None,
        calibration_warning=False,
        detected_supplement_label=None,
        detected_supplement_profile=None,
        spec_efficiency_source="default",
        empirical_potency_dkh_per_mL=None,
        empirical_to_spec_ratio=None,
        empirical_potency_basis="not yet",
        spec_drift_warning=False,
        last_water_change_at=None,
        last_water_change_percent=None,
        days_since_water_change=None,
        samples_excluded_for_wc=0,
    )


def test_floor_guard_suppresses_no3_increase_when_at_floor():
    """NO3 = 0.3 ppm (below 0.5 floor) + algorithm wants more removal
    → guard holds dose, drops confidence to low."""
    coord = _CoordWithSnapshots({}, {})
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=3.5, median=0.3,
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.suggested_dose_mL == 3.0  # held
    assert out.change_mL == 0.0
    assert out.confidence == "low"
    assert "floor" in out.reason.lower()
    assert "0.30" in out.reason or "0.3" in out.reason


def test_floor_guard_passes_when_above_floor():
    """NO3 = 5.0 ppm (well above 0.5 floor) → guard is no-op,
    original recommendation passes through."""
    coord = _CoordWithSnapshots({}, {})
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=3.5, median=5.0,
        reason="NO3 high — increase removal",
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.suggested_dose_mL == 3.5
    assert out.change_mL == 0.5
    assert "floor" not in out.reason.lower()


def test_floor_guard_only_blocks_increases():
    """Even at-floor, a recommendation to LOWER removal dose passes
    through (lowering removal lets value recover — that's what we
    WANT when at floor)."""
    coord = _CoordWithSnapshots({}, {})
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=2.5,  # decrease
        median=0.3,  # at floor
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.suggested_dose_mL == 2.5  # passes through


def test_floor_guard_phosphate_at_floor():
    """PO4 = 0.02 ppm (below 0.03 floor) → guard fires."""
    coord = _CoordWithSnapshots({}, {})
    rec = _make_rec_for_remover(
        current_dose=2.0, suggested_dose=2.5, median=0.02,
    )
    out = param_advisor._apply_safety_guards(coord, "phosphate", rec)
    assert out.suggested_dose_mL == 2.0
    assert "floor" in out.reason.lower()


def test_floor_guard_does_not_apply_to_calcium():
    """Floor guard is for removers (NO3/PO4) only. Ca rec passes
    through to the snowstorm guard, not the floor guard."""
    coord = _CoordWithSnapshots(
        {}, {"kh": [], "magnesium": []},  # no cross-param data
    )
    rec = _make_rec(current_dose=3.0, suggested_dose=3.5)
    out = param_advisor._apply_safety_guards(coord, "calcium", rec)
    # Ca with no cross-param data → snowstorm guard passes through;
    # floor guard isn't even checked. Original rec returned.
    assert out.suggested_dose_mL == 3.5


# ---------------------------------------------------------------------------
# Redfield-ratio warning (0.5.3) — soft warning, not a hard block.
# Reads NO3 + PO4 medians, flags imbalance outside [50:1, 200:1].
# ---------------------------------------------------------------------------
def test_redfield_warning_fires_when_ratio_low():
    """NO3:PO4 = 30:1 (PO4 too high relative to NO3) → cyano risk
    warning prepended to reason."""
    coord = _CoordWithSnapshots(
        {},
        {
            # 3 ppm NO3 / 0.10 ppm PO4 = 30:1 ratio (low)
            "nitrate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 3.0,
                         "dose_mL": 3.0}],
            "phosphate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 0.10,
                           "dose_mL": 1.0}],
        },
    )
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=2.5, median=3.0,
        reason="NO3 in band — hold",
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.redfield_ratio is not None
    assert 28 < out.redfield_ratio < 32  # ~30:1
    assert out.redfield_warning is True
    assert "redfield" in out.reason.lower()
    assert "cyano" in out.reason.lower()


def test_redfield_warning_fires_when_ratio_high():
    """NO3:PO4 = 250:1 (NO3 too high relative to PO4) → dino risk."""
    coord = _CoordWithSnapshots(
        {},
        {
            # 25 ppm NO3 / 0.10 ppm PO4 = 250:1 ratio (high)
            "nitrate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 25.0,
                         "dose_mL": 3.0}],
            "phosphate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 0.10,
                           "dose_mL": 1.0}],
        },
    )
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=3.5, median=25.0,
        reason="NO3 high — remove more",
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.redfield_warning is True
    assert "dino" in out.reason.lower()


def test_redfield_in_band_no_warning():
    """NO3:PO4 = 100:1 → no warning (canonical NSW ratio)."""
    coord = _CoordWithSnapshots(
        {},
        {
            "nitrate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 5.0,
                         "dose_mL": 3.0}],
            "phosphate": [{"at": "2026-05-08T08:00:00+00:00", "kh": 0.05,
                           "dose_mL": 1.0}],
        },
    )
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=3.5, median=5.0,
        reason="NO3 mid-band",
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    # Above floor → floor guard is a no-op; redfield in band → no
    # prepended warning, but redfield_ratio is still set diagnostically
    assert out.redfield_ratio is not None
    assert 95 < out.redfield_ratio < 105  # ~100:1
    assert out.redfield_warning is False
    assert "redfield" not in out.reason.lower()


def test_redfield_skipped_when_no_snapshots():
    """No NO3 or PO4 snapshots yet → redfield is skipped, ratio stays
    None, no warning. Floor guard still applies if relevant."""
    coord = _CoordWithSnapshots({}, {})
    rec = _make_rec_for_remover(
        current_dose=3.0, suggested_dose=3.5, median=5.0,
    )
    out = param_advisor._apply_safety_guards(coord, "nitrate", rec)
    assert out.redfield_ratio is None
    assert out.redfield_warning is False
