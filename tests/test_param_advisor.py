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
    (see memory: reference_reef_dosing_research.md) AND the manual-
    once-a-week testing cadence assumption (0.5.1+)."""
    ca = param_advisor.PARAM_DEFAULTS["calcium"]
    # Target 420-440 (SPS-friendly per Holmes-Farley)
    assert ca["target_min"] == 420.0
    assert ca["target_max"] == 440.0
    # ±10% step cap, conservative across reef types
    assert ca["step_cap_pct"] == 10.0
    # 21 days — manual once-a-week testing means 3 weekly cycles per
    # cooldown. Holmes-Farley's 5-day rule applies for daily-tested
    # cadence (auto-tester); we tune for the slower manual flow.
    assert ca["cooldown_days"] == 21.0
    # 5 ppm hysteresis (within test-kit noise)
    assert ca["hysteresis"] == 5.0
    # Foundation A: 2 ppm Ca per mL per 100L
    assert ca["default_eff_per_mL_per_100L"] == 2.0
    assert ca["value_unit"] == "ppm"
    # Manual-cadence window: 42 days = ~6 weeks for 4 weekly readings
    # to accumulate (with 1-2 missed weeks tolerated).
    assert ca["window_days"] == 42
    assert ca["min_samples"] == 4
    # Correction spread over 3 weeks → gentler dose changes appropriate
    # for the manual-test feedback loop.
    assert ca["correction_period_days"] == 21.0


def test_param_label_and_unit_propagate_to_advisor_config():
    """`compute_for_param` builds an AdvisorConfig with `param_label` +
    `value_unit` set for the parameter, so the algorithm's reason text
    says "Calcium median X ppm" instead of the alk-default "KH median X dKH"."""
    coord = _StubCoord({"advisor_calcium_enabled": True})
    cfg = param_advisor._build_config(coord, "calcium", spec_eff=2.0)
    assert cfg.param_label == "Calcium"
    assert cfg.value_unit == "ppm"
