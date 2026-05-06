#!/usr/bin/env python3
"""Seed dev HA's alk-advisor snapshot history from 7 days of prod data.

For each of the past N_DAYS local days, computes:
  - KH    = median of `sensor.kh_keeper_kh` state changes during that day,
            fallback to value-at-end-of-day, fallback to SEED_DEFAULTS.
  - dose  = sum of `*_daily_dose` value-at-end-of-day across the
            configured alk heads, fallback to SEED_DEFAULTS.

Then POSTs each as a snapshot to dev's reeftanktracker.capture_snapshot_now
service. Idempotent in spirit (re-running just appends more snapshots —
the algorithm tolerates duplicates).

Run from the Mac (not in a container):

    cd dev
    set -a; source .env; set +a
    HA_DEV_URL=http://localhost:8123 python tools/seed_from_prod.py

Requires `aiohttp` (already in tools/requirements.txt; install via the
venv-tools you used for the expose script).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import aiohttp


# Tank-specific configuration. Tweak if your alk-head set or KH source ever
# changes — these match the dev MIRROR_ENTITIES default.
KH_ENTITY = "sensor.kh_keeper_kh"
DOSE_ENTITIES = [
    "sensor.rsdose4_1881531676_head_2_daily_dose",
]
N_DAYS = 7
LOCAL_TZ = timezone(timedelta(hours=10))   # Brisbane, no DST
END_OF_DAY = (23, 55)                        # hour, minute (matches snapshotter default)

# Last-resort values when prod has no history for a given day.
SEED_DEFAULTS = {
    "kh": 9.08,        # rough match to current prod state
    "dose_mL": 3.0,    # current programmed alk dose
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("seed")


def _required(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        log.error("missing env var: %s", key)
        sys.exit(2)
    return v


def _parse_state_float(s: dict[str, Any]) -> float | None:
    state = s.get("state")
    if state in ("unknown", "unavailable", None):
        return None
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def fetch_history(
    session: aiohttp.ClientSession, base: str, token: str,
    entity_id: str, start: datetime, end: datetime,
) -> list[dict[str, Any]]:
    url = f"{base}/api/history/period/{start.isoformat()}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "end_time": end.isoformat(),
        "filter_entity_id": entity_id,
        "minimal_response": "true",
    }
    async with session.get(
        url, headers=headers, params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data[0] if data else []


def value_at(history: list[dict[str, Any]], timestamp: datetime) -> float | None:
    """Most recent state at or before `timestamp`, parsed as float."""
    best: float | None = None
    for s in history:
        ts = _parse_iso(s.get("last_changed", ""))
        if ts is None:
            continue
        if ts > timestamp:
            break
        v = _parse_state_float(s)
        if v is not None:
            best = v
    return best


def median_for_day(
    history: list[dict[str, Any]], day_start: datetime, day_end: datetime,
) -> float | None:
    vals: list[float] = []
    for s in history:
        ts = _parse_iso(s.get("last_changed", ""))
        if ts is None:
            continue
        if not (day_start <= ts < day_end):
            continue
        v = _parse_state_float(s)
        if v is not None:
            vals.append(v)
    return median(vals) if vals else None


async def call_capture_snapshot(
    session: aiohttp.ClientSession, base: str, token: str,
    at: str, kh: float, dose_mL: float,
) -> bool:
    url = f"{base}/api/services/reeftanktracker/capture_snapshot_now"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"at": at, "kh": kh, "dose_mL": dose_mL}
    try:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                log.error("capture failed (%d): %s", resp.status, text[:300])
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        log.error("capture errored: %s", exc)
        return False


async def main() -> int:
    prod_url = _required("HA_PROD_URL").rstrip("/")
    prod_token = _required("HA_PROD_TOKEN")
    dev_url = _required("HA_DEV_URL").rstrip("/")
    dev_token = _required("HA_DEV_TOKEN")

    now_local = datetime.now(LOCAL_TZ)
    # Fetch a window from a day before the earliest target snapshot to a
    # bit after now, so value_at() always has a leading state to anchor on.
    earliest = (now_local - timedelta(days=N_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    latest = now_local.replace(hour=23, minute=59, second=59, microsecond=0)

    log.info("seeding %d daily snapshots into dev", N_DAYS)
    log.info("prod %s -> dev %s", prod_url, dev_url)
    log.info("history window %s -> %s", earliest.isoformat(), latest.isoformat())

    async with aiohttp.ClientSession() as session:
        # Fetch KH history
        try:
            kh_history = await fetch_history(
                session, prod_url, prod_token, KH_ENTITY, earliest, latest,
            )
            log.info("KH history: %d state changes", len(kh_history))
        except Exception as exc:  # noqa: BLE001
            log.warning("KH history fetch failed (%s) — will use defaults", exc)
            kh_history = []

        # Fetch dose history per head
        dose_histories: dict[str, list[dict[str, Any]]] = {}
        for eid in DOSE_ENTITIES:
            try:
                dose_histories[eid] = await fetch_history(
                    session, prod_url, prod_token, eid, earliest, latest,
                )
                log.info("dose history %s: %d state changes",
                         eid, len(dose_histories[eid]))
            except Exception as exc:  # noqa: BLE001
                log.warning("dose history fetch failed for %s (%s)", eid, exc)
                dose_histories[eid] = []

        # Build per-day snapshots
        plan: list[dict[str, Any]] = []
        for i in range(N_DAYS - 1, -1, -1):   # oldest first
            day = now_local - timedelta(days=i)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            snap_at = day.replace(
                hour=END_OF_DAY[0], minute=END_OF_DAY[1],
                second=0, microsecond=0,
            )

            # KH: prefer same-day median (smooths out individual titrations);
            # fallback to value-at-end-of-day; fallback to seed default.
            kh = median_for_day(kh_history, day_start, day_end)
            kh_source = "prod-day-median"
            if kh is None:
                kh = value_at(kh_history, snap_at)
                kh_source = "prod-value-at-eod" if kh is not None else "seed-default"
            if kh is None:
                kh = SEED_DEFAULTS["kh"]

            # Dose: sum across heads of value-at-end-of-day.
            dose_total: float | None = None
            dose_partial = False
            for eid, hist in dose_histories.items():
                v = value_at(hist, snap_at)
                if v is not None:
                    dose_total = (dose_total or 0.0) + v
                else:
                    dose_partial = True
            if dose_total is None:
                dose_total = SEED_DEFAULTS["dose_mL"]
                dose_source = "seed-default"
            elif dose_partial:
                dose_source = "prod-partial"
            else:
                dose_source = "prod"

            plan.append({
                "at": snap_at.isoformat(),
                "kh": round(kh, 3),
                "dose_mL": round(dose_total, 3),
                "kh_source": kh_source,
                "dose_source": dose_source,
            })

        log.info("---- seed plan ----")
        for s in plan:
            log.info(
                "  %s  kh=%-7s [%-18s]  dose_mL=%-6s [%s]",
                s["at"], s["kh"], s["kh_source"], s["dose_mL"], s["dose_source"],
            )

        log.info("---- posting to dev ----")
        ok = err = 0
        for s in plan:
            success = await call_capture_snapshot(
                session, dev_url, dev_token,
                at=s["at"], kh=s["kh"], dose_mL=s["dose_mL"],
            )
            if success:
                log.info("  ✓ seeded %s", s["at"])
                ok += 1
            else:
                log.error("  ✗ failed %s", s["at"])
                err += 1

        log.info("seeded %d/%d snapshots", ok, ok + err)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
