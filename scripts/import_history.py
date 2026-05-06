#!/usr/bin/env python3
"""One-shot importer for historical reef-tank readings.

Pushes legacy data into Reef Tank Tracker via Home Assistant's REST API.
Two sources supported:

  * `--aquarium-log path.csv` — the Aquarium Log app's export format:
        parameter,value,createdAt,note
    All rows are recorded as manual Hanna readings (the user's setup).

  * `--triton path.csv` — Triton ICP-OES export:
        Element,Name,Analysis,Setpoint,Unit,"Warning level",Group
    Each row becomes an ICP reading. Sample date must be passed via
    `--triton-sample-date YYYY-MM-DD` (Triton CSVs don't carry it).

Usage:
    export HA_URL='https://hass.example.com'
    export HA_TOKEN='long-lived-access-token'
    python3 scripts/import_history.py \\
        --aquarium-log Reef_measurements.csv \\
        --triton "Reef tank - 16.04.2025 (B-KJAZM8).csv" \\
        --triton-sample-date 2025-04-16

Get the token from: HA → your profile → Long-Lived Access Tokens → Create.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# Aquarium Log "parameter" column → integration parameter id
AQ_LOG_MAP: dict[str, str] = {
    "Calcium": "calcium",
    "KH": "kh",
    "Alkalinity": "kh",       # some versions of Aquarium Log export this label
    "pH": "ph",
    "PH": "ph",
    "Phosphate": "phosphate",
    "Nitrate": "nitrate",
    "Nitrite": "nitrite",
    "Ammonia": "ammonia",
    "Salinity": "salinity",
    "Temperature": "temperature",
    "Magnesium": "magnesium",
    "Iodine": "iodine",
    "Strontium": "strontium",
}

# Triton element symbol → integration parameter id
TRITON_MAP: dict[str, str] = {
    "Al": "aluminium", "Sb": "antimony", "As": "arsenic", "Pb": "lead",
    "Cd": "cadmium", "Cu": "copper", "La": "lanthanum", "Hg": "mercury",
    "Sc": "scandium", "Se": "selenium", "Ti": "titanium", "W": "tungsten",
    "Sn": "tin",
    "Cl": "chloride", "Na": "sodium", "Ca": "calcium", "Mg": "magnesium",
    "K": "potassium", "Br": "bromide", "B": "boron", "F": "fluoride",
    "Sr": "strontium", "S": "sulphur",
    "Li": "lithium", "Ni": "nickel", "Mo": "molybdenum",
    "V": "vanadium", "Zn": "zinc", "Mn": "manganese", "I": "iodine",
    "Cr": "chromium", "Co": "cobalt", "Fe": "iron",
    "Ba": "barium", "Be": "beryllium",
    "Si": "silicon",
    "P": "phosphorus", "PO4": "phosphate",
    "Sal": "salinity",
}


def call_service(base_url: str, token: str, domain: str,
                 service: str, data: dict) -> None:
    """POST a service call. Raises on non-2xx."""
    url = f"{base_url.rstrip('/')}/api/services/{domain}/{service}"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(data).encode("utf-8"),
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def to_iso_local(dt_str: str, tz_name: str | None) -> str:
    """Parse 'YYYY-MM-DD HH:MM:SS' (no tz) and emit a tz-aware ISO string.

    The Aquarium Log app exports timestamps without timezone info — they
    are recorded in the user's local time. We attach `tz_name` (defaults
    to system local) so HA's recorder gets a date-correct sample time.
    """
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        if tz_name:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        else:
            dt = dt.astimezone()  # system local
    return dt.isoformat()


def normalise_salinity(value: float) -> tuple[float, str | None]:
    """Coerce a salinity reading into ppt.

    Reef hobby data is wildly inconsistent — some apps record specific
    gravity (~1.024–1.027), some record ppt (~33–37). We can't tell from
    the column name. Heuristic: values below 2 are SG, above 20 are ppt.

    Returns (value_in_ppt, conversion_note). The note is None if no
    conversion was needed.
    """
    if value < 2.0:
        # Practical reefkeeping convention: 1.026 SG @ 25°C ≈ 35 ppt.
        # Linear approx (SG − 1) × 1346 lands 1.026 on 35.0.
        ppt = round((value - 1.0) * 1346, 2)
        return ppt, f"converted SG {value} → {ppt} ppt"
    return value, None


def import_aquarium_log(base_url: str, token: str, csv_path: Path,
                        method: str, tz_name: str | None,
                        dry_run: bool) -> tuple[int, int]:
    """Returns (imported, skipped)."""
    imported = 0
    skipped = 0
    with csv_path.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            label = row["parameter"]
            param_id = AQ_LOG_MAP.get(label)
            if not param_id:
                print(f"  row {i}: skip unknown parameter {label!r}")
                skipped += 1
                continue
            try:
                value = float(row["value"])
            except (KeyError, ValueError):
                print(f"  row {i}: skip — bad value {row.get('value')!r}")
                skipped += 1
                continue
            # Unit normalisation per parameter
            note_extra = None
            if param_id == "salinity":
                value, note_extra = normalise_salinity(value)
            sample_at = to_iso_local(row["createdAt"], tz_name)
            note = (row.get("note") or "").strip() or None
            payload = {
                "parameter": param_id,
                "value": value,
                "method": method,
                "source": "manual",
                "sample_taken_at": sample_at,
            }
            if note:
                payload["notes"] = note
            tag = f" [{note_extra}]" if note_extra else ""
            if dry_run:
                print(f"  DRY  {param_id} {value} @ {sample_at}{tag}")
            else:
                call_service(base_url, token, "reeftanktracker",
                             "record_reading", payload)
                print(f"  ✓    {param_id} {value} @ {sample_at}{tag}")
            imported += 1
    return imported, skipped


def import_triton(base_url: str, token: str, csv_path: Path,
                  sample_date: str, test_id: str | None,
                  dry_run: bool) -> tuple[int, int]:
    """Import a Triton ICP-OES CSV. Each element becomes a `record_reading`
    with source=icp; we also stash a full ICP record via import_icp."""
    imported = 0
    skipped = 0
    elements: dict[str, dict] = {}
    with csv_path.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            sym = row["Element"]
            param_id = TRITON_MAP.get(sym)
            if not param_id:
                print(f"  row {i}: skip unknown Triton element {sym!r}")
                skipped += 1
                continue
            try:
                value = float(row["Analysis"])
            except (KeyError, ValueError):
                print(f"  row {i}: skip — bad analysis {row.get('Analysis')!r}")
                skipped += 1
                continue
            unit = row.get("Unit") or None
            elements[sym] = {
                "value": value, "unit": unit,
                "name": row.get("Name"),
                "setpoint": row.get("Setpoint"),
                "warning": row.get("Warning level") or None,
                "group": row.get("Group"),
            }
            sample_at = f"{sample_date}T08:00:00+00:00"  # noon-ish UTC, sample-day-correct
            payload = {
                "parameter": param_id,
                "value": value,
                "unit": unit,
                "method": "Triton ICP-OES",
                "source": "icp",
                "sample_taken_at": sample_at,
                "test_id": test_id or "",
            }
            if dry_run:
                print(f"  DRY  ICP {param_id} {value} {unit or ''} @ {sample_at}")
            else:
                call_service(base_url, token, "reeftanktracker",
                             "record_reading", payload)
                print(f"  ✓    ICP {param_id} {value} {unit or ''} @ {sample_at}")
            imported += 1

    # Stash the test record itself
    if not dry_run and test_id:
        record = {
            "test_id": test_id,
            "sample_date": sample_date,
            "imported_at": datetime.now().astimezone().isoformat(),
            "source": "triton-csv",
            "elements": elements,
        }
        call_service(base_url, token, "reeftanktracker", "import_icp",
                     {"test_record": record})
        print(f"  ✓    stashed ICP test {test_id}")

    return imported, skipped


def _strip_quoted_path(s: str) -> str:
    """Tolerate accidentally-double-quoted paths.

    If a user types `--triton "'/path/to/file.csv'"` the shell hands us
    the inner quotes literally, leading to FileNotFoundError on a path
    with embedded apostrophes. Strip a single matching pair if we see
    one — real paths shouldn't start AND end with the same quote char.
    """
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _path(s: str) -> Path:
    return Path(_strip_quoted_path(s)).expanduser()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ha-url", default=os.environ.get("HA_URL"),
                   help="HA base URL (or HA_URL env var)")
    p.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"),
                   help="Long-lived access token (or HA_TOKEN env var)")
    p.add_argument("--aquarium-log", type=_path,
                   help="Path to Aquarium Log CSV export")
    p.add_argument("--method", default="Hanna ULR",
                   help="Test method label for Aquarium Log rows (default: Hanna ULR)")
    p.add_argument("--triton", type=_path,
                   help="Path to Triton ICP-OES CSV export")
    p.add_argument("--triton-sample-date",
                   help="Sample date for the Triton test, YYYY-MM-DD")
    p.add_argument("--triton-test-id",
                   help="Triton test reference (e.g. B-KJAZM8)")
    p.add_argument("--tz", help="Timezone for Aquarium Log timestamps "
                                "(e.g. Australia/Melbourne). Defaults to system local.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be sent without calling HA")
    args = p.parse_args()

    if not args.dry_run and (not args.ha_url or not args.ha_token):
        p.error("--ha-url and --ha-token (or HA_URL/HA_TOKEN env) are required "
                "unless --dry-run is set")

    if not args.aquarium_log and not args.triton:
        p.error("Provide --aquarium-log and/or --triton")

    total_in = total_skip = 0

    if args.aquarium_log:
        print(f"Importing Aquarium Log: {args.aquarium_log}")
        i, s = import_aquarium_log(
            args.ha_url or "", args.ha_token or "",
            args.aquarium_log, args.method, args.tz, args.dry_run,
        )
        print(f"  → {i} imported, {s} skipped\n")
        total_in += i
        total_skip += s

    if args.triton:
        if not args.triton_sample_date:
            p.error("--triton-sample-date YYYY-MM-DD is required with --triton")
        print(f"Importing Triton CSV: {args.triton}")
        i, s = import_triton(
            args.ha_url or "", args.ha_token or "",
            args.triton, args.triton_sample_date, args.triton_test_id,
            args.dry_run,
        )
        print(f"  → {i} imported, {s} skipped\n")
        total_in += i
        total_skip += s

    print(f"Done: {total_in} imported, {total_skip} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
