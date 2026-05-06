"""Tests for parameter declarations.

These mostly validate that the data we hand-rolled is internally
consistent — every parameter has the fields downstream code assumes.
"""
from __future__ import annotations

from reeftanktracker.parameters import (
    ALL_PARAMETERS,
    ICP_ONLY_PARAMETERS,
    ICP_SYMBOL_TO_ID,
    INPUT_PARAMETERS,
    get_parameter,
    get_parameter_by_symbol,
)


def test_all_parameters_have_unique_ids():
    ids = [p["id"] for p in ALL_PARAMETERS]
    assert len(ids) == len(set(ids)), f"duplicate parameter ids: {ids}"


def test_input_parameters_have_required_fields():
    for p in INPUT_PARAMETERS:
        assert "id" in p
        assert "name" in p
        assert "unit" in p
        assert "min" in p
        assert "max" in p
        assert "step" in p
        assert p["min"] < p["max"]


def test_icp_only_flagged_correctly():
    assert all(p.get("icp_only") for p in ICP_ONLY_PARAMETERS)
    assert all(p.get("input_only") for p in INPUT_PARAMETERS)


def test_icp_symbol_lookup_round_trips():
    # Every parameter referenced by ICP_SYMBOL_TO_ID must exist
    for sym, pid in ICP_SYMBOL_TO_ID.items():
        param = get_parameter(pid)
        assert param is not None, f"symbol {sym} maps to unknown parameter {pid}"

    # And get_parameter_by_symbol works
    p = get_parameter_by_symbol("Ca")
    assert p is not None
    assert p["id"] == "calcium"

    p = get_parameter_by_symbol("Sal")
    assert p is not None
    assert p["id"] == "salinity"


def test_unknown_symbol_returns_none():
    assert get_parameter_by_symbol("ZZ") is None
    assert get_parameter("not_a_real_param") is None


def test_temperature_has_celsius_unit():
    p = get_parameter("temperature")
    assert p is not None
    assert p["unit"] == "°C"


def test_kh_default_target_range_sensible():
    p = get_parameter("kh")
    assert p is not None
    lo = p["default_target_min"]
    hi = p["default_target_max"]
    assert 6 < lo < hi < 12, "KH defaults should be a sane reef range"


def test_phosphate_precision_supports_micro_values():
    p = get_parameter("phosphate")
    assert p is not None
    # Hanna ULR resolves to 3 decimal ppm
    assert p["precision"] >= 3
    assert p["step"] <= 0.001
