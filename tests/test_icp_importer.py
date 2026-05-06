"""Pure tests for the ICP importer.

The parser body is a stub in Phase 1 — these tests exercise the
guard-rail logic (empty/unrecognised input → ParserError), the element
mapping, and the data shapes. Once we have a real Triton showroom URL
and write the deterministic parser, the parser tests get replaced with
HTML-fixture tests that assert the full ParsedReport shape.
"""
from __future__ import annotations

import pytest

from reeftanktracker.icp_importer import ParserError, parse_triton_showroom
from reeftanktracker.triton_elements import (
    TRITON_ELEMENT_TO_PARAM_ID,
    param_id_for_element,
)


# ---------------------------------------------------------------------------
# Parser guard-rails (Phase 1 stub)
# ---------------------------------------------------------------------------
def test_parser_rejects_empty_input():
    with pytest.raises(ParserError, match="empty"):
        parse_triton_showroom("")


def test_parser_rejects_short_input():
    with pytest.raises(ParserError, match="empty|short"):
        parse_triton_showroom("hello")


def test_parser_rejects_non_triton_html():
    """Real-looking HTML but no Triton/ICP marker."""
    html = "<html><body>" + ("<p>some content</p>" * 50) + "</body></html>"
    with pytest.raises(ParserError, match="doesn't look like"):
        parse_triton_showroom(html)


def test_parser_phase1_stub_raises_for_recognised_pages():
    """A page that contains 'triton' but isn't yet parseable raises with
    a clear "stub" message — explicit reminder to inspect a real URL
    before writing parser code."""
    html = "<html><body>" + "Triton ICP report " * 50 + "</body></html>"
    with pytest.raises(ParserError, match="stub|sample"):
        parse_triton_showroom(html)


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
