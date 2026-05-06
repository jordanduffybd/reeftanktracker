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
from html import unescape
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

# --- Triton showroom HTML structure (inspected 2026-05-06 against
# https://www.triton-lab.de/en/showroom/icp-oes/229019) ---
#
# Each measured element is a <tr id="<symbol>"> block with five <td>
# cells in order:
#   0. periodic-table icon (skipped — same data as the row id)
#   1. display name, e.g. "Calcium"
#   2. analysis, e.g. "429.00 mg/l" or "0.055 mg/l" with a
#      `class="elem-warn-red"` (or `-yellow`) when out of range
#   3. setpoint, e.g. "415 - 520 mg/l" (range) or "35 PSU" (point)
#   4. visual status bar (skipped — same data as the warning class)
#
# Sample date is NOT in the public showroom HTML; we accept it as an
# optional service arg and default to "today" with a logged warning.
# Test ID is the numeric URL segment (e.g. 229019).

_ROW_RE = re.compile(
    r'<tr id="(?P<sym>[A-Za-z0-9]+)">(?P<body>.*?)</tr>',
    re.DOTALL,
)
_TD_RE = re.compile(
    r'<td(?P<attrs>[^>]*)>(?P<content>.*?)</td>',
    re.DOTALL,
)
_CLASS_RE = re.compile(r'class="([^"]*)"')
_VALUE_UNIT_RE = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>\S.*?)?\s*$"
)
_TEST_ID_FROM_URL_RE = re.compile(r"/icp-oes/(\d+)")

# Dose-tab parsing. Each element rated for dosing is wrapped in
#   <div class="dosage-block dosage-block-Ca"> ... </div>
# inside a parent group:
#   <div class="dosage-block-group dosage-block-group-N"> <h3>label</h3> ... </div>
# where N is the importance rating (1-5; 5 is the most important).
_DOSE_GROUP_RE = re.compile(
    r'<div class="dosage-block-group dosage-block-group-(?P<stars>\d+)">'
    r'(?P<body>.*?)'
    r'(?=<div class="dosage-block-group |</section>)',
    re.DOTALL,
)
_DOSE_GROUP_TITLE_RE = re.compile(r"<h3>([^<]+)</h3>")
_DOSE_BLOCK_RE = re.compile(
    # Match each dose block from its opening tag to the next sibling
    # block's opening tag (or end of the enclosing group body). Using
    # a lookahead avoids the depth-counting we'd need to balance the
    # nested <div class="advice-item ..."> children.
    r'<div class="dosage-block dosage-block-(?P<sym>\w+)">(?P<body>.*?)'
    r'(?=<div class="dosage-block dosage-block-|\Z)',
    re.DOTALL,
)
# Inside a dose block: detection-style paragraph in advice-detection,
# corrective-dose in advice-measures, product reference in onclick handler.
_DOSE_DETECTION_RE = re.compile(
    r'<div class="advice-item advice-detection">(?P<body>.*?)</div>',
    re.DOTALL,
)
_DOSE_MEASURE_RE = re.compile(
    r'<div class="advice-item advice-measures">(?P<body>.*?)</div>',
    re.DOTALL,
)
# Stop at sentence-end (period followed by space or `<`) rather than
# any period — values like "17.73g for 1 day" have an internal decimal
# that an "any period" stop truncates incorrectly.
_DOSE_AMOUNT_RE = re.compile(r"corrective dosage of\s+(.+?)(?=\.\s|\.<|<)")
_DOSE_PRODUCT_RE = re.compile(
    r"window\.t43857093847509387\(\s*['\"]([^'\"]+)['\"]"
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Selected habitat / problem in the dropdowns
_SELECTED_OPTION_RE = re.compile(
    r'<option value="(?P<value>[^"]+)"\s+selected="?selected"?[^>]*>'
    r'(?P<text>[^<]+)</option>'
)
_DROPDOWN_HABITAT_RE = re.compile(
    r'<select id="dropdown_habitat">(.*?)</select>', re.DOTALL,
)
_DROPDOWN_PROBLEM_RE = re.compile(
    r'<select id="dropdown_problem">(.*?)</select>', re.DOTALL,
)


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
class DoseRecommendation:
    """A per-element dosing recommendation from Triton's Dose tab.

    Triton's recommendation depends on the user's chosen habitat AND
    problem at share time. Re-share with different selections to get
    different recommendations.
    """
    symbol: str                # element identifier ("Ca", "Mg", ...)
    importance_stars: int      # 1-5 (5 = "Very important", 1 = "Advanced fine-tuning")
    importance_label: str      # the heading: "Very important for your aquarium" etc.
    advice_text: str | None    # detection-style advice paragraph
    corrective_dose: str | None  # raw text e.g. "17.73g for 1 day"
    product_reference: str | None  # e.g. "Triton Reagents Calcium 1000g"


@dataclass
class ParsedReport:
    """Full parsed Triton report.

    `test_id` is what we dedupe on in `async_record_icp_test`. Must be
    stable for a given physical test. We use `triton-<numeric-url-id>`.

    `selected_habitat` / `selected_problem` reflect the user's choices
    when generating the showroom URL. Different choices change the
    rendered dose recommendations — re-share with different selections
    if you want a different view.
    """
    test_id: str
    sample_date: str       # ISO date, "YYYY-MM-DD"
    lab_received_date: str | None
    elements: list[ParsedElement] = field(default_factory=list)
    recommendations: list[DoseRecommendation] = field(default_factory=list)
    selected_habitat: str | None = None
    selected_problem: str | None = None
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
def _strip_html_to_text(html_fragment: str) -> str:
    """Strip tags and collapse whitespace for human-readable advice text."""
    text = _TAG_STRIP_RE.sub(" ", html_fragment)
    text = unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _parse_dose_recommendations(html: str) -> list[DoseRecommendation]:
    """Extract per-element dose recommendations from the Dose tab.

    Each importance group (1-5 stars) wraps a list of element blocks.
    The 5-star group is "Very important for your aquarium" and contains
    the elements Triton most strongly recommends dosing.
    """
    recs: list[DoseRecommendation] = []
    for group in _DOSE_GROUP_RE.finditer(html):
        try:
            stars = int(group.group("stars"))
        except (TypeError, ValueError):
            continue
        body = group.group("body")
        title_m = _DOSE_GROUP_TITLE_RE.search(body)
        title = (title_m.group(1) if title_m else "").strip()
        for block in _DOSE_BLOCK_RE.finditer(body):
            sym = block.group("sym")
            block_body = block.group("body")

            advice = None
            det_m = _DOSE_DETECTION_RE.search(block_body)
            if det_m:
                advice = _strip_html_to_text(det_m.group("body"))
                # Strip the leading <i class="..."> icon-only text remnants
                advice = advice.lstrip()

            corrective = None
            meas_m = _DOSE_MEASURE_RE.search(block_body)
            if meas_m:
                amt_m = _DOSE_AMOUNT_RE.search(meas_m.group("body"))
                if amt_m:
                    corrective = amt_m.group(1).strip()

            product = None
            prod_m = _DOSE_PRODUCT_RE.search(block_body)
            if prod_m:
                product = prod_m.group(1).strip()

            recs.append(DoseRecommendation(
                symbol=sym,
                importance_stars=stars,
                importance_label=title,
                advice_text=advice,
                corrective_dose=corrective,
                product_reference=product,
            ))
    return recs


def _parse_selected_habitat_problem(
    html: str,
) -> tuple[str | None, str | None]:
    """Find the habitat + problem the user chose at share time. Either
    may be None if the user hadn't picked them in their Triton account.
    """
    habitat = problem = None
    h_m = _DROPDOWN_HABITAT_RE.search(html)
    if h_m:
        sel = _SELECTED_OPTION_RE.search(h_m.group(1))
        if sel:
            habitat = sel.group("text").strip()
    p_m = _DROPDOWN_PROBLEM_RE.search(html)
    if p_m:
        sel = _SELECTED_OPTION_RE.search(p_m.group(1))
        if sel:
            problem = sel.group("text").strip()
    return habitat, problem


def parse_triton_showroom(html: str) -> ParsedReport:
    """Parse a Triton public showroom HTML page into a ParsedReport.

    Returns a ParsedReport with `test_id` and `sample_date` left as
    empty strings — the caller (`import_triton_url`) populates those
    from the URL and service-call args. The parser only needs the HTML.

    Raises `ParserError` if the page doesn't look like a Triton report
    or if zero element rows are found. The caller is responsible for
    capturing the offending HTML in a debug bundle.
    """
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

    elements: list[ParsedElement] = []
    seen_symbols: set[str] = set()

    for row in _ROW_RE.finditer(html):
        sym = row.group("sym")
        # The same element appears twice in the page (summary table +
        # per-element detail section); keep only the first.
        if sym in seen_symbols:
            continue

        body = row.group("body")
        cells = list(_TD_RE.finditer(body))
        if len(cells) < 4:
            # The detail-section <h1>...</h1>... blocks have a few <td>s
            # but in a different shape; only the summary-table rows
            # have ≥ 4 cells in the order we expect.
            continue

        # Cell 1: display name (e.g. "Calcium")
        name = unescape(cells[1].group("content")).strip() or None

        # Cell 2: analysis with optional warning class
        analysis_attrs = cells[2].group("attrs") or ""
        analysis_text = unescape(cells[2].group("content")).strip()
        m = _VALUE_UNIT_RE.match(analysis_text)
        if not m:
            # Some elements show no analysis (placeholder rows). Skip.
            continue
        try:
            value = float(m.group("value"))
        except (TypeError, ValueError):
            continue
        unit = (m.group("unit") or "").strip() or None

        # The optional class attribute encodes the warning level:
        #   elem-warn-red    — outside acceptable range
        #   elem-warn-yellow — borderline
        # Absence = OK / in range.
        warning: str | None = None
        cls_match = _CLASS_RE.search(analysis_attrs)
        if cls_match:
            cls = cls_match.group(1)
            if "elem-warn-red" in cls:
                warning = "high-or-low"
            elif "elem-warn-yellow" in cls:
                warning = "borderline"

        # Cell 3: setpoint as a free-form string (e.g. "415 - 520 mg/l").
        # We keep it raw rather than parsing — it's diagnostic, and
        # the parametric form differs across elements (range vs. point).
        setpoint = unescape(cells[3].group("content")).strip() or None

        elements.append(ParsedElement(
            symbol=sym,
            name=name,
            analysis=value,
            unit=unit,
            setpoint=setpoint,
            warning=warning,
            group=None,  # Triton groups elements visually via <table>
                          # boundaries; not extracted in v1.
        ))
        seen_symbols.add(sym)

    if not elements:
        raise ParserError(
            "Found 0 element rows in the page — Triton's HTML structure "
            "may have changed since this parser was written."
        )

    # Dose recommendations from the Dose tab + selected habitat/problem.
    # Best-effort: a missing Dose tab doesn't fail the parse; the
    # report just lacks recommendations.
    recommendations = _parse_dose_recommendations(html)
    selected_habitat, selected_problem = _parse_selected_habitat_problem(html)

    return ParsedReport(
        test_id="",       # filled in by import_triton_url from the URL
        sample_date="",   # filled in by import_triton_url from service arg
        lab_received_date=None,
        elements=elements,
        recommendations=recommendations,
        selected_habitat=selected_habitat,
        selected_problem=selected_problem,
        parser_version="phase1-2026-05-06",
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

    # Persist habitat-specific dosing recommendations alongside the
    # element analyses. These are what change when the user picks a
    # different habitat or problem on Triton's site — so they're
    # snapshot-tied to whichever combo was selected at share time.
    recommendations_blob = [
        {
            "symbol": r.symbol,
            "importance_stars": r.importance_stars,
            "importance_label": r.importance_label,
            "advice_text": r.advice_text,
            "corrective_dose": r.corrective_dose,
            "product_reference": r.product_reference,
        }
        for r in report.recommendations
    ]

    test_record = {
        "test_id": report.test_id,
        "sample_date": report.sample_date,
        "lab_received_date": report.lab_received_date,
        "imported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "triton-url",
        "url": report.raw_url,
        "selected_habitat": report.selected_habitat,
        "selected_problem": report.selected_problem,
        "elements": elements_blob,
        "recommendations": recommendations_blob,
    }
    await coordinator.async_record_icp_test(test_record)

    return imported, skipped


def _extract_test_id_from_url(url: str) -> str:
    """Build a stable, human-recognisable test_id from a Triton URL.

    Triton's public showroom URL is `/icp-oes/<NUMERIC_ID>` and the
    numeric ID is what we want to dedupe on. Prefix with `triton-` so
    `coordinator._data["icp_tests"][...]["test_id"]` is greppable in
    storage and in HA logs.
    """
    m = _TEST_ID_FROM_URL_RE.search(url)
    if m:
        return f"triton-{m.group(1)}"
    # Fallback: hash the URL for a stable id when the path shape
    # doesn't match our expectation. Better than empty/unstable.
    import hashlib
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"triton-url-{digest}"


async def import_triton_url(
    hass: HomeAssistant,
    coordinator: ReefDataCoordinator,
    url: str,
    sample_date: str | None = None,
) -> dict[str, Any]:
    """Top-level orchestration. Fetch → parse → record.

    `sample_date` (ISO `YYYY-MM-DD`) is optional. If omitted, defaults
    to today (UTC date) with a logged warning — Triton's public
    showroom HTML doesn't expose the actual sample date, so the user
    should pass it explicitly when the test isn't fresh.

    Returns a summary dict suitable for logs + UI feedback:
        {
          "test_id": "triton-229019",
          "sample_date": "2026-04-16",
          "imported": 32,
          "skipped_symbols": [],
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
    except ParserError as exc:
        bundle = _write_debug_bundle(hass, url, html, exc)
        # Re-raise with the bundle path attached so the surface is
        # "open this folder for the failed page" instead of a generic
        # "parser failed" message.
        raise ParserError(
            f"{exc} Debug bundle written to: {bundle}",
            debug_path=bundle,
        ) from None

    # Populate the URL-derived + service-arg fields the parser left blank.
    report.raw_url = url
    report.test_id = _extract_test_id_from_url(url)
    if sample_date:
        report.sample_date = sample_date
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report.sample_date = today
        _LOGGER.warning(
            "Triton import: no sample_date supplied — defaulting to "
            "today (%s). Pass `sample_date: YYYY-MM-DD` to the service "
            "call when importing an older test, otherwise the readings "
            "land on the wrong day in HA's history graphs.",
            today,
        )

    imported, skipped = await _record_report(coordinator, report)
    summary = {
        "test_id": report.test_id,
        "sample_date": report.sample_date,
        "imported": imported,
        "skipped_symbols": skipped,
    }
    _LOGGER.info("Triton import complete: %s", summary)
    return summary
