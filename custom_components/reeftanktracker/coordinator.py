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
import re
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
    EVENT_ADVISOR_ACKNOWLEDGED,
    EVENT_ADVISOR_DISMISSED,
    EVENT_ADVISOR_SNAPSHOT_CAPTURED,
    EVENT_DEMAND_CHANGE_LOGGED,
    EVENT_WATER_CHANGE_LOGGED,
    HABITATS,
    PROBLEMS,
    SIGNAL_ADVISOR_FORM_CHANGED,
    SIGNAL_ADVISOR_UPDATED,
    SIGNAL_HABITAT_CHANGED,
    SIGNAL_ICP_TEST_RECORDED,
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


def _try_log_to_logbook(
    hass: HomeAssistant, name: str, message: str, entity_id: str | None,
) -> None:
    """Best-effort logbook entry via the public `logbook_entry` event.

    The logbook component listens for this event and renders a
    chronological entry visible at /logbook. We fire the event rather
    than calling logbook's helper API directly because the helper's
    import path has shifted across HA versions; the event is stable.
    """
    try:
        payload: dict[str, Any] = {
            "name": name,
            "message": message,
            "domain": DOMAIN,
        }
        if entity_id:
            payload["entity_id"] = entity_id
        hass.bus.async_fire("logbook_entry", payload)
    except Exception:  # noqa: BLE001
        pass


def _slugify(label: str) -> str:
    """ASCII slug: lowercase, non-alnum runs → _, strip leading/trailing _."""
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return s or "supplement"


def _unique_slug(label: str, taken: set[str]) -> str:
    """Slug from label with `_2`, `_3`, … suffix on collision."""
    base = _slugify(label)
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


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
        # Advisor config from Options flow (window_days, target band, etc.).
        # Populated at setup; sensors read this to construct AdvisorConfig.
        self._advisor_config: dict[str, Any] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        self._data = loaded or self._default_data()
        # Backfill any keys that may be missing on upgrade.
        defaults = self._default_data()
        for k, v in defaults.items():
            self._data.setdefault(k, v)
        # Advisor blob has nested per-parameter structure — backfill those
        # too so existing installs upgrade cleanly.
        advisor_default = defaults["advisor"]
        advisor_current = self._data.setdefault("advisor", {})
        for param_id, param_default in advisor_default.items():
            param_current = advisor_current.setdefault(param_id, {})
            for k, v in param_default.items():
                param_current.setdefault(k, v)

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
            # Advisor state per parameter — daily snapshots and user events
            # (acknowledgments, dismissals, demand-changes). The algorithm
            # is in advisor.py; this is just persistent storage.
            "advisor": {
                "kh": {
                    "snapshots": [],         # [{at, kh, dose_mL}]
                    "acknowledgments": [],   # [{at, applied_value_mL, prev_value_mL}]
                    "dismissals": [],        # [{at, suggested_value_mL}]
                    "demand_changes": [],    # [{at, reason, expected_direction, magnitude_hint_pct}]
                    "water_changes": [],     # [{at, percent, salt_mix_kh, notes}]
                },
                # Persisted state of the dashboard's inline form fields,
                # so values survive HA restarts and dashboard reloads.
                "form": {
                    "wc_percent": 10.0,
                    "wc_salt_mix_kh": None,
                    "wc_notes": "",
                    "demand_reason": "",
                    "demand_direction": "unknown",
                    "demand_magnitude_pct": None,
                },
            },
            # User-added supplement profiles. Builtins live in
            # alk_advisor.BUILTIN_PROFILES; these merge on top at runtime.
            "supplement_profiles": [],
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
    # Advisor config (set by __init__ from entry.options)
    # ------------------------------------------------------------------
    def set_advisor_config(self, cfg: dict[str, Any]) -> None:
        self._advisor_config = dict(cfg or {})

    def get_advisor_config(self) -> dict[str, Any]:
        return dict(self._advisor_config)

    def get_target_range(
        self, param_id: str,
    ) -> tuple[float | None, float | None]:
        """Return the (min, max) target range for a parameter.

        User overrides from the Options-flow "Target ranges" page win;
        otherwise we fall back to the static `default_target_min/max`
        in parameters.py. Returns (None, None) if neither is set
        (e.g. for ICP-only params with no default and no override).

        The override keys live alongside advisor settings under
        `target_<param_id>_min` and `target_<param_id>_max`. We read
        from `_advisor_config` because that's already populated from
        `entry.options` at setup; no second config wiring needed.
        """
        from .parameters import get_parameter
        cfg = self._advisor_config
        ovr_min = cfg.get(f"target_{param_id}_min")
        ovr_max = cfg.get(f"target_{param_id}_max")
        if ovr_min is not None and ovr_max is not None:
            try:
                return float(ovr_min), float(ovr_max)
            except (TypeError, ValueError):
                pass  # fall through to defaults
        param = get_parameter(param_id)
        if param is None:
            return (None, None)
        return (
            param.get("default_target_min"),
            param.get("default_target_max"),
        )

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

    def _resolve_advisor_entity_id(self) -> str | None:
        """Find the actual entity_id for the alk advisor sensor in the
        registry. Used so logbook entries about user actions link to
        the sensor's history view."""
        try:
            registry = er.async_get(self.hass)
        except Exception:  # noqa: BLE001
            return None
        target_uid = "reef_alk_advisor_recommendation"
        for entry in registry.entities.values():
            if entry.platform == DOMAIN and entry.unique_id == target_uid:
                return entry.entity_id
        return None

    def _emit_advisor_event(
        self, event_name: str, message: str, payload: dict[str, Any],
    ) -> None:
        """Fire an event-bus event AND log a logbook entry for a user-
        initiated advisor action. Both happen best-effort — failures are
        swallowed so a logbook hiccup never blocks the underlying state
        update."""
        advisor_eid = self._resolve_advisor_entity_id()
        _try_log_to_logbook(self.hass, "Alk Advisor", message, advisor_eid)
        try:
            self.hass.bus.async_fire(event_name, payload)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_latest_entity_id(self, parameter: str) -> str | None:
        """Find the actual entity_id for `sensor.<...>_latest` in the
        registry, no matter which version of the integration first
        registered it. Returns None if the entity doesn't exist (e.g.
        ICP-only parameters where no sensor was created).
        """
        try:
            registry = er.async_get(self.hass)
        except Exception:  # noqa: BLE001
            return None
        unique_id = f"reef_{parameter}_latest"
        for entry in registry.entities.values():
            if entry.platform == DOMAIN and entry.unique_id == unique_id:
                return entry.entity_id
        return None

    def _import_statistic(self, reading: Reading) -> None:
        """Write a single reading into HA's long-term statistics table.

        Stat granularity is hourly — multiple readings in the same hour
        for the same parameter collapse to a single bucket (the latest
        write wins). For typical reef-test cadences (< 1 reading per
        parameter per hour), no information is lost.

        Looks up the actual entity_id from the registry so this works
        regardless of which version first registered the entities.
        """
        if not _STATS_AVAILABLE:
            return
        try:
            dt = datetime.fromisoformat(reading.sample_taken_at)
        except (ValueError, TypeError):
            return
        statistic_id = self._resolve_latest_entity_id(reading.parameter)
        if not statistic_id:
            _LOGGER.debug(
                "No registered entity for parameter %r — skipping stats import",
                reading.parameter,
            )
            return
        # Round to the hour — HA stores stats hourly.
        dt = dt.replace(minute=0, second=0, microsecond=0)
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
            _LOGGER.warning(
                "Backfill skipped — recorder/statistics module not available"
            )
            return 0
        readings = self._data["readings"]
        if parameter:
            readings = [r for r in readings if r["parameter"] == parameter]

        _LOGGER.warning(
            "Backfill: processing %d stored readings (parameter filter: %s)",
            len(readings), parameter or "all",
        )

        # Group by parameter so we can write each in one call.
        by_param: dict[str, list[dict[str, Any]]] = {}
        for r in readings:
            by_param.setdefault(r["parameter"], []).append(r)

        total = 0
        skipped_no_entity: list[str] = []
        for pid, rs in by_param.items():
            statistic_id = self._resolve_latest_entity_id(pid)
            if not statistic_id:
                skipped_no_entity.append(f"{pid} ({len(rs)} readings)")
                continue

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
                statistic_id=statistic_id,
                unit_of_measurement=unit,
            )
            try:
                async_import_statistics(self.hass, metadata, stats)
                total += len(stats)
                _LOGGER.warning(
                    "Backfill: wrote %d points to %s", len(stats), statistic_id,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Backfill failed for %s (%s): %s",
                    pid, statistic_id, exc, exc_info=True,
                )

        if skipped_no_entity:
            _LOGGER.warning(
                "Backfill: skipped (no registered entity): %s",
                ", ".join(skipped_no_entity),
            )
        _LOGGER.warning("Backfill complete: %d points written", total)
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
        async_dispatcher_send(self.hass, SIGNAL_ICP_TEST_RECORDED)

    @property
    def icp_tests(self) -> list[dict[str, Any]]:
        return self._data["icp_tests"]

    @property
    def latest_icp_test(self) -> dict[str, Any] | None:
        """Most recently imported ICP test (by `imported_at`)."""
        tests = self._data.get("icp_tests") or []
        if not tests:
            return None
        return max(tests, key=lambda t: t.get("imported_at") or "")

    # ------------------------------------------------------------------
    # Advisor state (per-parameter — currently only KH is implemented)
    # ------------------------------------------------------------------
    def _advisor_blob(self, param_id: str) -> dict[str, Any]:
        """Get-or-create the per-parameter advisor sub-dict, backfilling
        any sub-keys that may be missing on upgrade."""
        advisor = self._data.setdefault("advisor", {})
        blob = advisor.setdefault(param_id, {})
        for k in (
            "snapshots", "acknowledgments", "dismissals",
            "demand_changes", "water_changes",
        ):
            blob.setdefault(k, [])
        return blob

    def advisor_snapshots(self, param_id: str) -> list[dict[str, Any]]:
        return list(self._advisor_blob(param_id)["snapshots"])

    def advisor_acknowledgments(self, param_id: str) -> list[dict[str, Any]]:
        return list(self._advisor_blob(param_id)["acknowledgments"])

    def advisor_dismissals(self, param_id: str) -> list[dict[str, Any]]:
        return list(self._advisor_blob(param_id)["dismissals"])

    def advisor_demand_changes(self, param_id: str) -> list[dict[str, Any]]:
        return list(self._advisor_blob(param_id)["demand_changes"])

    def advisor_water_changes(self, param_id: str) -> list[dict[str, Any]]:
        return list(self._advisor_blob(param_id)["water_changes"])

    async def async_record_advisor_snapshot(
        self, param_id: str, *, at: str, kh: float | None,
        dose_mL: float | None,
    ) -> None:
        """Append a daily snapshot. Caps history at 90 entries (rolling)."""
        blob = self._advisor_blob(param_id)
        entry = {"at": at, "kh": kh, "dose_mL": dose_mL}
        blob["snapshots"].append(entry)
        # Keep ~3 months — the algorithm only looks at `window_days`
        # but extra history is cheap and useful for diagnostics.
        if len(blob["snapshots"]) > 90:
            blob["snapshots"] = blob["snapshots"][-90:]
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        # Bus event only — daily cadence would clutter the logbook.
        try:
            self.hass.bus.async_fire(
                EVENT_ADVISOR_SNAPSHOT_CAPTURED,
                {"parameter": param_id, **entry},
            )
        except Exception:  # noqa: BLE001
            pass

    async def async_record_advisor_acknowledgment(
        self, param_id: str, *, applied_value_mL: float, prev_value_mL: float,
    ) -> None:
        blob = self._advisor_blob(param_id)
        entry = {
            "at": _now_iso(),
            "applied_value_mL": float(applied_value_mL),
            "prev_value_mL": float(prev_value_mL),
        }
        blob["acknowledgments"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        self._emit_advisor_event(
            EVENT_ADVISOR_ACKNOWLEDGED,
            f"Acknowledged: applied {applied_value_mL} mL/day "
            f"(was {prev_value_mL} mL/day)",
            {"parameter": param_id, **entry},
        )

    async def async_record_advisor_dismissal(
        self, param_id: str, *, suggested_value_mL: float,
    ) -> None:
        blob = self._advisor_blob(param_id)
        entry = {
            "at": _now_iso(),
            "suggested_value_mL": float(suggested_value_mL),
        }
        blob["dismissals"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        self._emit_advisor_event(
            EVENT_ADVISOR_DISMISSED,
            f"Dismissed suggestion of {suggested_value_mL} mL/day",
            {"parameter": param_id, **entry},
        )

    async def async_record_advisor_demand_change(
        self, param_id: str, *, reason: str,
        expected_direction: str = "unknown",
        magnitude_hint_pct: float | None = None,
    ) -> None:
        blob = self._advisor_blob(param_id)
        entry = {
            "at": _now_iso(),
            "reason": reason,
            "expected_direction": expected_direction,
            "magnitude_hint_pct": magnitude_hint_pct,
        }
        blob["demand_changes"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        msg = f"Demand change: {reason!r} (expected {expected_direction})"
        if magnitude_hint_pct is not None:
            msg += f", hint {magnitude_hint_pct:.0f}%"
        self._emit_advisor_event(
            EVENT_DEMAND_CHANGE_LOGGED,
            msg,
            {"parameter": param_id, **entry},
        )

    async def async_record_water_change(
        self, param_id: str, *, percent: float,
        salt_mix_kh: float | None = None, notes: str | None = None,
    ) -> None:
        blob = self._advisor_blob(param_id)
        entry = {
            "at": _now_iso(),
            "percent": float(percent),
            "salt_mix_kh": float(salt_mix_kh) if salt_mix_kh is not None else None,
            "notes": notes,
        }
        blob["water_changes"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        msg = f"Water change logged: {percent:.1f}%"
        if salt_mix_kh is not None:
            msg += f" (salt mix KH {salt_mix_kh:.2f} dKH)"
        if notes:
            msg += f" — {notes}"
        self._emit_advisor_event(
            EVENT_WATER_CHANGE_LOGGED,
            msg,
            {"parameter": param_id, **entry},
        )

    # ------------------------------------------------------------------
    # Supplement profiles (user-managed; merge with alk_advisor builtins)
    # ------------------------------------------------------------------
    @property
    def supplement_profiles(self) -> list[dict[str, Any]]:
        return self._data.setdefault("supplement_profiles", [])

    async def async_add_supplement_profile(
        self, *, label: str, eff_dkh_per_mL_per_100L: float | None = None,
        param_id: str | list[str] = "kh",
        label_patterns: list[str] | None = None, notes: str | None = None,
    ) -> dict[str, Any]:
        # Lazy import keeps coordinator import-time light and avoids a
        # cycle (alk_advisor imports coordinator types).
        from .alk_advisor import BUILTIN_PROFILES
        existing = self.supplement_profiles
        taken = set(BUILTIN_PROFILES) | {p["id"] for p in existing}
        # Always store param_id as a list internally — caller can pass
        # a string for single-target supplements (the common case) or
        # a list for multi-target supplements like Red Sea NO3:PO4-X
        # which targets nitrate AND phosphate simultaneously. The
        # `supplement_profiles_for(param_id)` helper checks membership
        # so a multi-target supplement surfaces in every per-element
        # advisor whose parameter it affects.
        pids = [param_id] if isinstance(param_id, str) else list(param_id)
        if not pids:
            pids = ["kh"]
        entry: dict[str, Any] = {
            "id": _unique_slug(label, taken),
            "label": label,
            # Stored as float when present, None for non-KH supplements
            # whose potency the alk advisor never reads. Per-element
            # advisors (0.5.0+) will introduce parameter-aware potency
            # fields on profiles; for now this stays KH-shaped.
            "eff_dkh_per_mL_per_100L": (
                float(eff_dkh_per_mL_per_100L)
                if eff_dkh_per_mL_per_100L is not None else None
            ),
            "param_id": pids,
            "label_patterns": [p.lower() for p in (label_patterns or [])],
            "created_at": _now_iso(),
            "notes": notes,
        }
        existing.append(entry)
        await self.async_save()
        # Dispatch on each target parameter so per-element advisors
        # (0.5.0+) only recompute when their own supplements change.
        # Single-target adds fire one dispatch; multi-target fires once
        # per param.
        for pid in pids:
            async_dispatcher_send(
                self.hass, SIGNAL_ADVISOR_UPDATED, pid,
            )
        return entry

    def supplement_profiles_for(self, param_id: str) -> list[dict[str, Any]]:
        """Return user-added supplement profiles that include `param_id`
        in their target parameter list.

        Profiles registered before the param_id field existed default
        to "kh". Profiles registered with a single string (the common
        case) match if the string equals `param_id`. Multi-target
        profiles like Red Sea NO3:PO4-X (registered with
        param_id=["nitrate", "phosphate"]) match if `param_id` is in
        the list — they surface in BOTH the nitrate and phosphate
        per-element advisors (0.5.0+).
        """
        out: list[dict[str, Any]] = []
        for p in self.supplement_profiles:
            stored = p.get("param_id", "kh")
            pids = [stored] if isinstance(stored, str) else list(stored)
            if param_id in pids:
                out.append(p)
        return out

    async def async_remove_supplement_profile(self, profile_id: str) -> None:
        existing = self.supplement_profiles
        for i, p in enumerate(existing):
            if p["id"] == profile_id:
                del existing[i]
                await self.async_save()
                async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, "kh")
                return
        raise ValueError(f"Supplement profile not found: {profile_id}")

    # ------------------------------------------------------------------
    # Advisor form state — persists the dashboard inline-form values so
    # the user's typed inputs survive reloads.
    # ------------------------------------------------------------------
    def _form_blob(self) -> dict[str, Any]:
        advisor = self._data.setdefault("advisor", {})
        form = advisor.setdefault("form", {})
        # Backfill defaults for any missing keys (handles upgrades).
        form.setdefault("wc_percent", 10.0)
        form.setdefault("wc_salt_mix_kh", None)
        form.setdefault("wc_notes", "")
        form.setdefault("demand_reason", "")
        form.setdefault("demand_direction", "unknown")
        form.setdefault("demand_magnitude_pct", None)
        return form

    def advisor_form_value(self, key: str) -> Any:
        return self._form_blob().get(key)

    async def async_set_advisor_form_value(self, key: str, value: Any) -> None:
        # Always persist, even if the new value matches in-memory. An
        # earlier same-value early-return was masking a bug where
        # in-memory state drifted from disk (a service call would no-op
        # because the in-memory form had the right value but the file
        # didn't). Save is cheap; correctness wins.
        form = self._form_blob()
        form[key] = value
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_FORM_CHANGED, key)
