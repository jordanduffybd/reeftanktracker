"""Coordinator behaviour tests.

The coordinator is where real logic lives — readings storage, latest-
by-sample-date selection, inventory CRUD, ICP record persistence. These
tests run against the in-memory Store stub from conftest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reeftanktracker.coordinator import ReefDataCoordinator
from reeftanktracker.const import (
    HABITATS, PROBLEMS, SOURCE_AUTO, SOURCE_ICP, SOURCE_MANUAL,
)


def _iso(when: datetime) -> str:
    return when.astimezone().isoformat()


@pytest.mark.asyncio
async def test_load_initialises_default_data(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    assert coord.tank["name"] == "Reef Tank"
    assert coord.tank["habitat"] in HABITATS
    assert coord.tank["problem"] in PROBLEMS
    assert coord.inventory == []
    assert coord.icp_tests == []


@pytest.mark.asyncio
async def test_record_reading_persists_with_timestamp(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    reading = await coord.async_record_reading(
        parameter="kh", value=8.4, unit="dKH", method="Hanna ULR",
    )
    assert reading.value == 8.4
    assert reading.source == SOURCE_MANUAL
    # Sample timestamp defaults to now and is iso-parseable
    parsed = datetime.fromisoformat(reading.sample_taken_at)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_latest_reading_picks_highest_sample_taken_at(hass):
    """Two readings, ICP sampled earlier than a Hanna sampled later — Hanna wins.

    Even though the ICP was *recorded* most recently (e.g. lab results
    just imported), the user's Hanna test was sampled more recently.
    """
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    now = datetime.now(timezone.utc)
    older = _iso(now - timedelta(days=14))   # ICP sample 2 weeks ago
    newer = _iso(now - timedelta(days=1))    # Hanna sample yesterday

    await coord.async_record_reading(
        "kh", 7.9, source=SOURCE_ICP, sample_taken_at=older, test_id="B-OLD",
    )
    # Imported a moment later, but sample_taken_at is yesterday
    await coord.async_record_reading(
        "kh", 8.4, source=SOURCE_MANUAL, sample_taken_at=newer, method="Hanna",
    )

    latest = coord.latest_reading("kh")
    assert latest is not None
    assert latest["value"] == 8.4
    assert latest["source"] == SOURCE_MANUAL


@pytest.mark.asyncio
async def test_latest_manual_ignores_icp(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)

    await coord.async_record_reading(
        "calcium", 429, source=SOURCE_ICP, sample_taken_at=_iso(now),
    )
    await coord.async_record_reading(
        "calcium", 451, source=SOURCE_MANUAL,
        sample_taken_at=_iso(now - timedelta(days=3)), method="Hanna",
    )

    latest_any = coord.latest_reading("calcium")
    latest_manual = coord.latest_manual("calcium")
    # latest_reading goes by sample date (ICP just now wins)
    assert latest_any["value"] == 429
    # latest_manual ignores ICP entirely
    assert latest_manual["value"] == 451


@pytest.mark.asyncio
async def test_record_reading_rejects_invalid_source(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    with pytest.raises(ValueError):
        await coord.async_record_reading("kh", 8.4, source="bogus")


@pytest.mark.asyncio
async def test_set_habitat_validates(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    await coord.async_set_habitat(habitat="LPS Dominant", problem="Cyanobacteria")
    assert coord.tank["habitat"] == "LPS Dominant"
    assert coord.tank["problem"] == "Cyanobacteria"

    with pytest.raises(ValueError):
        await coord.async_set_habitat(habitat="Made-up biotope")
    with pytest.raises(ValueError):
        await coord.async_set_habitat(problem="Aliens")


@pytest.mark.asyncio
async def test_inventory_lifecycle(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    entry = await coord.async_add_inventory(
        category="coral", name="Acropora millepora", type="SPS",
        added_at="2024-08-15", count=1, notes="Top right, high light",
    )
    assert entry["id"]
    assert entry["category"] == "coral"
    assert entry["removed_at"] is None
    assert len(coord.inventory) == 1

    await coord.async_remove_inventory(entry["id"], removed_at="2025-01-10")
    survivor = coord.inventory[0]
    assert survivor["removed_at"] == "2025-01-10"

    with pytest.raises(ValueError):
        await coord.async_remove_inventory("not-a-real-id")


@pytest.mark.asyncio
async def test_icp_test_dedupes_by_test_id(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    rec_a = {"test_id": "B-KJAZM8", "sample_date": "2025-04-01",
             "elements": {"Ca": {"value": 429}}}
    rec_b = {"test_id": "B-KJAZM8", "sample_date": "2025-04-01",
             "elements": {"Ca": {"value": 432}}}  # corrected import

    await coord.async_record_icp_test(rec_a)
    await coord.async_record_icp_test(rec_b)

    # Only one record with that id; the latest one wins
    matches = [t for t in coord.icp_tests if t["test_id"] == "B-KJAZM8"]
    assert len(matches) == 1
    assert matches[0]["elements"]["Ca"]["value"] == 432


@pytest.mark.asyncio
async def test_icp_record_requires_test_id(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    with pytest.raises(ValueError):
        await coord.async_record_icp_test({"sample_date": "2025-04-01"})


@pytest.mark.asyncio
async def test_readings_persist_across_load(hass):
    """Save then re-load via the same Store stub — data must round-trip."""
    coord1 = ReefDataCoordinator(hass)
    await coord1.async_load()
    await coord1.async_record_reading("kh", 8.4)

    # Reuse the same Store instance — coordinator2 is a fresh wrapper but
    # the underlying _payload is preserved.
    coord2 = ReefDataCoordinator(hass)
    coord2._store = coord1._store  # type: ignore[attr-defined]
    await coord2.async_load()

    assert coord2.latest_reading("kh")["value"] == 8.4
