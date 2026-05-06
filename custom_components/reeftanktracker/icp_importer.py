"""ICP test ingestion from a Triton public showroom URL.

Phase 1: paste a URL → fetch the page → parse to a structured report →
persist via the existing `coordinator.async_record_icp_test` and
`coordinator.async_record_reading` paths. No write back to Triton.

The parser is deterministic (no LLM fallback). When the page structure
changes, we drop a debug bundle to
`<config>/.storage/icpimport_debug/<timestamp>/` containing the raw HTML
plus a parse trace, and raise a `ParserError` with the bundle path so
the failure surfaces in the UI as a real, actionable error message
rather than a silent no-op.

This module is HA-aware (uses `HomeAssistant`) but the parser function
itself is pure — `parse_triton_showroom(html) -> ParsedReport` —
exercised by `tests/test_icp_importer.py` with HTML fixtures.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ReefDataCoordinator
from .triton_elements import param_id_for_element


_LOGGER = logging.getLogger(__name__)

DEBUG_BUNDLE_DIR = "icpimport_debug"
USER_AGENT = "reeftanktracker (+https://github.com/jordanduffybd/reeftanktracker)"


# ---------------------------------------------------------------------------
# Parsed-report shape
# ---------------------------------------------------------------------------
@dataclass
class ParsedElement:
    """One element row from a Triton report."""
    symbol: str            # "Ca", "Mg", "PO4", ...
    name: str | None       # "Calcium", "Magnesium" — display name from page
    analysis: float        # the measured value
    unit: str | None       # "mg/L", "ppm", "dKH", ...
    setpoint: str | None   # display setpoint range, e.g. "420 - 440"
    warning: str | None    # "OK", "Low", "High", or None
    group: str | None      # element group as Triton presents it


@dataclass
class ParsedReport:
    """Full parsed Triton report.

    `test_id` is what we dedupe on in `async_record_icp_test`. Must be
    stable for a given physical test. Triton's test reference (e.g.
    `B-KJAZM8`) is the natural choice; if it isn't recoverable from the
    page we fall back to a hash of url + sample_date and warn.
    """
    test_id: str
    sample_date: str       # ISO date, "YYYY-MM-DD"
    lab_received_date: str | None
    elements: list[ParsedElement] = field(default_factory=list)
    raw_url: str | None = None
    parser_version: str = "phase1-stub"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ParserError(Exception):
    """Parser couldn't make sense of the page. Carries a debug-bundle
    path so the user sees an actionable error in the service-call UI."""
    def __init__(self, message: str, debug_path: Path | None = None):
        super().__init__(message)
        self.debug_path = debug_path


# ---------------------------------------------------------------------------
# Pure parser — exercised by tests with HTML fixtures
# ---------------------------------------------------------------------------
def parse_triton_showroom(html: str) -> ParsedReport:
    """Parse a Triton public showroom HTML page into a ParsedReport.

    Phase 1: this is a stub. Returning here without raising would mean
    we successfully recognised the page; raising means we couldn't.
    Real selectors land once we have a sample URL from Jordan to inspect.

    Why not implement speculatively: the page might be JS-rendered,
    might be served as JSON-LD, might use class names that look stable
    but aren't. Two minutes of reading the actual HTML beats two hours
    of fixing wrong assumptions (CLAUDE.md rule #3).
    """
    # Quick sanity heuristic to avoid blowing up on entirely wrong pages.
    if not isinstance(html, str) or len(html) < 100:
        raise ParserError(
            "Triton page response is empty or impossibly short — "
            "not a valid showroom URL."
        )
    if "triton" not in html.lower() and "icp" not in html.lower():
        raise ParserError(
            "Page doesn't look like a Triton ICP report (no 'triton' "
            "or 'icp' marker in the HTML). Check the URL."
        )
    raise ParserError(
        "Phase 1 parser is a stub. The structure of Triton's public "
        "showroom HTML hasn't been inspected yet — please share a real "
        "showroom URL so the parser can be written against it."
    )


# ---------------------------------------------------------------------------
# Debug bundle — dropped on parser failure
# ---------------------------------------------------------------------------
def _write_debug_bundle(
    hass: HomeAssistant, url: str, html: str, error: Exception,
) -> Path:
    """Persist HTML + parse trace under `<config>/.storage/icpimport_debug/`.

    Best-effort: a write failure here doesn't suppress the original
    parser error — we log and return a placeholder path.
    """
    storage = Path(hass.config.path(".storage")) / DEBUG_BUNDLE_DIR
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = storage / timestamp
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "url.txt").write_text(url, encoding="utf-8")
        (bundle / "page.html").write_text(html, encoding="utf-8")
        (bundle / "error.txt").write_text(
            f"{type(error).__name__}: {error}\n", encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not write ICP debug bundle: %s", exc)
    return bundle


# ---------------------------------------------------------------------------
# Fetch + persist
# ---------------------------------------------------------------------------
async def _fetch_url(url: str) -> str:
    """GET the URL with a custom User-Agent. Raises on non-2xx."""
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.text()


async def _record_report(
    coordinator: ReefDataCoordinator, report: ParsedReport,
) -> tuple[int, list[str]]:
    """Push a ParsedReport into the coordinator.

    Records each known element as a `record_reading` with `source=icp`,
    then stashes the full test_record. Returns (count_imported, skipped_symbols).
    """
    sample_at = f"{report.sample_date}T08:00:00+00:00"
    skipped: list[str] = []
    imported = 0
    elements_blob: dict[str, dict[str, Any]] = {}

    for el in report.elements:
        param_id = param_id_for_element(el.symbol)
        if not param_id:
            skipped.append(el.symbol)
            continue
        await coordinator.async_record_reading(
            parameter=param_id,
            value=float(el.analysis),
            unit=el.unit,
            method="Triton ICP-OES",
            source="icp",
            sample_taken_at=sample_at,
            test_id=report.test_id,
        )
        imported += 1
        elements_blob[el.symbol] = {
            "value": el.analysis,
            "unit": el.unit,
            "name": el.name,
            "setpoint": el.setpoint,
            "warning": el.warning,
            "group": el.group,
        }

    test_record = {
        "test_id": report.test_id,
        "sample_date": report.sample_date,
        "lab_received_date": report.lab_received_date,
        "imported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "triton-url",
        "url": report.raw_url,
        "elements": elements_blob,
    }
    await coordinator.async_record_icp_test(test_record)

    return imported, skipped


async def import_triton_url(
    hass: HomeAssistant,
    coordinator: ReefDataCoordinator,
    url: str,
) -> dict[str, Any]:
    """Top-level orchestration. Fetch → parse → record.

    Returns a summary dict (suitable to log + surface to the user via
    the service response):
        {
          "test_id": "...",
          "sample_date": "...",
          "imported": 28,
          "skipped_symbols": ["NewElement"],
        }

    Raises ParserError on parse failure with a debug-bundle path
    attached so the failure is loud + actionable.
    """
    if not url or not isinstance(url, str):
        raise ValueError("import_triton_url requires a non-empty url string")

    _LOGGER.info("Fetching Triton showroom URL: %s", url)
    try:
        html = await _fetch_url(url)
    except aiohttp.ClientError as exc:
        raise ParserError(
            f"Could not fetch {url}: {exc}", debug_path=None,
        ) from exc

    try:
        report = parse_triton_showroom(html)
        report.raw_url = url
    except ParserError as exc:
        bundle = _write_debug_bundle(hass, url, html, exc)
        # Re-raise with the bundle path attached so the surface is
        # "open this folder for the failed page" instead of a generic
        # "parser failed" message.
        raise ParserError(
            f"{exc} Debug bundle written to: {bundle}",
            debug_path=bundle,
        ) from None

    imported, skipped = await _record_report(coordinator, report)
    summary = {
        "test_id": report.test_id,
        "sample_date": report.sample_date,
        "imported": imported,
        "skipped_symbols": skipped,
    }
    _LOGGER.info("Triton import complete: %s", summary)
    return summary
