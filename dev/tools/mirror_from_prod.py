"""Mirror selected entity states from prod HA into the dev HA container.

Read-only on prod (only GET /api/states). Pushes to dev's
POST /api/states/<entity_id>, which sets state + attributes directly on
HA's state machine — no integration logic involved on the dev side.
For testing the alk advisor this is enough: the integration reads
`sensor.kh_keeper_kh` etc. via the state machine, and doesn't care how
the entity got there.

Configured via env vars (see .env.example for the full list):
  HA_PROD_URL, HA_PROD_TOKEN — prod HA + a long-lived access token
  HA_DEV_URL,  HA_DEV_TOKEN  — dev HA + a token created post-onboarding
  MIRROR_ENTITIES            — optional CSV; defaults to the alk-advisor list
  MIRROR_ENABLED             — "false" disables polling without removing the container
  POLL_INTERVAL_SECONDS      — int, default 60

The mirror waits for dev HA to be reachable on first boot before
polling, then runs a forever loop with per-entity error isolation: a
single failed entity doesn't block the rest of the cycle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import aiohttp


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mirror")


# Default scope: the KH Keeper surface plus every head on both RSDOSE4
# units. v1's alk advisor only needs RSDose 1 head 2, but mirroring the
# full doser surface keeps dev realistic for future per-element advisors
# (Ca, Mg, NO3) and lets us visually verify the integration handles
# multi-head config. Override via MIRROR_ENTITIES env to narrow scope.
_RSDOSE_UNITS: list[str] = [
    "rsdose4_1881531676",   # RSDose 1 — Foundation A, Foundation B (alk),
                            #            Foundation C, NO3PO4-X
    "rsdose4_2360278834",   # RSDose 2 — HR Nitrate Remover, dormant alk,
                            #            two unconfigured heads
]
_RSDOSE_PER_HEAD_KEYS: list[str] = [
    "supplement",
    "daily_dose",
    "auto_dosed_today",
    "manual_dosed_today",
    "status",
]
_KH_KEEPER_ENTITIES: list[str] = [
    "sensor.kh_keeper_kh",
    # Three pH sensors exist as of kh-keeper-bridge 0.1.12:
    #   - ph                       — legacy/raw last-reported (any cuvette content)
    #   - ph_pure_tank_water       — only updated by refresh_ph cycle (pure tank water)
    #   - ph_kh_test_water_reagent — only updated by KH-test history (water + reagent)
    # Mirror all three so dev exercises the same downstream consumer
    # logic as prod (e.g. when reeftanktracker pH advisor's auto_source
    # is wired to ph_pure_tank_water). The pH_kh_test sensor is
    # published as entity_category=diagnostic and may be missing on
    # devices not yet on 0.1.12 — the mirror skips 404s so older prod
    # is fine. Note: HA's MQTT discovery generates entity_ids from the
    # friendly NAME ("pH (Pure Tank Water)"), not the object_id we
    # specify in the discovery payload — that's why the entity_ids are
    # the long-form versions below rather than `ph_pure` / `ph_kh_test`.
    "sensor.kh_keeper_ph",
    "sensor.kh_keeper_ph_pure_tank_water",
    "sensor.kh_keeper_ph_kh_test_water_reagent",
    # Refresh-pH cycle progress (kh-keeper-bridge ≥ 0.1.13). Mirrors
    # the silent ~10-min refresh_ph window so dev dashboards show the
    # same phase tile as prod. 404 on older add-on versions is fine.
    "sensor.kh_keeper_refresh_ph_phase",
    "sensor.kh_keeper_refresh_ph_phase_eta",
    "sensor.kh_keeper_last_test_time",
    "sensor.kh_keeper_calibration_due",
    "binary_sensor.kh_keeper_calibration_due_warning",
]

DEFAULT_ENTITIES: list[str] = list(_KH_KEEPER_ENTITIES) + [
    f"sensor.{unit}_head_{head}_{key}"
    for unit in _RSDOSE_UNITS
    for head in (1, 2, 3, 4)
    for key in _RSDOSE_PER_HEAD_KEYS
]


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        log.error("missing required env var: %s", key)
        sys.exit(2)
    return val


async def fetch_prod_state(
    session: aiohttp.ClientSession, base: str, token: str, eid: str,
) -> dict[str, Any] | None:
    url = f"{base}/api/states/{eid}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("prod fetch failed for %s: %s", eid, exc)
        return None


async def push_dev_state(
    session: aiohttp.ClientSession, base: str, token: str,
    eid: str, state: str, attributes: dict[str, Any],
) -> bool:
    url = f"{base}/api/states/{eid}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"state": state, "attributes": attributes}
    try:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("dev push failed for %s: %s", eid, exc)
        return False


async def cycle(
    session: aiohttp.ClientSession,
    prod_url: str, prod_token: str,
    dev_url: str, dev_token: str,
    entities: list[str],
) -> None:
    ok = miss = err = 0
    for eid in entities:
        s = await fetch_prod_state(session, prod_url, prod_token, eid)
        if s is None:
            miss += 1
            continue
        if await push_dev_state(
            session, dev_url, dev_token, eid,
            s.get("state", "unknown"),
            s.get("attributes", {}) or {},
        ):
            ok += 1
        else:
            err += 1
    log.info(
        "mirror cycle complete: ok=%d miss=%d err=%d (of %d total)",
        ok, miss, err, len(entities),
    )


async def wait_for_dev(
    session: aiohttp.ClientSession, dev_url: str, max_attempts: int = 60,
) -> bool:
    """Poll dev's `/api/` endpoint until it accepts a connection.

    HA returns 401 (auth required) when up — that's "ready" for our purposes.
    Connection refused / DNS fail is "not ready yet". Sleeps 2s between
    attempts (so up to 2 min total).
    """
    log.info("waiting for dev HA to be reachable at %s ...", dev_url)
    for _ in range(max_attempts):
        try:
            async with session.get(
                f"{dev_url}/api/",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as r:
                if r.status in (200, 401):
                    log.info("dev HA reachable (status=%d)", r.status)
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2)
    log.error("dev HA not reachable after %d attempts", max_attempts)
    return False


async def main() -> int:
    if os.environ.get("MIRROR_ENABLED", "true").lower() != "true":
        log.warning("MIRROR_ENABLED=false — exiting without polling")
        # Sleep forever rather than exiting (so docker-compose doesn't
        # restart-loop the container while disabled).
        while True:
            await asyncio.sleep(3600)

    prod_url = _required("HA_PROD_URL").rstrip("/")
    prod_token = _required("HA_PROD_TOKEN")
    dev_url = _required("HA_DEV_URL").rstrip("/")
    dev_token = _required("HA_DEV_TOKEN")

    entities_str = os.environ.get("MIRROR_ENTITIES", "").strip()
    entities = (
        [e.strip() for e in entities_str.split(",") if e.strip()]
        if entities_str else DEFAULT_ENTITIES
    )
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

    log.info(
        "mirror starting: %d entities, poll every %ds",
        len(entities), poll_interval,
    )
    log.info("prod %s -> dev %s", prod_url, dev_url)
    for eid in entities:
        log.info("  watching %s", eid)

    async with aiohttp.ClientSession() as session:
        if not await wait_for_dev(session, dev_url):
            return 1
        while True:
            await cycle(
                session, prod_url, prod_token, dev_url, dev_token, entities,
            )
            await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        sys.exit(0)
