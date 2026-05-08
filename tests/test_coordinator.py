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
async def test_latest_icp_test_returns_most_recent_by_imported_at(hass):
    """The dosing-plan sensor reads `latest_icp_test` — must return the
    record with the highest `imported_at`, regardless of insertion order."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    older = {
        "test_id": "T-OLDER", "sample_date": "2026-04-01",
        "imported_at": "2026-04-01T10:00:00+00:00",
        "elements": {}, "recommendations": [],
    }
    newer = {
        "test_id": "T-NEWER", "sample_date": "2026-05-01",
        "imported_at": "2026-05-06T10:00:00+00:00",
        "elements": {}, "recommendations": [],
    }
    # Insert newer first to prove the sort works regardless of order.
    await coord.async_record_icp_test(newer)
    await coord.async_record_icp_test(older)

    latest = coord.latest_icp_test
    assert latest is not None
    assert latest["test_id"] == "T-NEWER"


@pytest.mark.asyncio
async def test_latest_icp_test_returns_none_when_empty(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    assert coord.latest_icp_test is None


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


@pytest.mark.asyncio
async def test_advisor_storage_round_trips(hass):
    """Snapshots, ack, dismiss, demand-change persist and round-trip."""
    coord1 = ReefDataCoordinator(hass)
    await coord1.async_load()

    await coord1.async_record_advisor_snapshot(
        "kh", at="2026-05-06T23:55:00+10:00", kh=9.08, dose_mL=3.0,
    )
    await coord1.async_record_advisor_acknowledgment(
        "kh", applied_value_mL=2.7, prev_value_mL=3.0,
    )
    await coord1.async_record_advisor_dismissal(
        "kh", suggested_value_mL=2.7,
    )
    await coord1.async_record_advisor_demand_change(
        "kh", reason="added 3 SPS frags", expected_direction="increase",
        magnitude_hint_pct=10.0,
    )

    # Re-load — same store payload
    coord2 = ReefDataCoordinator(hass)
    coord2._store = coord1._store  # type: ignore[attr-defined]
    await coord2.async_load()

    snaps = coord2.advisor_snapshots("kh")
    assert len(snaps) == 1
    assert snaps[0]["kh"] == 9.08
    assert snaps[0]["dose_mL"] == 3.0

    acks = coord2.advisor_acknowledgments("kh")
    assert len(acks) == 1
    assert acks[0]["applied_value_mL"] == 2.7
    assert acks[0]["prev_value_mL"] == 3.0

    dismisses = coord2.advisor_dismissals("kh")
    assert len(dismisses) == 1
    assert dismisses[0]["suggested_value_mL"] == 2.7

    demands = coord2.advisor_demand_changes("kh")
    assert len(demands) == 1
    assert demands[0]["reason"] == "added 3 SPS frags"
    assert demands[0]["expected_direction"] == "increase"
    assert demands[0]["magnitude_hint_pct"] == 10.0


@pytest.mark.asyncio
async def test_advisor_blob_backfilled_on_upgrade(hass):
    """Old install without `advisor` key — async_load should backfill."""
    from homeassistant.helpers.storage import Store

    # Pre-populate a Store with v0 schema (no advisor blob).
    store = Store(hass, 1, "old")
    await store.async_save({
        "tank": {"name": "Reef Tank", "habitat": "Mixed Reef",
                 "problem": "None", "method": "Unspecified"},
        "readings": [], "inventory": [], "icp_tests": [],
        "user_removed_dashboard": False,
    })

    coord = ReefDataCoordinator(hass)
    coord._store = store  # type: ignore[attr-defined]
    await coord.async_load()

    # Backfill should have created an empty advisor blob and per-param subdict
    assert coord.advisor_snapshots("kh") == []
    assert coord.advisor_acknowledgments("kh") == []
    assert coord.advisor_water_changes("kh") == []
    assert coord.supplement_profiles == []


@pytest.mark.asyncio
async def test_supplement_profile_lifecycle(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    entry = await coord.async_add_supplement_profile(
        label="Brightwell Alkalin8.3",
        eff_dkh_per_mL_per_100L=0.083,
        label_patterns=["Alkalin8", "ALKALIN 8"],   # mixed case in
        notes="Vendor: brightwellaquatics.com",
    )
    assert entry["id"] == "brightwell_alkalin8_3"
    assert entry["label"] == "Brightwell Alkalin8.3"
    assert entry["eff_dkh_per_mL_per_100L"] == 0.083
    assert entry["label_patterns"] == ["alkalin8", "alkalin 8"]
    assert entry["created_at"]
    assert len(coord.supplement_profiles) == 1

    await coord.async_remove_supplement_profile("brightwell_alkalin8_3")
    assert coord.supplement_profiles == []

    with pytest.raises(ValueError):
        await coord.async_remove_supplement_profile("nonexistent")


@pytest.mark.asyncio
async def test_supplement_profile_slug_collision_with_builtin(hass):
    """Label that slugs to a builtin id ('Custom' → 'custom') gets a suffix."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    entry = await coord.async_add_supplement_profile(
        label="Custom",
        eff_dkh_per_mL_per_100L=0.15,
    )
    assert entry["id"] == "custom_2"


@pytest.mark.asyncio
async def test_supplement_profile_slug_collision_with_existing_user(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    a = await coord.async_add_supplement_profile(
        label="Brightwell Alkalin8.3", eff_dkh_per_mL_per_100L=0.083,
    )
    b = await coord.async_add_supplement_profile(
        label="Brightwell Alkalin8.3", eff_dkh_per_mL_per_100L=0.083,
    )
    assert a["id"] == "brightwell_alkalin8_3"
    assert b["id"] == "brightwell_alkalin8_3_2"


@pytest.mark.asyncio
async def test_supplement_profile_round_trip(hass):
    coord1 = ReefDataCoordinator(hass)
    await coord1.async_load()
    await coord1.async_add_supplement_profile(
        label="Brightwell Alkalin8.3", eff_dkh_per_mL_per_100L=0.083,
        label_patterns=["alkalin8"],
    )

    coord2 = ReefDataCoordinator(hass)
    coord2._store = coord1._store  # type: ignore[attr-defined]
    await coord2.async_load()
    profiles = coord2.supplement_profiles
    assert len(profiles) == 1
    assert profiles[0]["id"] == "brightwell_alkalin8_3"
    assert profiles[0]["label_patterns"] == ["alkalin8"]


@pytest.mark.asyncio
async def test_water_change_round_trip(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    await coord.async_record_water_change(
        "kh", percent=10.0, salt_mix_kh=8.0, notes="Red Sea Coral Pro",
    )
    wcs = coord.advisor_water_changes("kh")
    assert len(wcs) == 1
    assert wcs[0]["percent"] == 10.0
    assert wcs[0]["salt_mix_kh"] == 8.0
    assert wcs[0]["notes"] == "Red Sea Coral Pro"


# ---------------------------------------------------------------------------
# Target-range overrides (Options-flow "Target ranges" page)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_target_range_falls_back_to_parameters_default(hass):
    """No override set → returns the static default from parameters.py.
    Without this fallback, every newly-installed integration would
    return (None, None) for every parameter until the user opened the
    Options page, breaking dashboard tile-color hints + automations."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({})

    lo, hi = coord.get_target_range("kh")
    assert lo == 8.5
    assert hi == 8.9


@pytest.mark.asyncio
async def test_target_range_user_override_wins(hass):
    """An override in entry.options (forwarded into _advisor_config)
    must replace BOTH min and max — partial overrides fall back to the
    defaults so we don't end up with one custom value paired with a
    stale default."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({
        "target_kh_min": 8.0,
        "target_kh_max": 9.5,
    })

    lo, hi = coord.get_target_range("kh")
    assert lo == 8.0
    assert hi == 9.5


@pytest.mark.asyncio
async def test_target_range_partial_override_falls_back(hass):
    """Only `target_kh_min` set, not max → fall back to BOTH defaults
    rather than mixing override-min with default-max. Prevents the
    user from accidentally creating an inverted band (override-min
    above default-max)."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({"target_kh_min": 8.0})

    lo, hi = coord.get_target_range("kh")
    # Both fall back to the parameters.py defaults
    assert lo == 8.5
    assert hi == 8.9


@pytest.mark.asyncio
async def test_target_range_unknown_param_returns_none(hass):
    """A param_id not in parameters.py → (None, None), not a crash.
    Defends against typos in entry.options or future parameter renames."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({})

    lo, hi = coord.get_target_range("not_a_real_param")
    assert lo is None
    assert hi is None


@pytest.mark.asyncio
async def test_target_range_invalid_override_falls_back(hass):
    """Garbage in entry.options (e.g. someone hand-edited the
    .storage file with a string) → fall back to defaults rather than
    crash on float() conversion."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    coord.set_advisor_config({
        "target_kh_min": "not a number",
        "target_kh_max": "also not a number",
    })

    lo, hi = coord.get_target_range("kh")
    assert lo == 8.5  # fell back to default
    assert hi == 8.9
