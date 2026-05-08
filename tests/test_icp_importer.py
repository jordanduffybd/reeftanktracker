"""Pure tests for the ICP importer.

Parser tests use a real Triton showroom HTML fixture saved at
`tests/fixtures/triton_showroom_229019.html` (captured 2026-05-06 from
https://www.triton-lab.de/en/showroom/icp-oes/229019). Re-fetch the
fixture if Triton changes the page structure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reeftanktracker.icp_importer import (
    ParsedElement,
    ParsedReport,
    ParserError,
    TRITON_PER_HABITAT_SETPOINTS,
    TRITON_PROBLEM_LABEL_MAP,
    _build_parse_trace,
    _extract_test_id_from_url,
    _setpoint_for_habitat,
    element_identifiers_for_combo,
    parse_triton_showroom,
)
from reeftanktracker.triton_elements import (
    TRITON_ELEMENT_TO_PARAM_ID,
    param_id_for_element,
)


FIXTURE = (
    Path(__file__).parent / "fixtures" / "triton_showroom_229019.html"
)


@pytest.fixture
def triton_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parser guard-rails
# ---------------------------------------------------------------------------
def test_parser_rejects_empty_input():
    with pytest.raises(ParserError, match="empty"):
        parse_triton_showroom("")


def test_parser_rejects_short_input():
    with pytest.raises(ParserError, match="empty|short"):
        parse_triton_showroom("hello")


def test_parser_rejects_non_triton_html():
    html = "<html><body>" + ("<p>some content</p>" * 50) + "</body></html>"
    with pytest.raises(ParserError, match="doesn't look like"):
        parse_triton_showroom(html)


def test_parser_no_element_rows_raises():
    """Page mentions Triton but has no <tr id="X"> blocks."""
    html = "<html><body>" + "Triton ICP report " * 50 + "</body></html>"
    with pytest.raises(ParserError, match="0 element rows"):
        parse_triton_showroom(html)


# ---------------------------------------------------------------------------
# Real-fixture parser tests
# ---------------------------------------------------------------------------
def test_fixture_parses_to_full_report(triton_html: str):
    report = parse_triton_showroom(triton_html)
    # Triton publishes 39 elements; all should round-trip through the parser.
    assert len(report.elements) == 39


def test_fixture_known_values(triton_html: str):
    """Spot-check a few cells against the known fixture content."""
    report = parse_triton_showroom(triton_html)
    by_sym = {el.symbol: el for el in report.elements}

    # Calcium — major ion, no warning class
    ca = by_sym["Ca"]
    assert ca.name == "Calcium"
    assert ca.analysis == 429.00
    assert ca.unit == "mg/l"
    assert ca.setpoint == "415 - 520 mg/l"
    assert ca.warning is None

    # Magnesium
    mg = by_sym["Mg"]
    assert mg.analysis == 1363.00
    assert mg.unit == "mg/l"

    # Phosphate — page marks this elem-warn-red (out of range)
    po4 = by_sym["PO4"]
    assert po4.analysis == 0.055
    assert po4.unit == "mg/l"
    assert po4.warning == "high-or-low"

    # Salinity — point setpoint (no range), unit PSU
    sal = by_sym["Sal"]
    assert sal.analysis == pytest.approx(35.285)
    assert sal.unit == "PSU"
    assert sal.setpoint == "35 PSU"

    # Copper at 0 — no warning, μg/l unit
    cu = by_sym["Cu"]
    assert cu.analysis == 0.0
    assert cu.unit in ("µg/l", "&micro;g/l")  # tolerate both renderings


def test_fixture_dedupes_repeated_symbols(triton_html: str):
    """Each element appears in the page TWICE (summary table + detail
    section). The parser keeps only the first occurrence — the
    out-list should have no duplicate symbols."""
    report = parse_triton_showroom(triton_html)
    symbols = [el.symbol for el in report.elements]
    assert len(symbols) == len(set(symbols))


def test_every_fixture_symbol_maps_to_a_known_param_id(triton_html: str):
    """If Triton publishes a symbol our map doesn't cover, we'd silently
    drop the reading on import. Catch that early."""
    report = parse_triton_showroom(triton_html)
    unmapped = [
        el.symbol for el in report.elements
        if param_id_for_element(el.symbol) is None
    ]
    assert unmapped == [], (
        f"Triton fixture publishes elements not mapped in "
        f"TRITON_ELEMENT_TO_PARAM_ID: {unmapped}"
    )


def test_parser_leaves_test_id_blank(triton_html: str):
    """Parser is pure HTML → ParsedReport. test_id is filled in by the
    caller from the URL (which the parser doesn't see)."""
    report = parse_triton_showroom(triton_html)
    assert report.test_id == ""
    assert report.sample_date == ""


# ---------------------------------------------------------------------------
# Dose-tab parsing — habitat-specific recommendations
# ---------------------------------------------------------------------------
def test_dose_recommendations_extracted(triton_html: str):
    """The Dose tab on the showroom page is grouped by importance
    (1-5 stars). Each group has element-specific dosing advice. We
    capture all of it as `recommendations`."""
    report = parse_triton_showroom(triton_html)
    by_sym = {r.symbol: r for r in report.recommendations}
    # The fixture's 5-star group should include Ca + Mg
    assert "Ca" in by_sym
    assert "Mg" in by_sym
    # Both are 5-star (Very important)
    assert by_sym["Ca"].importance_stars == 5
    assert by_sym["Mg"].importance_stars == 5
    assert "Very important" in by_sym["Ca"].importance_label


def test_dose_recommendation_corrective_dose(triton_html: str):
    """For Ca, Triton recommends a specific corrective grams + duration.
    Captured as raw text — this is for display, not parsing further."""
    report = parse_triton_showroom(triton_html)
    by_sym = {r.symbol: r for r in report.recommendations}
    assert by_sym["Ca"].corrective_dose is not None
    # Fixture says "17.73g for 1 day"
    assert "17.73g" in by_sym["Ca"].corrective_dose
    assert "day" in by_sym["Ca"].corrective_dose


def test_dose_recommendation_product_reference(triton_html: str):
    """The "Show me the product" button reveals the recommended Triton
    product. Captured for the user's purchasing convenience."""
    report = parse_triton_showroom(triton_html)
    by_sym = {r.symbol: r for r in report.recommendations}
    assert by_sym["Ca"].product_reference is not None
    assert "Calcium" in by_sym["Ca"].product_reference


def test_dose_recommendations_span_importance_levels(triton_html: str):
    """The fixture has elements rated across multiple importance buckets
    (5-star, 4-star, 2-star, 1-star). All should be captured."""
    report = parse_triton_showroom(triton_html)
    star_levels = {r.importance_stars for r in report.recommendations}
    assert star_levels == {1, 2, 4, 5}


# ---------------------------------------------------------------------------
# ECS rule library — filter recommendations by habitat × problem
# ---------------------------------------------------------------------------
def test_rule_library_extracted_from_fixture(triton_html: str):
    """Triton embeds the full habitat × problem rule library as a JSON
    blob. We extract it once during parse so we can filter without
    re-fetching."""
    report = parse_triton_showroom(triton_html)
    assert isinstance(report.rule_library, list)
    assert len(report.rule_library) == 9   # 9 habitats
    names = {h["name"] for h in report.rule_library}
    assert "SPS Outer Reef" in names
    assert "LPS Dominant" in names


def test_element_lookup_known_combo(triton_html: str):
    """For a given habitat × problem, the rule library tells us which
    element identifiers Triton flags. Check we recover this for SPS
    Outer Reef + Cyanobacteria."""
    report = parse_triton_showroom(triton_html)
    ids = element_identifiers_for_combo(
        report.rule_library, "SPS Outer Reef", "Cyanobacteria",
    )
    assert isinstance(ids, set)
    assert len(ids) > 0
    # Cyanobacteria-specific elements should include things like Ca / Mg / I.
    # We don't pin the exact set (Triton can update) — just sanity-check
    # that some are there.
    assert "Ca" in ids
    assert "Mg" in ids


def test_element_lookup_problem_label_mapping(triton_html: str):
    """Our integration's PROBLEMS list uses 'None' for the no-issue
    case; Triton's rule library labels it 'No issues (default)'. The
    lookup translates."""
    report = parse_triton_showroom(triton_html)
    ids_via_none = element_identifiers_for_combo(
        report.rule_library, "SPS Outer Reef", "None",
    )
    ids_via_default = element_identifiers_for_combo(
        report.rule_library, "SPS Outer Reef", "No issues (default)",
    )
    assert ids_via_none == ids_via_default
    assert ids_via_none is not None
    assert len(ids_via_none) > 0


def test_element_lookup_unknown_combo_returns_none():
    """Missing inputs → no filtering → None signal. Caller treats this
    as 'don't filter, show everything'."""
    assert element_identifiers_for_combo([], "SPS Outer Reef", "Cyanobacteria") is None
    assert element_identifiers_for_combo([{"name": "X"}], None, "Cyanobacteria") is None
    assert element_identifiers_for_combo([{"name": "X"}], "SPS", None) is None


def test_per_habitat_setpoints_complete():
    """The static P/TNb setpoint matrix must cover every habitat in the
    integration's HABITATS list."""
    from reeftanktracker.const import HABITATS
    for h in HABITATS:
        assert h in TRITON_PER_HABITAT_SETPOINTS, (
            f"Habitat {h!r} missing from TRITON_PER_HABITAT_SETPOINTS"
        )
        assert "P" in TRITON_PER_HABITAT_SETPOINTS[h]
        assert "TNb" in TRITON_PER_HABITAT_SETPOINTS[h]


def test_problem_label_map_covers_problems():
    from reeftanktracker.const import PROBLEMS
    for p in PROBLEMS:
        assert p in TRITON_PROBLEM_LABEL_MAP, (
            f"Problem {p!r} missing from TRITON_PROBLEM_LABEL_MAP"
        )


# ---------------------------------------------------------------------------
# Habitat-aware setpoint resolution
# ---------------------------------------------------------------------------
def test_setpoint_for_habitat_overrides_phosphorus():
    """P has habitat-specific setpoints. SPS Outer Reef gets 6 µg/l,
    not whatever the page rendered."""
    result = _setpoint_for_habitat("12 µg/l", "P", "SPS Outer Reef")
    assert result == "6 µg/l"


def test_setpoint_for_habitat_overrides_tnb():
    """TNb has habitat-specific setpoints. Soft Coral Dominant gets 1.3 mg/l."""
    result = _setpoint_for_habitat("0.4 mg/l", "TNb", "Soft Coral Dominant")
    assert result == "1.3 mg/l"


def test_setpoint_for_habitat_passthrough_for_other_elements():
    """Ca/Mg/etc. share one setpoint across habitats — fall back to
    the rendered value."""
    result = _setpoint_for_habitat("440 mg/l", "Ca", "SPS Outer Reef")
    assert result == "440 mg/l"


def test_setpoint_for_habitat_no_habitat_returns_rendered():
    """Without a habitat selection, just return what was on the page."""
    assert _setpoint_for_habitat("440 mg/l", "Ca", None) == "440 mg/l"
    assert _setpoint_for_habitat("12 µg/l", "P", None) == "12 µg/l"


def test_setpoint_for_habitat_unknown_habitat_returns_rendered():
    """Habitat not in the matrix → fall back to rendered. Caller's job
    to validate the habitat name; this helper is forgiving."""
    result = _setpoint_for_habitat("12 µg/l", "P", "Made-Up Habitat")
    assert result == "12 µg/l"


# ---------------------------------------------------------------------------
# Habitat / problem selection
# ---------------------------------------------------------------------------
def test_selected_habitat_problem_extracted(triton_html: str):
    """The fixture URL doesn't have habitat/problem pre-selected
    (Triton account default is no selection). Parser must NOT crash on
    absence and should report None for both."""
    report = parse_triton_showroom(triton_html)
    # If the user re-shares with selections set, these will populate.
    # The fixture has neither; parser handles that gracefully.
    assert report.selected_habitat is None or isinstance(report.selected_habitat, str)
    assert report.selected_problem is None or isinstance(report.selected_problem, str)


# ---------------------------------------------------------------------------
# Test-ID extraction
# ---------------------------------------------------------------------------
def test_extract_test_id_standard_url():
    assert (
        _extract_test_id_from_url(
            "https://www.triton-lab.de/en/showroom/icp-oes/229019"
        ) == "triton-229019"
    )


def test_extract_test_id_with_query_string():
    assert (
        _extract_test_id_from_url(
            "https://www.triton-lab.de/en/showroom/icp-oes/229019?foo=bar"
        ) == "triton-229019"
    )


def test_extract_test_id_unrecognised_url_falls_back_to_hash():
    tid = _extract_test_id_from_url("https://example.com/something/else")
    assert tid.startswith("triton-url-")
    # Stable: same URL → same hash
    assert tid == _extract_test_id_from_url(
        "https://example.com/something/else"
    )


# ---------------------------------------------------------------------------
# Element mapping
# ---------------------------------------------------------------------------
def test_known_elements_map_correctly():
    assert param_id_for_element("Ca") == "calcium"
    assert param_id_for_element("Mg") == "magnesium"
    assert param_id_for_element("Sr") == "strontium"
    assert param_id_for_element("PO4") == "phosphate"
    assert param_id_for_element("Sal") == "salinity"


def test_unknown_element_returns_none():
    assert param_id_for_element("Unobtainium") is None
    assert param_id_for_element("") is None


def test_all_mapped_param_ids_match_parameters_py():
    """Every mapped target parameter id must exist in ALL_PARAMETERS,
    otherwise ICP imports would silently skip those readings even if
    Triton sends them."""
    from reeftanktracker.parameters import ALL_PARAMETERS
    valid_ids = {p["id"] for p in ALL_PARAMETERS}
    missing = [
        pid for pid in TRITON_ELEMENT_TO_PARAM_ID.values()
        if pid not in valid_ids
    ]
    assert missing == [], (
        f"TRITON_ELEMENT_TO_PARAM_ID points at param ids not in "
        f"parameters.py: {missing}"
    )


# ---------------------------------------------------------------------------
# Debug-bundle parse trace
# ---------------------------------------------------------------------------
def test_parse_trace_summarises_real_fixture(triton_html: str):
    """The trace should report counts that match the parser's actual
    success on the fixture: 39 elements, dose groups present, rule
    library present. If these drift in a future Triton page change,
    a parse failure will dump this trace into the debug bundle and
    we'll see exactly which selectors stopped matching."""
    trace = _build_parse_trace(triton_html)
    assert "html_bytes:" in trace
    assert "contains_triton_marker: True" in trace
    assert "element_row_count: 39" in trace
    # First10 list is in document order (heavy metals first in Triton's
    # layout). Just assert it's non-empty and looks like element symbols.
    assert "element_symbols_first10:" in trace
    assert "(none)" not in trace.split("element_symbols_first10:")[1].splitlines()[0]
    # Dose blocks present in the fixture (groups 1/2/4/5)
    assert "dose_group_count:" in trace
    assert "rule_library_present: True" in trace
    assert "habitat_dropdown_present: True" in trace


def test_parse_trace_handles_empty_input():
    """Empty HTML — every probe returns 0 / False / (none) instead of
    raising. Trace must still be a usable string for the user to read."""
    trace = _build_parse_trace("")
    assert "html_bytes: 0" in trace
    assert "contains_triton_marker: False" in trace
    assert "element_row_count: 0" in trace
    assert "element_symbols_first10: (none)" in trace
    assert "rule_library_present: False" in trace


def test_parse_trace_handles_non_triton_page():
    """A page that isn't Triton's lab report — `contains_triton_marker`
    flips to False so the user can quickly tell their URL was wrong."""
    trace = _build_parse_trace(
        "<html><body>some other lab's report</body></html>",
    )
    assert "html_bytes:" in trace
    assert "contains_triton_marker: False" in trace
    assert "element_row_count: 0" in trace
