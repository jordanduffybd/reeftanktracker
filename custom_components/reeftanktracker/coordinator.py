"""Data coordinator for Reef Tank Tracker.

Owns the on-disk state (`Store`-backed JSON in HA's .storage) and exposes
helpers to record readings, manage inventory, and query "latest known
value" per parameter. Sensors and number entities subscribe to its
state-changed dispatch signal.
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
                                problem: str | None = None) -> None:
        if habitat is not None:
            if habitat not in HABITATS:
                raise ValueError(f"Unknown habitat: {habitat!r}")
            self._data["tank"]["habitat"] = habitat
        if problem is not None:
            if problem not in PROBLEMS:
                raise ValueError(f"Unknown problem: {problem!r}")
            self._data["tank"]["problem"] = problem
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_HABITAT_CHANGED)

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
        async_dispatcher_send(self.hass, SIGNAL_READING_RECORDED, parameter)
        return reading

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
