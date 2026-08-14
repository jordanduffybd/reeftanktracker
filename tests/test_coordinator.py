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
async def test_supplement_profile_update_eff_per_mL(hass):
    """Use case: profile registered in 0.4.4 era without
    eff_per_mL_per_100L; we now add the per-element potency in place."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    # Pre-state: profile registered without per-element potency.
    await coord.async_add_supplement_profile(
        label="Quantum HR Nitrate Remover (5L)",
        param_id="nitrate",
        # No eff_per_mL_per_100L — simulating 0.4.4 registration.
    )
    pid = coord.supplement_profiles[0]["id"]
    assert coord.supplement_profiles[0]["eff_per_mL_per_100L"] is None

    # Update in place.
    updated = await coord.async_update_supplement_profile(
        pid, eff_per_mL_per_100L=-0.5,
    )
    assert updated["eff_per_mL_per_100L"] == -0.5
    # Other fields unchanged.
    assert updated["label"] == "Quantum HR Nitrate Remover (5L)"
    assert updated["param_id"] == ["nitrate"]


@pytest.mark.asyncio
async def test_supplement_profile_update_only_passed_fields(hass):
    """Sentinel pattern: fields not passed must remain unchanged.
    Distinguishes "leave alone" from "explicitly clear to None"."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    await coord.async_add_supplement_profile(
        label="X", param_id="kh", eff_dkh_per_mL_per_100L=0.1,
        label_patterns=["x_label"], notes="initial",
    )
    pid = coord.supplement_profiles[0]["id"]

    # Update only notes — other fields must be untouched.
    updated = await coord.async_update_supplement_profile(
        pid, notes="updated",
    )
    assert updated["notes"] == "updated"
    assert updated["label_patterns"] == ["x_label"]
    assert updated["eff_dkh_per_mL_per_100L"] == 0.1
    assert updated["param_id"] == ["kh"]


@pytest.mark.asyncio
async def test_supplement_profile_update_clear_field_to_none(hass):
    """Explicitly passing None must CLEAR the field (vs sentinel which
    leaves it). Tests the _UNSET / explicit-None distinction."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    await coord.async_add_supplement_profile(
        label="X", param_id="kh", eff_dkh_per_mL_per_100L=0.1,
        notes="initial",
    )
    pid = coord.supplement_profiles[0]["id"]

    updated = await coord.async_update_supplement_profile(
        pid, notes=None,
    )
    assert updated["notes"] is None
    # Other fields still untouched.
    assert updated["eff_dkh_per_mL_per_100L"] == 0.1


@pytest.mark.asyncio
async def test_supplement_profile_update_unknown_id_raises(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    with pytest.raises(ValueError):
        await coord.async_update_supplement_profile(
            "nonexistent", eff_per_mL_per_100L=0.5,
        )


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


# ---------------------------------------------------------------------------
# Supplement profile param_id (0.4.4 — foundation for per-element advisors)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_supplement_profile_defaults_to_kh_param_id(hass):
    """A profile added without an explicit param_id defaults to "kh"
    so existing alk supplements keep working unchanged. Without this
    back-compat default, profiles registered before 0.4.4 would
    disappear from the alk advisor's dropdown after upgrade."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    entry = await coord.async_add_supplement_profile(
        label="Plain Alk Supplement",
        eff_dkh_per_mL_per_100L=0.1,
    )
    # Always stored as a list internally — string input → 1-element list.
    assert entry["param_id"] == ["kh"]


@pytest.mark.asyncio
async def test_supplement_profile_accepts_non_kh_param_id(hass):
    """Non-KH supplements (Ca, Mg, NO3, PO4) can be registered with
    no eff_dkh_per_mL_per_100L since the field is meaningless for
    them. Per-element advisors arriving in 0.5.0 will read these."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    entry = await coord.async_add_supplement_profile(
        label="Quantum AR Phosphate Remover",
        param_id="phosphate",
        notes="lanthanum-based, dose by PO4 level",
    )
    assert entry["param_id"] == ["phosphate"]
    assert entry["eff_dkh_per_mL_per_100L"] is None


@pytest.mark.asyncio
async def test_supplement_profiles_for_filters_by_param_id(hass):
    """The new helper returns only profiles for the requested
    parameter. Per-element advisors call this to get THEIR
    supplements without seeing alk supplements (and vice versa)."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    await coord.async_add_supplement_profile(
        label="Foundation B Custom", eff_dkh_per_mL_per_100L=0.1,
    )  # defaults param_id="kh"
    await coord.async_add_supplement_profile(
        label="Quantum AR Phosphate", param_id="phosphate",
    )
    await coord.async_add_supplement_profile(
        label="Quantum LR Nitrate", param_id="nitrate",
    )
    await coord.async_add_supplement_profile(
        label="Quantum HR Nitrate", param_id="nitrate",
    )

    kh = coord.supplement_profiles_for("kh")
    po4 = coord.supplement_profiles_for("phosphate")
    no3 = coord.supplement_profiles_for("nitrate")
    ca = coord.supplement_profiles_for("calcium")

    assert len(kh) == 1
    assert kh[0]["label"] == "Foundation B Custom"
    assert len(po4) == 1
    assert len(no3) == 2
    assert ca == []  # no calcium supplements registered


@pytest.mark.asyncio
async def test_supplement_profiles_for_multi_target_appears_in_each(hass):
    """Multi-target supplements (e.g. Red Sea NO3:PO4-X with
    param_id=["nitrate","phosphate"]) must surface in BOTH the
    nitrate AND phosphate per-element advisors. Without this, the
    user has to register the same supplement twice — error-prone
    and creates duplicate dose tracking."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    npx = await coord.async_add_supplement_profile(
        label="Red Sea NO3:PO4-X",
        param_id=["nitrate", "phosphate"],
        notes="targets both NO3 and PO4 simultaneously",
    )
    assert npx["param_id"] == ["nitrate", "phosphate"]
    assert npx["eff_dkh_per_mL_per_100L"] is None

    # Surfaces in BOTH per-element queries
    no3_supps = coord.supplement_profiles_for("nitrate")
    po4_supps = coord.supplement_profiles_for("phosphate")
    kh_supps = coord.supplement_profiles_for("kh")

    assert len(no3_supps) == 1 and no3_supps[0]["id"] == npx["id"]
    assert len(po4_supps) == 1 and po4_supps[0]["id"] == npx["id"]
    assert kh_supps == []  # multi-target NO3+PO4 doesn't pollute alk advisor


@pytest.mark.asyncio
async def test_supplement_profile_string_param_id_normalizes_to_list(hass):
    """The user can pass a single string (the common case) and we
    normalize to a list internally — so the storage shape is always
    consistent and `supplement_profiles_for` doesn't need two
    code paths."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    entry = await coord.async_add_supplement_profile(
        label="Quantum AR Phosphate Remover",
        param_id="phosphate",  # passed as string
    )
    assert entry["param_id"] == ["phosphate"]  # stored as list


@pytest.mark.asyncio
async def test_supplement_profiles_for_kh_includes_legacy_no_param_id(hass):
    """Profiles created before 0.4.4 won't have a param_id field on
    disk. Reading them back must default to "kh" so existing alk
    supplements survive the upgrade.

    Without this back-compat, every supplement in `.storage` from a
    pre-0.4.4 install would be dropped from the alk advisor's dropdown
    silently — confidence in the upgrade gets shaken."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    # Simulate a pre-0.4.4 storage shape: profile with no param_id
    coord._data["supplement_profiles"] = [{
        "id": "legacy_alk_supp",
        "label": "Legacy Alk Supplement",
        "eff_dkh_per_mL_per_100L": 0.083,
        "label_patterns": [],
        "created_at": "2026-04-01T00:00:00+00:00",
        "notes": None,
        # Note: NO param_id field — written by pre-0.4.4 code
    }]

    kh = coord.supplement_profiles_for("kh")
    assert len(kh) == 1
    assert kh[0]["id"] == "legacy_alk_supp"


# ---------------------------------------------------------------------------
# ignore_readings / unignore_readings (0.5.9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ignore_readings_marks_in_window(hass):
    """Readings inside the window get excluded_at + excluded_reason;
    readings outside are untouched."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()

    now = datetime.now(timezone.utc)
    before = _iso(now - timedelta(hours=10))
    inside_a = _iso(now - timedelta(hours=6))
    inside_b = _iso(now - timedelta(hours=5))
    after = _iso(now - timedelta(hours=1))

    await coord.async_record_reading(
        "kh", 8.4, source=SOURCE_AUTO, sample_taken_at=before,
    )
    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=inside_a,
    )
    await coord.async_record_reading(
        "kh", 0.1, source=SOURCE_AUTO, sample_taken_at=inside_b,
    )
    await coord.async_record_reading(
        "kh", 8.5, source=SOURCE_AUTO, sample_taken_at=after,
    )

    result = await coord.async_ignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=7)),
        to_iso=_iso(now - timedelta(hours=4)),
        reason="sensor malfunction",
    )
    assert result["readings"] == 2

    # Excluded readings should NOT appear in default readings_for
    visible = coord.readings_for("kh")
    assert len(visible) == 2
    assert {r["value"] for r in visible} == {8.4, 8.5}

    # But ARE visible with include_excluded
    all_readings = coord.readings_for("kh", include_excluded=True)
    assert len(all_readings) == 4
    excluded = [r for r in all_readings if r.get("excluded_at")]
    assert len(excluded) == 2
    assert all(r["excluded_reason"] == "sensor malfunction" for r in excluded)


@pytest.mark.asyncio
async def test_ignore_readings_source_filter(hass):
    """source='auto' must not touch manual readings in the same window."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    at = _iso(now - timedelta(hours=2))

    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=at,
    )
    await coord.async_record_reading(
        "kh", 8.4, source=SOURCE_MANUAL, sample_taken_at=at,
    )

    result = await coord.async_ignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=3)),
        to_iso=_iso(now - timedelta(hours=1)),
        reason="sensor",
        source=SOURCE_AUTO,
    )
    assert result["readings"] == 1

    visible = coord.readings_for("kh")
    assert len(visible) == 1
    assert visible[0]["source"] == SOURCE_MANUAL


@pytest.mark.asyncio
async def test_unignore_readings_clears_exclusion(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    at = _iso(now - timedelta(hours=2))

    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=at,
    )
    await coord.async_ignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=3)),
        to_iso=_iso(now - timedelta(hours=1)),
        reason="sensor",
    )
    assert coord.readings_for("kh") == []

    result = await coord.async_unignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=3)),
        to_iso=_iso(now - timedelta(hours=1)),
    )
    assert result["readings"] == 1
    visible = coord.readings_for("kh")
    assert len(visible) == 1
    assert "excluded_at" not in visible[0]
    assert "excluded_reason" not in visible[0]


@pytest.mark.asyncio
async def test_ignore_readings_sweeps_advisor_snapshots(hass):
    """The whole point: advisor snapshots in the same window must be
    excluded too, otherwise the advisor would still see the bad
    median because it reads snapshots not raw readings."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    inside = _iso(now - timedelta(hours=4))
    outside = _iso(now - timedelta(hours=20))

    await coord.async_record_advisor_snapshot(
        "kh", at=outside, kh=8.4, dose_mL=10.0,
    )
    await coord.async_record_advisor_snapshot(
        "kh", at=inside, kh=99.0, dose_mL=10.0,
    )

    # Also a reading inside the window so we exercise both paths
    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=inside,
    )

    result = await coord.async_ignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=6)),
        to_iso=_iso(now - timedelta(hours=2)),
        reason="sensor malfunction",
    )
    assert result["readings"] == 1
    assert result["snapshots"] == 1

    # Default-filtered snapshot list excludes the bad one
    snaps = coord.advisor_snapshots("kh")
    assert len(snaps) == 1
    assert snaps[0]["kh"] == 8.4

    # include_excluded shows both
    all_snaps = coord.advisor_snapshots("kh", include_excluded=True)
    assert len(all_snaps) == 2


@pytest.mark.asyncio
async def test_ignore_readings_idempotent(hass):
    """Running ignore twice over the same window doesn't re-mark
    already-excluded readings (counts only newly-affected)."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    at = _iso(now - timedelta(hours=2))

    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=at,
    )
    kwargs = dict(
        from_iso=_iso(now - timedelta(hours=3)),
        to_iso=_iso(now - timedelta(hours=1)),
        reason="sensor",
    )
    r1 = await coord.async_ignore_readings("kh", **kwargs)
    r2 = await coord.async_ignore_readings("kh", **kwargs)
    assert r1["readings"] == 1
    assert r2["readings"] == 0


@pytest.mark.asyncio
async def test_ignore_readings_rejects_bad_args(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    valid_from = _iso(now - timedelta(hours=2))
    valid_to = _iso(now)

    # from > to
    with pytest.raises(ValueError):
        await coord.async_ignore_readings(
            "kh", from_iso=valid_to, to_iso=valid_from, reason="x",
        )
    # blank reason
    with pytest.raises(ValueError):
        await coord.async_ignore_readings(
            "kh", from_iso=valid_from, to_iso=valid_to, reason="   ",
        )
    # invalid source filter
    with pytest.raises(ValueError):
        await coord.async_ignore_readings(
            "kh", from_iso=valid_from, to_iso=valid_to,
            reason="x", source="bogus",
        )


@pytest.mark.asyncio
async def test_latest_reading_skips_excluded(hass):
    """The latest_reading helper must skip excluded readings — sensor.py
    reads through this for the displayed 'latest KH' state."""
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)

    older = _iso(now - timedelta(hours=10))
    newer = _iso(now - timedelta(hours=1))

    await coord.async_record_reading(
        "kh", 8.4, source=SOURCE_AUTO, sample_taken_at=older,
    )
    # The newer one is the malfunction
    await coord.async_record_reading(
        "kh", 99.0, source=SOURCE_AUTO, sample_taken_at=newer,
    )

    # Before ignoring: newest (bad) wins
    pre = coord.latest_reading("kh")
    assert pre["value"] == 99.0

    await coord.async_ignore_readings(
        "kh",
        from_iso=_iso(now - timedelta(hours=2)),
        to_iso=_iso(now),
        reason="sensor malfunction",
    )

    # After ignoring: the older clean reading is now "latest" again
    post = coord.latest_reading("kh")
    assert post["value"] == 8.4

    # include_excluded restores the original behaviour
    forced = coord.latest_reading("kh", include_excluded=True)
    assert forced["value"] == 99.0


# ---------------------------------------------------------------------------
# Statistics live-window guard (0.5.12)
#
# `ReefLatestSensor` sets state_class=measurement, so HA's recorder already
# compiles hourly statistics for `sensor.<param>_latest`. Importing into the
# same statistic_id for the same hour raced the recorder and produced hourly
# "UNIQUE constraint failed: statistics.metadata_id, statistics.start_ts"
# errors in production on HA 2026.7.2. The recorder owns the present; the
# import path owns the past.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_is_backfillable_rejects_recent_samples(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    assert coord._is_backfillable(now) is False
    assert coord._is_backfillable(now - timedelta(minutes=30)) is False
    # Just inside the boundary — still the recorder's hour.
    assert coord._is_backfillable(
        now - coord.LIVE_STATS_WINDOW + timedelta(minutes=1)
    ) is False


@pytest.mark.asyncio
async def test_is_backfillable_accepts_historical_samples(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    now = datetime.now(timezone.utc)
    assert coord._is_backfillable(now - timedelta(days=1)) is True
    assert coord._is_backfillable(
        datetime(2023, 4, 1, tzinfo=timezone.utc)
    ) is True


@pytest.mark.asyncio
async def test_is_backfillable_treats_naive_timestamps_as_utc(hass):
    coord = ReefDataCoordinator(hass)
    await coord.async_load()
    naive_old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert coord._is_backfillable(naive_old) is True
    assert coord._is_backfillable(naive_now) is False
