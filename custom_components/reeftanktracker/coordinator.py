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
from datetime import datetime, timedelta, timezone
from typing import Any
import logging
import re
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    EVENT_CALIBRATION_RECORDED,
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


class _Unset:
    """Sentinel for `update_supplement_profile` to distinguish "leave
    this field alone" (param missing from the call) from "clear this
    field to None" (param explicitly set to None)."""


_UNSET = _Unset()


@dataclass
class Reading:
    """One recorded parameter reading.

    `sample_taken_at` is when the water was actually sampled (canonical for
    history). `recorded_at` is when the bridge / user logged it (might be
    weeks later for ICP imports).

    `excluded_at` + `excluded_reason` mark a reading as ignored (e.g. sensor
    malfunction). Excluded readings stay in storage for audit but are
    skipped by advisor, snapshot, drift, and statistics consumers — see
    `_is_excluded()` helper. Both fields are None for normal readings.
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
    excluded_at: str | None = None    # ISO 8601, set by ignore_readings
    excluded_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
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
        # Only persist exclusion fields when set — keeps existing
        # readings' on-disk shape unchanged and storage compact.
        if self.excluded_at is not None:
            d["excluded_at"] = self.excluded_at
        if self.excluded_reason is not None:
            d["excluded_reason"] = self.excluded_reason
        return d


def _is_excluded(item: dict[str, Any]) -> bool:
    """Return True if a reading or snapshot dict has been ignored.

    Centralises the predicate so every consumer site uses the same check.
    A reading is excluded iff `excluded_at` is a non-empty string —
    `excluded_reason` is informational only.
    """
    return bool(item.get("excluded_at"))


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
                    # Calibration events from the KH Keeper bridge — every
                    # time the device's `adjustment` offset changes, the
                    # bridge publishes one event and reeftank records it
                    # here. Each entry: {at, prev, new, delta, source,
                    # hanna_value, serial}. Used to: (a) skip snapshots
                    # in a settling window around the event so the alk
                    # advisor doesn't attribute the resulting KH step-
                    # change to dose, and (b) trigger an empirical-
                    # potency reset (the step contaminates the slope
                    # derivation used by the empirical learner).
                    "calibration_events": [],
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
        # For parameters with a per-element advisor (Ca / Mg / NO3 / PO4 —
        # see param_advisor.PARAM_DEFAULTS), each manual reading also
        # captures an advisor snapshot. This is the equivalent of the alk
        # advisor's daily 23:55 auto-snapshot path, but for parameters
        # that have no auto-source sensor (until Mastertronic Essential
        # arrives). Snapshots are param-keyed in storage so they don't
        # collide with KH's auto-captured ones.
        await self._maybe_snapshot_for_advisor(
            parameter, value, reading.sample_taken_at,
        )
        return reading

    async def _maybe_snapshot_for_advisor(
        self, parameter: str, value: float, sample_taken_at: str,
    ) -> None:
        """If `parameter` has a per-element advisor configured (i.e. is
        listed in `param_advisor.PARAM_DEFAULTS`), record a snapshot
        with the current dose summed across the configured heads.

        Best-effort: failures don't propagate to the caller (a snapshot
        write should never break a `record_reading` call). Lazy import
        breaks the circular dependency between coordinator and
        param_advisor.
        """
        try:
            from . import param_advisor
            if parameter not in param_advisor.PARAM_DEFAULTS:
                return
            heads_key = param_advisor.opt_key(parameter, "heads")
            heads = list(self._advisor_config.get(heads_key) or [])
            dose_mL = param_advisor._sum_dose_mL(self.hass, heads)
            await self.async_record_advisor_snapshot(
                parameter,
                at=sample_taken_at,
                kh=float(value),
                dose_mL=dose_mL,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Snapshot-on-reading failed for parameter=%r: %s",
                parameter, exc,
            )

    async def async_backfill_advisor_snapshots(
        self, param_id: str, *, days: int = 90,
    ) -> int:
        """Synthesize advisor snapshots from existing Reading records
        for `param_id`, going back `days` days. Idempotent — skips
        readings whose sample timestamp already has a snapshot.

        Solves the cold-start problem: when the user enables a per-
        element advisor for a parameter they've already been tracking
        manually, the snapshot blob is empty (snapshots only fire for
        readings recorded AFTER the advisor was added in 0.5.0+). This
        method backfills from the historical Reading records so the
        advisor can produce a recommendation immediately.

        For each unique sample DATE in the readings (one snapshot per
        day max — multiple readings same day get coalesced via mean),
        appends a snapshot. `dose_mL` is set to the current sum across
        configured heads — the algorithm tolerates `dose_mL=None` for
        historical days (slope/median calculations skip None entries),
        so this approximation is fine.

        Returns the count of snapshots added.
        """
        from . import param_advisor
        if param_id not in param_advisor.PARAM_DEFAULTS:
            return 0

        readings = self.readings_for(param_id)
        if not readings:
            return 0

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        recent = [
            r for r in readings if (r.get("sample_taken_at") or "") >= cutoff
        ]
        # Group by DATE (YYYY-MM-DD) — one snapshot per day max
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in recent:
            sat = r.get("sample_taken_at") or ""
            date_key = sat[:10]
            if not date_key:
                continue
            by_date.setdefault(date_key, []).append(r)

        # Find existing snapshot dates so we don't duplicate
        existing_dates = {
            (s.get("at") or "")[:10]
            for s in self.advisor_snapshots(param_id)
        }

        added = 0
        for date_key in sorted(by_date):
            if date_key in existing_dates:
                continue
            day_readings = by_date[date_key]
            # Average value across same-day readings
            values = [
                float(r["value"]) for r in day_readings
                if r.get("value") is not None
            ]
            if not values:
                continue
            avg_value = sum(values) / len(values)
            # Use the first reading's sample_taken_at as the snapshot
            # timestamp (close to the reading time, not synthesised
            # midnight).
            first_at = day_readings[0].get("sample_taken_at")
            await self.async_record_advisor_snapshot(
                param_id,
                at=first_at or f"{date_key}T08:00:00+00:00",
                kh=round(avg_value, 4),
                dose_mL=None,  # historical — current dose isn't representative
            )
            added += 1
        if added:
            _LOGGER.info(
                "Backfilled %d snapshot(s) for advisor param=%s "
                "from existing Reading history (looked back %d days)",
                added, param_id, days,
            )
        return added

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
        # The recorder owns the present; we own the past.
        #
        # `ReefLatestSensor` carries `state_class = measurement`, so HA's
        # recorder already compiles an hourly statistic for this entity from
        # its live state. Importing into the same statistic_id for the same
        # hour makes two writers race for one row and the insert is rejected:
        #     UNIQUE constraint failed:
        #       statistics.metadata_id, statistics.start_ts
        # which HA 2026.x surfaces as an hourly "Blocked attempt to insert
        # duplicated statistic rows" ERROR. The import exists solely so that
        # historical readings (CSV/ICP imports with old sample dates) land at
        # their true sample time instead of piling up at "now" — for a
        # just-taken reading the recorder's own value is already correct and
        # our import adds nothing. So skip anything the recorder is actively
        # covering.
        if not self._is_backfillable(dt):
            _LOGGER.debug(
                "Reading for %s sampled at %s is inside the recorder's live "
                "window — leaving statistics to the recorder",
                statistic_id, dt,
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

    def _is_backfillable(self, sample_at: datetime) -> bool:
        """True when `sample_at` is old enough that the recorder is not
        already compiling statistics for that hour from live state.

        Anything newer than `LIVE_STATS_WINDOW` belongs to the recorder —
        see the long comment in `_import_statistic` for why writing there
        produces UNIQUE-constraint errors. The window is deliberately
        generous (a couple of hours rather than one) because recorder
        compilation lags the hour boundary and clocks drift.
        """
        if sample_at.tzinfo is None:
            sample_at = sample_at.replace(tzinfo=timezone.utc)
        return sample_at < (
            datetime.now(timezone.utc) - self.LIVE_STATS_WINDOW
        )

    def latest_reading(
        self, parameter: str, *, include_excluded: bool = False,
    ) -> dict[str, Any] | None:
        """Most recent reading for `parameter`, comparing by sample_taken_at.

        A Hanna test taken yesterday beats an ICP sampled three weeks ago
        even if the ICP was imported today. Excluded readings (marked via
        `ignore_readings`) are skipped by default.
        """
        candidates = [
            r for r in self._data["readings"]
            if r["parameter"] == parameter
            and (include_excluded or not _is_excluded(r))
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["sample_taken_at"])

    def latest_manual(
        self, parameter: str, *, include_excluded: bool = False,
    ) -> dict[str, Any] | None:
        candidates = [
            r for r in self._data["readings"]
            if r["parameter"] == parameter
            and r["source"] == SOURCE_MANUAL
            and (include_excluded or not _is_excluded(r))
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["sample_taken_at"])

    def readings_for(
        self, parameter: str, *, include_excluded: bool = False,
    ) -> list[dict[str, Any]]:
        """All readings for `parameter`. Excluded readings are skipped
        unless `include_excluded=True` (e.g. for the dashboard timeline
        which wants to show them with a strikethrough)."""
        return [
            r for r in self._data["readings"]
            if r["parameter"] == parameter
            and (include_excluded or not _is_excluded(r))
        ]

    async def async_ignore_readings(
        self,
        parameter: str,
        *,
        from_iso: str,
        to_iso: str,
        reason: str,
        source: str | None = None,
    ) -> dict[str, int]:
        """Mark all readings for `parameter` with
        sample_taken_at in [from_iso, to_iso] as excluded.

        Also marks the corresponding advisor snapshots for the same
        parameter and window — the advisor reads snapshots not raw
        readings, so without this the bad data would still drive
        recommendations even after the raw readings are ignored.

        `source` (optional) restricts to one of "manual" | "auto" |
        "icp" — e.g. ignore only the bad KH-Keeper auto readings
        without touching manual Salifert backups in the same window.

        Returns a dict like
        ``{"readings": N, "snapshots": M}`` of items affected.
        Idempotent: re-running on already-excluded items is a no-op
        for those items but will re-apply to any items that were
        added to the window since the last call.
        """
        if from_iso > to_iso:
            raise ValueError(
                f"from_iso ({from_iso!r}) must be <= to_iso ({to_iso!r})"
            )
        if source is not None and source not in (
            SOURCE_MANUAL, SOURCE_AUTO, SOURCE_ICP,
        ):
            raise ValueError(f"Unknown source filter: {source!r}")
        if not reason or not reason.strip():
            raise ValueError("reason is required (non-empty)")

        marked_at = _now_iso()
        readings_affected = 0
        for r in self._data["readings"]:
            if r["parameter"] != parameter:
                continue
            sat = r.get("sample_taken_at") or ""
            if not (from_iso <= sat <= to_iso):
                continue
            if source is not None and r.get("source") != source:
                continue
            if _is_excluded(r):
                continue  # don't overwrite an existing exclusion
            r["excluded_at"] = marked_at
            r["excluded_reason"] = reason
            readings_affected += 1

        # Sweep advisor snapshots in the same window. Snapshots store
        # their time in `at` (the snapshot tick timestamp), not
        # sample_taken_at — daily snapshotter writes one at the
        # configured local time (default 23:55).
        snapshots_affected = 0
        try:
            blob = self._advisor_blob(parameter)
        except Exception:  # noqa: BLE001
            blob = None
        if blob is not None:
            for s in blob.get("snapshots", []):
                sat = s.get("at") or ""
                if not (from_iso <= sat <= to_iso):
                    continue
                if _is_excluded(s):
                    continue
                s["excluded_at"] = marked_at
                s["excluded_reason"] = reason
                snapshots_affected += 1

        if readings_affected or snapshots_affected:
            await self.async_save()
            async_dispatcher_send(
                self.hass, SIGNAL_READING_RECORDED, parameter,
            )
            async_dispatcher_send(
                self.hass, SIGNAL_ADVISOR_UPDATED, parameter,
            )

        _LOGGER.info(
            "Ignored %d %s reading(s) and %d snapshot(s) in [%s, %s] "
            "(reason: %s)",
            readings_affected, parameter, snapshots_affected,
            from_iso, to_iso, reason,
        )
        return {
            "readings": readings_affected,
            "snapshots": snapshots_affected,
        }

    async def async_unignore_readings(
        self,
        parameter: str,
        *,
        from_iso: str,
        to_iso: str,
    ) -> dict[str, int]:
        """Reverse of `async_ignore_readings`: clears the
        `excluded_at` + `excluded_reason` fields from any readings
        and snapshots in the window. Returns affected counts."""
        if from_iso > to_iso:
            raise ValueError(
                f"from_iso ({from_iso!r}) must be <= to_iso ({to_iso!r})"
            )

        readings_affected = 0
        for r in self._data["readings"]:
            if r["parameter"] != parameter:
                continue
            sat = r.get("sample_taken_at") or ""
            if not (from_iso <= sat <= to_iso):
                continue
            if not _is_excluded(r):
                continue
            r.pop("excluded_at", None)
            r.pop("excluded_reason", None)
            readings_affected += 1

        snapshots_affected = 0
        try:
            blob = self._advisor_blob(parameter)
        except Exception:  # noqa: BLE001
            blob = None
        if blob is not None:
            for s in blob.get("snapshots", []):
                sat = s.get("at") or ""
                if not (from_iso <= sat <= to_iso):
                    continue
                if not _is_excluded(s):
                    continue
                s.pop("excluded_at", None)
                s.pop("excluded_reason", None)
                snapshots_affected += 1

        if readings_affected or snapshots_affected:
            await self.async_save()
            async_dispatcher_send(
                self.hass, SIGNAL_READING_RECORDED, parameter,
            )
            async_dispatcher_send(
                self.hass, SIGNAL_ADVISOR_UPDATED, parameter,
            )

        _LOGGER.info(
            "Unignored %d %s reading(s) and %d snapshot(s) in [%s, %s]",
            readings_affected, parameter, snapshots_affected,
            from_iso, to_iso,
        )
        return {
            "readings": readings_affected,
            "snapshots": snapshots_affected,
        }

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
        readings = [r for r in self._data["readings"] if not _is_excluded(r)]
        if parameter:
            readings = [r for r in readings if r["parameter"] == parameter]

        _LOGGER.warning(
            "Backfill: processing %d stored readings (parameter filter: %s, "
            "excluded readings skipped)",
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
            skipped_live = 0
            for r in rs:
                try:
                    dt = datetime.fromisoformat(r["sample_taken_at"])
                except (ValueError, TypeError, KeyError):
                    continue
                # Same rule as `_import_statistic` — never write into hours
                # the recorder is already compiling from live state, or every
                # backfill run re-triggers the duplicate-row errors.
                if not self._is_backfillable(dt):
                    skipped_live += 1
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
            if skipped_live:
                _LOGGER.warning(
                    "Backfill: %s — skipped %d reading(s) inside the "
                    "recorder's live window (already covered by the "
                    "recorder's own statistics)", pid, skipped_live,
                )
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

    def advisor_snapshots(
        self, param_id: str, *, include_excluded: bool = False,
    ) -> list[dict[str, Any]]:
        """Daily snapshots for `param_id`. Excluded snapshots (set by
        `ignore_readings`, which also sweeps snapshots in the same
        window) are skipped by default — the advisor recommendation
        path must NEVER see them or it'd compute medians from data the
        user explicitly invalidated."""
        return [
            s for s in self._advisor_blob(param_id)["snapshots"]
            if include_excluded or not _is_excluded(s)
        ]

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
        kh_kind: str | None = None,
        kh_samples: int | None = None,
    ) -> None:
        """Append a daily snapshot. Caps history at 90 entries (rolling).

        `kh_kind` ("manual" | "auto" | "live") and `kh_samples` record how
        the day's value was derived — see `alk_advisor._daily_value`. Both
        are optional and omitted from the entry when None, so snapshots
        written by older versions stay valid and the algorithm (which only
        reads `at`/`kh`/`dose_mL`) is unaffected.
        """
        blob = self._advisor_blob(param_id)
        entry: dict[str, Any] = {"at": at, "kh": kh, "dose_mL": dose_mL}
        if kh_kind is not None:
            entry["kh_kind"] = kh_kind
        if kh_samples is not None:
            entry["kh_samples"] = kh_samples
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
        implicit: bool = False,
        suggested_value_mL: float | None = None,
        tolerance_pct: float | None = None,
    ) -> None:
        """Record an acknowledgment.

        `implicit=True` flags acks inferred from the user changing the
        doser's `_daily_dose` entity directly (e.g. in the ReefBeat app)
        rather than clicking the dashboard's Acknowledge button. Stored
        so empirical-potency learning can weight implicit acks vs
        explicit ones — and so the dashboard can show "(auto-detected)"
        next to recent implicit acks. `suggested_value_mL` /
        `tolerance_pct` are diagnostic for implicit acks; explicit
        button presses leave them None.
        """
        blob = self._advisor_blob(param_id)
        entry = {
            "at": _now_iso(),
            "applied_value_mL": float(applied_value_mL),
            "prev_value_mL": float(prev_value_mL),
        }
        if implicit:
            entry["implicit"] = True
            if suggested_value_mL is not None:
                entry["suggested_value_mL"] = float(suggested_value_mL)
            if tolerance_pct is not None:
                entry["tolerance_pct"] = float(tolerance_pct)
        blob["acknowledgments"].append(entry)
        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, param_id)
        msg = (
            f"Acknowledged{' (auto-detected)' if implicit else ''}: "
            f"applied {applied_value_mL} mL/day "
            f"(was {prev_value_mL} mL/day)"
        )
        self._emit_advisor_event(
            EVENT_ADVISOR_ACKNOWLEDGED,
            msg,
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

    # ------------------------------------------------------------------
    # KH Keeper calibration events
    # ------------------------------------------------------------------
    # Window around a calibration event where alk snapshots are excluded
    # from the empirical-potency slope derivation. A calibration changes
    # the displayed KH by Δadjustment instantly — if the advisor sees that
    # step-change in a snapshot, it'll attribute the entire jump to dose
    # and explode the empirical potency. 24h either side is enough for
    # one full daily measurement cadence to settle.
    CALIBRATION_SETTLING_HOURS = 24.0

    # Readings sampled inside this window are left entirely to HA's recorder,
    # which already compiles hourly statistics for the `*_latest` entities
    # (they carry `state_class = measurement`). Importing there collides on
    # (metadata_id, start_ts). See `_import_statistic`.
    LIVE_STATS_WINDOW = timedelta(hours=2)

    async def async_record_calibration_event(self, event: dict[str, Any]) -> None:
        """Record a KH Keeper calibration event under advisor.kh and,
        if a Hanna value was supplied, also record it as a manual KH
        reading. Idempotent — events from the retained `last_calibration`
        topic on startup are de-duped by (serial, ts).

        Expected event shape (from kh-keeper-bridge 0.1.15+):
            {prev, new, delta, ts, source, hanna_value, serial}
        """
        ts = event.get("ts") or _now_iso()
        serial = event.get("serial") or "unknown"
        entry = {
            "at": ts,
            "prev": event.get("prev"),
            "new": event.get("new"),
            "delta": event.get("delta"),
            "source": event.get("source") or "device",
            "hanna_value": event.get("hanna_value"),
            "serial": serial,
        }
        blob = self._advisor_blob("kh")
        events_list = blob.setdefault("calibration_events", [])
        # De-dupe: retained MQTT topic re-fires on every restart, so if
        # we already have an event with the same (serial, ts) skip the
        # entire record path (including the Hanna reading).
        for existing in events_list:
            if existing.get("serial") == serial and existing.get("at") == ts:
                _LOGGER.debug(
                    "Calibration event %s/%s already recorded; skipping", serial, ts,
                )
                return
        events_list.append(entry)
        await self.async_save()

        # The Hanna value is the manual ground-truth the user measured
        # to drive the calibration — record it as a manual KH reading so
        # the variance-aware reading picker has a high-confidence point.
        # Only `ha_drop_test`-sourced events carry one; device-side
        # calibrations don't expose the user's actual test reading.
        hanna = entry["hanna_value"]
        if hanna is not None:
            try:
                await self.async_record_reading(
                    parameter="kh",
                    value=float(hanna),
                    unit="dKH",
                    method="Hanna ULR (KH Keeper drop test)",
                    source=SOURCE_MANUAL,
                    sample_taken_at=ts,
                    notes=(
                        f"Auto-recorded from KH Keeper calibration "
                        f"(adjustment {entry['prev']:+.2f} → {entry['new']:+.2f})"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Calibration event recorded but Hanna reading insertion "
                    "failed: %s", exc,
                )

        _LOGGER.info(
            "Calibration event recorded: serial=%s prev=%+.2f new=%+.2f "
            "delta=%+.2f source=%s hanna=%s",
            serial, entry["prev"], entry["new"], entry["delta"],
            entry["source"], entry["hanna_value"],
        )
        async_dispatcher_send(self.hass, SIGNAL_ADVISOR_UPDATED, "kh")
        self.hass.bus.async_fire(EVENT_CALIBRATION_RECORDED, entry)

    def is_in_calibration_settling_window(self, at_iso: str) -> bool:
        """Return True if `at_iso` falls within ±CALIBRATION_SETTLING_HOURS
        of any recorded calibration event. Used by the alk snapshotter
        to skip captures that would otherwise contaminate the empirical-
        potency slope derivation."""
        try:
            at = dt_util.parse_datetime(at_iso)
        except (TypeError, ValueError):
            return False
        if at is None:
            return False
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt_util.UTC)
        window = timedelta(hours=self.CALIBRATION_SETTLING_HOURS)
        blob = self._advisor_blob("kh")
        for entry in blob.get("calibration_events", []):
            ev_at = dt_util.parse_datetime(entry.get("at") or "")
            if ev_at is None:
                continue
            if ev_at.tzinfo is None:
                ev_at = ev_at.replace(tzinfo=dt_util.UTC)
            if abs((at - ev_at).total_seconds()) <= window.total_seconds():
                return True
        return False

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
        eff_per_mL_per_100L: float | None = None,
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
            # KH-specific potency (back-compat): the alk advisor reads
            # this directly via `eff_dkh_per_mL_per_100L`.
            "eff_dkh_per_mL_per_100L": (
                float(eff_dkh_per_mL_per_100L)
                if eff_dkh_per_mL_per_100L is not None else None
            ),
            # Generic per-element potency (added 0.5.1). Units depend
            # on `param_id`:
            # - calcium: ppm Ca per mL per 100L (Foundation A = 2.0)
            # - magnesium: ppm Mg per mL per 100L (Foundation C = 1.0)
            # - nitrate/phosphate (removers): negative values
            # Per-element advisors (param_advisor.compute_for_param)
            # read this value first, falling back to the KH field for
            # back-compat with profiles registered before 0.5.1.
            "eff_per_mL_per_100L": (
                float(eff_per_mL_per_100L)
                if eff_per_mL_per_100L is not None else None
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

    async def async_update_supplement_profile(
        self, profile_id: str, *,
        eff_dkh_per_mL_per_100L: float | None | _Unset = _UNSET,
        eff_per_mL_per_100L: float | None | _Unset = _UNSET,
        param_id: str | list[str] | None | _Unset = _UNSET,
        label_patterns: list[str] | None | _Unset = _UNSET,
        notes: str | None | _Unset = _UNSET,
    ) -> dict[str, Any]:
        """Update a supplement profile in place. Only the fields you
        pass are changed — sentinel `_UNSET` means "leave alone" (vs
        explicit None which means "clear this field").

        Use case: profiles registered in 0.4.4 era didn't have
        `eff_per_mL_per_100L` populated, so per-element advisors fall
        back to the param-built-in default. Calling this service with
        `eff_per_mL_per_100L` populates the field in place — no need
        to remove + re-add.

        Raises ValueError if the profile id doesn't exist.
        """
        existing = self.supplement_profiles
        for p in existing:
            if p["id"] == profile_id:
                if not isinstance(eff_dkh_per_mL_per_100L, _Unset):
                    p["eff_dkh_per_mL_per_100L"] = (
                        float(eff_dkh_per_mL_per_100L)
                        if eff_dkh_per_mL_per_100L is not None else None
                    )
                if not isinstance(eff_per_mL_per_100L, _Unset):
                    p["eff_per_mL_per_100L"] = (
                        float(eff_per_mL_per_100L)
                        if eff_per_mL_per_100L is not None else None
                    )
                if not isinstance(param_id, _Unset):
                    if param_id is None:
                        p["param_id"] = ["kh"]
                    elif isinstance(param_id, str):
                        p["param_id"] = [param_id]
                    else:
                        p["param_id"] = list(param_id)
                if not isinstance(label_patterns, _Unset):
                    p["label_patterns"] = [
                        s.lower() for s in (label_patterns or [])
                    ]
                if not isinstance(notes, _Unset):
                    p["notes"] = notes
                await self.async_save()
                # Re-fire dispatch on every target param so per-element
                # advisors recompute. After update, param_id may have
                # changed — fire on the union of old + new to be safe.
                # In practice the user almost always keeps the same
                # param_id; firing on the current list covers the
                # common case correctly.
                stored = p.get("param_id", "kh")
                pids = [stored] if isinstance(stored, str) else list(stored)
                for pid in pids:
                    async_dispatcher_send(
                        self.hass, SIGNAL_ADVISOR_UPDATED, pid,
                    )
                return p
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
