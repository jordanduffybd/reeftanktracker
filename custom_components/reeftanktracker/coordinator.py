"""Data coordinator for Reef Tank Tracker.

Owns the on-disk state (`Store`-backed JSON in HA's .storage) and exposes
helpers to record readings, manage inventory, and query "latest known
value" per parameter. Sensors and number entities subscribe to its
state-changed dispatch signal.

For historical data we ALSO write to HA's long-term statistics table
(via `recorder.async_import_statistics`) using each reading's actual
`sample_taken_at`. Without that, all readings would land at "now" in
HA's history graphs because the state machine timestamps state changes
with wall-clock time, not the underlying sample time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import logging
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

# Recorder/statistics imports are wrapped because they're an HA-internal
# API surface that occasionally moves; we degrade gracefully if the
# recorder isn't loaded (rare but possible in stripped-down setups).
try:
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
    )
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    _STATS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _STATS_AVAILABLE = False

from .const import (
    DOMAIN,
    HABITATS,
    PROBLEMS,
    SIGNAL_HABITAT_CHANGED,
    SIGNAL_INVENTORY_CHANGED,
    SIGNAL_READING_RECORDED,
    SOURCE_AUTO,
    SOURCE_ICP,
    SOURCE_MANUAL,
    STORAGE_KEY,
    STORAGE_VERSION,
    TEST_METHODS,
)

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass
class Reading:
    """One recorded parameter reading.

    `sample_taken_at` is when the water was actually sampled (canonical for
    history). `recorded_at` is when the bridge / user logged it (might be
    weeks later for ICP imports).
    """
    parameter: str
    value: float
    unit: str | None
    method: str | None
    source: str          # "manual" | "auto" | "icp"
    sample_taken_at: str  # ISO 8601
    recorded_at: str
    test_id: str | None = None       # ICP test reference
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter,
            "value": self.value,
            "unit": self.unit,
            "method": self.method,
            "source": self.source,
            "sample_taken_at": self.sample_taken_at,
            "recorded_at": self.recorded_at,
            "test_id": self.test_id,
            "notes": self.notes,
        }


class ReefDataCoordinator:
    """In-memory + persistent store for tank data.

    Storage layout (JSON):
        {
          "tank":      {"name": "Reef Tank", "habitat": "...", "problem": "..."},
          "readings":  [Reading.as_dict(), ...],
          "inventory": [{...}, ...],
          "icp_tests": [{...}, ...]
        }
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {}
        # Map of parameter_id → entity_id used as the auto-source.
        # Populated from config_entry.options at setup, falls back to
        # the parameter's hardcoded default if no override is set.
        self._auto_sources: dict[str, str] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        self._data = loaded or self._default_data()
        # Backfill any keys that may be missing on upgrade.
        for k, v in self._default_data().items():
            self._data.setdefault(k, v)

    def _default_data(self) -> dict[str, Any]:
        return {
            "tank": {
                "name": "Reef Tank",
                "habitat": HABITATS[0],
                "problem": PROBLEMS[0],
                # Active session method — used as the default when the
                # user enters a value via a `number.reef_tank_*_entry`
                # entity. "Unspecified" → method=None on records.
                "method": TEST_METHODS[0],
            },
            "readings": [],
            "inventory": [],
            "icp_tests": [],
            # Set to True if the user explicitly removes the auto-installed
            # Lovelace dashboard. Prevents it from coming back on next boot.
            "user_removed_dashboard": False,
        }

    # ------------------------------------------------------------------
    # Auto-source sensor map (configured via OptionsFlow)
    # ------------------------------------------------------------------
    def set_auto_sources(self, mapping: dict[str, str]) -> None:
        """Replace the parameter→sensor map. Empty strings drop entries."""
        self._auto_sources = {k: v for k, v in mapping.items() if v}

    def get_auto_source(self, param_id: str) -> str | None:
        """Return the configured auto-source entity_id for a parameter, or None."""
        return self._auto_sources.get(param_id)

    # ------------------------------------------------------------------
    # Dashboard flag
    # ------------------------------------------------------------------
    def is_dashboard_user_removed(self) -> bool:
        return bool(self._data.get("user_removed_dashboard"))

    async def async_set_dashboard_user_removed(self, value: bool) -> None:
        self._data["user_removed_dashboard"] = bool(value)
        await self.async_save()

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    # ------------------------------------------------------------------
    # Tank context
    # ------------------------------------------------------------------
    @property
    def tank(self) -> dict[str, Any]:
        return self._data["tank"]

    async def async_set_habitat(self, habitat: str | None = None,
                                problem: str | None = None,
                                method: str | None = None) -> None:
        if habitat is not None:
            if habitat not in HABITATS:
                raise ValueError(f"Unknown habitat: {habitat!r}")
            self._data["tank"]["habitat"] = habitat
        if problem is not None:
            if problem not in PROBLEMS:
                raise ValueError(f"Unknown problem: {problem!r}")
            self._data["tank"]["problem"] = problem
        if method is not None:
            if method not in TEST_METHODS:
                raise ValueError(f"Unknown method: {method!r}")
            self._data["tank"]["method"] = method
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_HABITAT_CHANGED)

    @property
    def active_method(self) -> str | None:
        """The currently selected test method, or None if 'Unspecified'."""
        m = self._data["tank"].get("method")
        if not m or m == "Unspecified":
            return None
        return m

    # ------------------------------------------------------------------
    # Readings
    # ------------------------------------------------------------------
    async def async_record_reading(
        self,
        parameter: str,
        value: float,
        *,
        unit: str | None = None,
        method: str | None = None,
        source: str = SOURCE_MANUAL,
        sample_taken_at: str | None = None,
        test_id: str | None = None,
        notes: str | None = None,
    ) -> Reading:
        if source not in (SOURCE_MANUAL, SOURCE_AUTO, SOURCE_ICP):
            raise ValueError(f"Unknown source: {source!r}")
        reading = Reading(
            parameter=parameter,
            value=float(value),
            unit=unit,
            method=method,
            source=source,
            sample_taken_at=sample_taken_at or _now_iso(),
            recorded_at=_now_iso(),
            test_id=test_id,
            notes=notes,
        )
        self._data["readings"].append(reading.as_dict())
        # Cap history if it grows obscene (>50k readings); HA's recorder
        # already keeps state history independently.
        if len(self._data["readings"]) > 50000:
            self._data["readings"] = self._data["readings"][-40000:]
        await self.async_save()
        _LOGGER.info(
            "Recorded %s=%s %s (source=%s, sample=%s)",
            parameter, value, unit or "", source, reading.sample_taken_at,
        )
        # Backfill HA's long-term statistics so the reading shows up in
        # history graphs at its actual sample time. Without this all
        # readings would pile up at "now" because the state machine
        # timestamps state changes with wall-clock time.
        self._import_statistic(reading)
        async_dispatcher_send(self.hass, SIGNAL_READING_RECORDED, parameter)
        return reading

    def _import_statistic(self, reading: Reading) -> None:
        """Write a single reading into HA's long-term statistics table.

        Stat granularity is hourly — multiple readings in the same hour
        for the same parameter collapse to a single bucket (the latest
        write wins). For typical reef-test cadences (< 1 reading per
        parameter per hour), no information is lost.
        """
        if not _STATS_AVAILABLE:
            return
        try:
            dt = datetime.fromisoformat(reading.sample_taken_at)
        except (ValueError, TypeError):
            return
        # Round to the hour — HA stores stats hourly.
        dt = dt.replace(minute=0, second=0, microsecond=0)
        statistic_id = f"sensor.reef_tank_{reading.parameter}_latest"
        metadata = StatisticMetaData(
            has_mean=True,
            has_sum=False,
            name=None,
            source="recorder",  # internal source — same statistic_id as the entity
            statistic_id=statistic_id,
            unit_of_measurement=reading.unit,
        )
        stats = [
            StatisticData(
                start=dt,
                mean=reading.value,
                min=reading.value,
                max=reading.value,
            )
        ]
        try:
            async_import_statistics(self.hass, metadata, stats)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "async_import_statistics failed for %s @ %s: %s",
                statistic_id, dt, exc,
            )

    def latest_reading(self, parameter: str) -> dict[str, Any] | None:
        """Most recent reading for `parameter`, comparing by sample_taken_at.

        A Hanna test taken yesterday beats an ICP sampled three weeks ago
        even if the ICP was imported today.
        """
        candidates = [r for r in self._data["readings"] if r["parameter"] == parameter]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["sample_taken_at"])

    def latest_manual(self, parameter: str) -> dict[str, Any] | None:
        candidates = [
            r for r in self._data["readings"]
            if r["parameter"] == parameter and r["source"] == SOURCE_MANUAL
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["sample_taken_at"])

    def readings_for(self, parameter: str) -> list[dict[str, Any]]:
        return [r for r in self._data["readings"] if r["parameter"] == parameter]

    async def async_backfill_statistics(self, parameter: str | None = None) -> int:
        """Re-import all stored readings into HA's statistics table.

        Use after upgrading to a version that adds statistics support
        for data that was already stored, OR after manually editing
        the storage file. Returns the number of readings imported.

        If `parameter` is given, only that parameter's readings are
        backfilled. Otherwise all parameters are processed.
        """
        if not _STATS_AVAILABLE:
            return 0
        readings = self._data["readings"]
        if parameter:
            readings = [r for r in readings if r["parameter"] == parameter]

        # Group by parameter so we can write each in one call.
        by_param: dict[str, list[dict[str, Any]]] = {}
        for r in readings:
            by_param.setdefault(r["parameter"], []).append(r)

        total = 0
        for pid, rs in by_param.items():
            stats: list[StatisticData] = []
            unit: str | None = None
            for r in rs:
                try:
                    dt = datetime.fromisoformat(r["sample_taken_at"])
                except (ValueError, TypeError, KeyError):
                    continue
                dt = dt.replace(minute=0, second=0, microsecond=0)
                stats.append(StatisticData(
                    start=dt,
                    mean=r["value"],
                    min=r["value"],
                    max=r["value"],
                ))
                if unit is None and r.get("unit"):
                    unit = r["unit"]
            if not stats:
                continue
            metadata = StatisticMetaData(
                has_mean=True, has_sum=False,
                name=None, source="recorder",
                statistic_id=f"sensor.reef_tank_{pid}_latest",
                unit_of_measurement=unit,
            )
            try:
                async_import_statistics(self.hass, metadata, stats)
                total += len(stats)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Backfill failed for %s: %s", pid, exc,
                )
        return total

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    @property
    def inventory(self) -> list[dict[str, Any]]:
        return self._data["inventory"]

    async def async_add_inventory(
        self,
        *,
        category: str,
        name: str,
        type: str | None = None,
        added_at: str | None = None,
        count: int = 1,
        notes: str | None = None,
        photo: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": str(uuid.uuid4()),
            "category": category,
            "type": type,
            "name": name,
            "added_at": added_at or _now_iso()[:10],
            "removed_at": None,
            "count": count,
            "notes": notes,
            "photo": photo,
        }
        self._data["inventory"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_INVENTORY_CHANGED)
        return entry

    async def async_remove_inventory(self, entry_id: str,
                                     removed_at: str | None = None) -> None:
        for entry in self._data["inventory"]:
            if entry["id"] == entry_id:
                entry["removed_at"] = removed_at or _now_iso()[:10]
                await self.async_save()
                async_dispatcher_send(self.hass, SIGNAL_INVENTORY_CHANGED)
                return
        raise ValueError(f"Inventory entry not found: {entry_id}")

    # ------------------------------------------------------------------
    # ICP tests
    # ------------------------------------------------------------------
    async def async_record_icp_test(self, test_record: dict[str, Any]) -> None:
        """Stash a full ICP test record (called from the icpimport companion).

        We dedupe by test_id; a fresh import overwrites a prior one with
        the same id (same lab report).
        """
        test_id = test_record.get("test_id")
        if not test_id:
            raise ValueError("ICP test record requires a test_id")
        self._data["icp_tests"] = [
            t for t in self._data["icp_tests"] if t.get("test_id") != test_id
        ]
        self._data["icp_tests"].append(test_record)
        await self.async_save()

    @property
    def icp_tests(self) -> list[dict[str, Any]]:
        return self._data["icp_tests"]
