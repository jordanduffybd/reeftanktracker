"""Sensor entities for Reef Tank Tracker.

Per parameter we expose:
  sensor.reef_<id>_latest         — most recent recorded value
  sensor.reef_<id>_latest_method  — "Hanna ULR" / "Triton ICP" / etc.
  sensor.reef_<id>_latest_at      — sample timestamp (canonical)
  sensor.reef_<id>_days_since     — days since last MANUAL test
  sensor.reef_<id>_drift          — manual − auto, when both are fresh

Tank-level sensors:
  sensor.reef_alk_advisor_recommendation  — alk dosing advisor
  sensor.reef_active_dosing_plan          — last ICP test's dose plan
                                            (state = sample timestamp)

Habitat and problem are exposed via the `select` platform only —
they're tappable to change. The redundant read-only sensors were
removed in 0.4.0.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import alk_advisor
from .advisor import Recommendation
from .const import (
    DEVICE_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    SIGNAL_ADVISOR_UPDATED,
    SIGNAL_ICP_TEST_RECORDED,
    SIGNAL_READING_RECORDED,
)
from .coordinator import ReefDataCoordinator
from .parameters import ALL_PARAMETERS, ParameterDef


def _device_info() -> DeviceInfo:
    """Single shared device — all entities attach here so they're grouped
    in HA's UI under one row instead of 239 loose entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, DEVICE_ID)},
        name=DEVICE_NAME,
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
    )

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ReefDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for param in ALL_PARAMETERS:
        # ICP-only params get just the value + sample timestamp.
        # Drift / days-since-test / last-method are meaningless for
        # parameters that only ever have one source (ICP imports), so
        # they're not registered for those.
        entities.extend([
            ReefLatestSensor(coordinator, param),
            ReefLatestAtSensor(coordinator, param),
        ])
        if not param.get("icp_only"):
            entities.extend([
                ReefLatestMethodSensor(coordinator, param),
                ReefDaysSinceSensor(coordinator, param),
                ReefDriftSensor(coordinator, param),
            ])

    entities.extend([
        AlkAdvisorSensor(coordinator),
        DosingPlanSensor(coordinator),
        # Per-element advisors. One sensor per param_id in PARAM_DEFAULTS
        # — the sensor is registered always; availability mirrors the
        # per-param `enabled` toggle so it shows as unavailable until
        # the user opts in via Configure → "<Param> dosing advisor".
        # 0.5.0 ships Calcium; Mg / NO3 / PO4 follow.
    ])
    from . import param_advisor
    for param_id in param_advisor.PARAM_DEFAULTS:
        entities.append(ParameterAdvisorSensor(coordinator, param_id))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Base sensor: subscribes to reading-recorded dispatch
# ---------------------------------------------------------------------------
class _ReefSensorBase(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef,
                 suffix: str, name: str) -> None:
        self._coordinator = coordinator
        self._param = param
        self._attr_unique_id = f"reef_{param['id']}_{suffix}"
        # Entity name combines parameter + variant (e.g. "KH Latest").
        # ICP-only parameters get an "ICP" prefix in the name so they're
        # visually distinct from home-testable parameters in the entity
        # list and dashboards (e.g. "ICP Cadmium Latest" vs "Calcium
        # Latest"). The entity_id picks up the prefix too:
        # `sensor.reef_tank_icp_cadmium_latest`.
        prefix = "ICP " if param.get("icp_only") else ""
        self._attr_name = f"{prefix}{param['name']} {name}"
        self._attr_icon = param.get("icon", "mdi:water")

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_reading(parameter: str | None = None) -> None:
            # Re-render whether or not it's our parameter — cheap enough,
            # and tank-level changes don't pass a parameter at all.
            if parameter is None or parameter == self._param["id"]:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_READING_RECORDED, _on_reading
            )
        )


# ---------------------------------------------------------------------------
# Latest value
# ---------------------------------------------------------------------------
class ReefLatestSensor(_ReefSensorBase):
    """Most recent value for a parameter, by sample_taken_at.

    Uses Triton/manual readings authoritatively. If no manual or ICP value
    is present BUT an `auto_source` sensor exists, fall back to that
    sensor's current state.
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest", "Latest")
        self._attr_native_unit_of_measurement = param.get("unit")
        self._attr_state_class = SensorStateClass.MEASUREMENT
        if param.get("unit") == "°C":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def native_value(self) -> float | None:
        return self._resolve_latest()[0]

    def _resolve_latest(
        self,
    ) -> tuple[float | None, str | None]:
        """Return `(value, source_label)` for the freshest known reading.

        Compares the most-recent recorded reading (manual / ICP / auto)
        against the live auto-source state and returns whichever has
        the newer timestamp. Without this comparison, a stale recorded
        reading would dominate forever — e.g. a manual KH from 2 days
        ago would beat the KH Keeper sensor reading 7.84 right now,
        because the auto-source listener filters out unchanged values
        and so never records a fresh entry when the device keeps
        reporting the same number across measurements.

        `source_label` is one of: 'manual', 'icp', 'auto', 'auto-live'
        ('auto-live' = pulled directly from the auto-source's current
        state because it's newer than any recorded reading).
        """
        latest = self._coordinator.latest_reading(self._param["id"])
        auto_src = self._coordinator.get_auto_source(self._param["id"])

        auto_value: float | None = None
        auto_at: datetime | None = None
        if auto_src:
            state = self.hass.states.get(auto_src)
            if state and state.state not in (
                "unknown", "unavailable", "none", "", None,
            ):
                try:
                    auto_value = float(state.state)
                    # `last_changed` is when the state VALUE last
                    # changed (vs `last_updated` which fires on any
                    # state-object refresh). When the auto-source has
                    # a fresh non-stale value, last_changed reflects
                    # that. For KH Keeper the underlying bridge
                    # publishes only on test completion, so
                    # last_changed = test time.
                    auto_at = state.last_changed or state.last_updated
                except (ValueError, TypeError):
                    auto_value = None

        precision = self._param.get("precision")

        if latest is not None and auto_at is not None and auto_value is not None:
            try:
                latest_at = datetime.fromisoformat(latest["sample_taken_at"])
                # Both have timestamps — prefer the newer.
                if auto_at > latest_at:
                    return _round(auto_value, precision), "auto-live"
                return _round(latest["value"], precision), latest.get("source")
            except (ValueError, TypeError):
                pass

        if latest is not None:
            return _round(latest["value"], precision), latest.get("source")
        if auto_value is not None:
            return _round(auto_value, precision), "auto-live"
        return None, None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self._coordinator.latest_reading(self._param["id"])
        auto_src = self._coordinator.get_auto_source(self._param["id"])
        target_min, target_max = self._coordinator.get_target_range(
            self._param["id"]
        )
        attrs: dict[str, Any] = {
            "auto_source_entity": auto_src,
            "target_min": target_min,
            "target_max": target_max,
        }

        # Determine which value won — the live auto-source override
        # or a recorded reading — by re-running the resolver. Then
        # populate source-specific attributes. This keeps the
        # `source` / `sample_taken_at` attributes consistent with the
        # state value the user actually sees.
        value, source = self._resolve_latest()
        if source == "auto-live" and auto_src:
            state = self.hass.states.get(auto_src)
            sample_at = (
                (state.last_changed or state.last_updated).isoformat()
                if state else None
            )
            attrs.update({
                "source": "auto-live",
                "method": None,
                "sample_taken_at": sample_at,
                "recorded_at": sample_at,
                "test_id": None,
                "notes": (
                    f"Pulled live from {auto_src} — newer than any "
                    f"recorded reading."
                ),
            })
        elif latest is not None:
            attrs.update({
                "source": latest["source"],
                "method": latest.get("method"),
                "sample_taken_at": latest["sample_taken_at"],
                "recorded_at": latest["recorded_at"],
                "test_id": latest.get("test_id"),
                "notes": latest.get("notes"),
            })
        else:
            attrs["source"] = "auto" if auto_src else None

        if (
            value is not None
            and target_min is not None
            and target_max is not None
        ):
            attrs["in_target_band"] = bool(target_min <= value <= target_max)
        else:
            attrs["in_target_band"] = None
        return attrs


class ReefLatestMethodSensor(_ReefSensorBase):
    """The method used for the latest reading."""

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest_method", "Last Method")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        latest = self._coordinator.latest_reading(self._param["id"])
        if not latest:
            return None
        method = latest.get("method")
        if method:
            return method
        return latest.get("source", "").title() or None


class ReefLatestAtSensor(_ReefSensorBase):
    """When the latest reading was sampled."""

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "latest_at", "Last Sampled")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        latest = self._coordinator.latest_reading(self._param["id"])
        if not latest:
            return None
        try:
            return datetime.fromisoformat(latest["sample_taken_at"])
        except ValueError:
            return None


class ReefDaysSinceSensor(_ReefSensorBase):
    """Days since the last MANUAL test of this parameter.

    ICP imports don't reset this — the point is to track *when you last
    looked at it yourself*, since manual tests are the user's check-in.
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "days_since", "Days Since Test")
        self._attr_native_unit_of_measurement = "d"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        latest = self._coordinator.latest_manual(self._param["id"])
        if not latest:
            return None
        try:
            sampled = datetime.fromisoformat(latest["sample_taken_at"])
        except ValueError:
            return None
        delta = datetime.now(timezone.utc).astimezone() - sampled
        return max(0, delta.days)


class ReefDriftSensor(_ReefSensorBase):
    """Manual − auto, when both are fresh enough to compare.

    Returns None if there's no auto source for this parameter, or if
    either side is stale (>24 h).
    """

    def __init__(self, coordinator: ReefDataCoordinator, param: ParameterDef) -> None:
        super().__init__(coordinator, param, "drift", "Drift (manual − auto)")
        self._attr_native_unit_of_measurement = param.get("unit")
        self._attr_icon = "mdi:vector-difference"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        auto_src = self._coordinator.get_auto_source(self._param["id"])
        if not auto_src:
            return None
        manual = self._coordinator.latest_manual(self._param["id"])
        if not manual:
            return None
        try:
            sampled = datetime.fromisoformat(manual["sample_taken_at"])
        except ValueError:
            return None
        # Stale if older than a day
        age = datetime.now(timezone.utc).astimezone() - sampled
        if age.days > 1:
            return None
        state = self.hass.states.get(auto_src)
        if not state or state.state in ("unknown", "unavailable", None):
            return None
        try:
            auto_val = float(state.state)
        except (ValueError, TypeError):
            return None
        return _round(manual["value"] - auto_val, self._param.get("precision"))


def _round(value: float | None, precision: int | None) -> float | None:
    if value is None:
        return None
    if precision is None:
        return value
    return round(value, precision)


# ---------------------------------------------------------------------------
# Alk advisor sensor — wraps the algorithm in alk_advisor.compute_for_entity
# ---------------------------------------------------------------------------
class AlkAdvisorSensor(SensorEntity):
    """`sensor.reef_tank_alk_advisor_recommendation` — suggested alk daily
    dose in mL.

    State is `None` (unavailable) when the advisor is disabled or there
    isn't enough data; show-your-work data lives in attributes.
    Recomputes on advisor-update signals (snapshot recorded, ack,
    dismiss, demand-change) — not on every upstream state change.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()
    _attr_native_unit_of_measurement = "mL"
    _attr_icon = "mdi:test-tube"
    # NOT diagnostic — the suggested daily dose is the headline output
    # the user actually acts on. Marking it diagnostic hid it from
    # HA's Activity-panel entity filter and the device entity tree.
    _attr_unique_id = "reef_alk_advisor_recommendation"
    _attr_name = "Alk Advisor Recommendation"

    def __init__(self, coordinator: ReefDataCoordinator) -> None:
        self._coordinator = coordinator
        self._last: Recommendation | None = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ADVISOR_UPDATED, self._handle_update
            )
        )
        # Also subscribe to state changes on the configured upstream
        # sensors (alk heads + KH source + calibration warning). Without
        # this, a transient ReefBeat outage can leave the advisor stuck
        # on a stale `state=None` (because `_sum_dose_mL` returned None
        # during the outage and SIGNAL_ADVISOR_UPDATED doesn't refire
        # when the doser comes back). Tracking state changes lets the
        # sensor recompute the moment upstream data returns.
        from homeassistant.helpers.event import async_track_state_change_event
        cfg = self._coordinator.get_advisor_config()
        upstream: list[str] = []
        upstream.extend(cfg.get(alk_advisor.OPT_ALK_HEADS) or [])
        kh_src = cfg.get(alk_advisor.OPT_KH_SOURCE)
        if kh_src:
            upstream.append(kh_src)
        cal_warn = cfg.get(alk_advisor.OPT_CALIBRATION_WARNING_ENTITY)
        if cal_warn:
            upstream.append(cal_warn)
        if upstream:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, upstream, self._handle_upstream_change,
                )
            )
        self.async_write_ha_state()

    @callback
    def _handle_update(self, _param_id: str | None = None) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_upstream_change(self, _event) -> None:
        """An alk-head / KH source / calibration-warning sensor changed
        state — recompute. This is what auto-recovers the advisor after
        a ReefBeat outage: as soon as the doser sensor flips from
        unavailable back to a number, we re-render with current_dose
        populated and the user sees the "doser unreachable" reason
        replaced by a real recommendation (or learning-mode message)."""
        self.async_write_ha_state()

    def _compute(self) -> Recommendation | None:
        rec = alk_advisor.compute_for_entity(self.hass, self._coordinator)
        self._last = rec
        return rec

    @property
    def native_value(self) -> float | None:
        rec = self._compute()
        return rec.state if rec is not None else None

    @property
    def available(self) -> bool:
        # The sensor is registered always; availability mirrors the
        # advisor toggle so HA shows it as unavailable until enabled.
        cfg = self._coordinator.get_advisor_config()
        return bool(cfg.get(alk_advisor.OPT_ADVISOR_ENABLED))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self._last or self._compute()
        cfg = self._coordinator.get_advisor_config()
        if rec is None:
            return {
                "advisor_enabled": False,
                "reason": "Advisor disabled in Options.",
            }
        attrs = rec.as_attributes()
        attrs["advisor_enabled"] = True
        attrs["alk_head_entity_ids"] = list(
            cfg.get(alk_advisor.OPT_ALK_HEADS) or []
        )
        attrs["kh_source_entity"] = cfg.get(alk_advisor.OPT_KH_SOURCE) or ""
        return attrs


# ---------------------------------------------------------------------------
# Per-parameter advisor sensor (Calcium for 0.5.0; Mg / NO3 / PO4 follow)
# ---------------------------------------------------------------------------
class ParameterAdvisorSensor(SensorEntity):
    """Generic per-element advisor sensor.

    Same surface as `AlkAdvisorSensor` but parameter-aware. Reads from
    `param_advisor.compute_for_param(hass, coordinator, param_id)`. One
    instance per enabled parameter (one for calcium today; mg/no3/po4
    in subsequent releases).

    The alk advisor stays on its own dedicated class for now (stable in
    prod, no need to refactor). A future cleanup can collapse the
    duplication once the per-element work is settled.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()
    _attr_icon = "mdi:test-tube"

    def __init__(
        self, coordinator: ReefDataCoordinator, param_id: str,
    ) -> None:
        from . import param_advisor
        self._coordinator = coordinator
        self._param_id = param_id
        self._param_advisor = param_advisor
        self._last: Recommendation | None = None
        # One-shot guard: backfill fires once per HA session when
        # the advisor sees zero snapshots. Without this, every
        # render would queue another backfill (idempotent but wasteful).
        self._backfilled = False
        defaults = param_advisor.PARAM_DEFAULTS.get(param_id, {})
        # ppm for Ca/Mg/NO3/PO4 — comes from PARAM_DEFAULTS; the algorithm
        # itself is unit-agnostic.
        self._attr_native_unit_of_measurement = defaults.get(
            "value_unit", "mL",
        )
        # Use mL for the dose unit since the suggested daily dose IS in
        # mL. The unit_of_measurement above describes the parameter's
        # native unit; the sensor's native_value is the dose in mL.
        self._attr_native_unit_of_measurement = "mL"
        # Param-specific entity_id + friendly name. Calcium →
        # `sensor.reef_tank_calcium_advisor_recommendation`.
        title = param_id.capitalize()
        self._attr_unique_id = f"reef_{param_id}_advisor_recommendation"
        self._attr_name = f"{title} Advisor Recommendation"

    async def async_added_to_hass(self) -> None:
        # Recompute on advisor-update dispatcher (snapshot recorded,
        # ack, dismiss, demand-change, water-change, profile change for
        # this param).
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ADVISOR_UPDATED, self._handle_update
            )
        )
        # Also subscribe to state changes on the configured doser heads
        # so the advisor auto-recovers when ReefBeat returns from a
        # transient outage (see the same pattern in AlkAdvisorSensor —
        # this is the fix that landed in 0.4.3).
        from homeassistant.helpers.event import async_track_state_change_event
        from . import param_advisor
        cfg = self._coordinator.get_advisor_config()
        heads_key = param_advisor.opt_key(self._param_id, "heads")
        upstream: list[str] = list(cfg.get(heads_key) or [])
        if upstream:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, upstream, self._handle_upstream_change,
                )
            )
        self.async_write_ha_state()

    @callback
    def _handle_update(self, dispatched_param_id: str | None = None) -> None:
        # Skip recompute if the dispatcher fired for a different
        # parameter — keeps work down on multi-advisor installs.
        if dispatched_param_id is not None and dispatched_param_id != self._param_id:
            return
        self.async_write_ha_state()

    @callback
    def _handle_upstream_change(self, _event) -> None:
        self.async_write_ha_state()

    def _compute(self) -> Recommendation | None:
        rec = self._param_advisor.compute_for_param(
            self.hass, self._coordinator, self._param_id,
        )
        # Cold-start backfill: if the advisor is enabled but has no
        # snapshots yet (typical right after first install), fire a
        # one-shot backfill from the historical Reading records.
        # Re-compute after backfill so the user sees the actual state
        # rather than "Only 0 of 4 required Calcium snapshots..." until
        # they manually call a service.
        if (
            rec is not None
            and rec.confidence == "insufficient"
            and not self._backfilled
        ):
            self._backfilled = True
            self.hass.async_create_task(self._async_backfill_and_refresh())
        self._last = rec
        return rec

    async def _async_backfill_and_refresh(self) -> None:
        """Run a one-shot snapshot backfill from existing Reading
        records, then write the updated state. Logged at INFO so the
        user sees the cold-start path firing in the integration log."""
        try:
            added = await self._coordinator.async_backfill_advisor_snapshots(
                self._param_id,
            )
            if added:
                # Re-render with the new snapshots in storage
                self.async_write_ha_state()
        except Exception:  # noqa: BLE001
            # Best-effort — never let a backfill failure break the sensor
            pass

    @property
    def native_value(self) -> float | None:
        rec = self._compute()
        return rec.state if rec is not None else None

    @property
    def available(self) -> bool:
        cfg = self._coordinator.get_advisor_config()
        enabled_key = self._param_advisor.opt_key(self._param_id, "enabled")
        return bool(cfg.get(enabled_key))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rec = self._last or self._compute()
        cfg = self._coordinator.get_advisor_config()
        opt_key = self._param_advisor.opt_key
        if rec is None:
            return {
                "advisor_enabled": False,
                "param_id": self._param_id,
                "reason": (
                    f"{self._param_id.capitalize()} advisor disabled in "
                    "Options."
                ),
            }
        attrs = rec.as_attributes()
        attrs["advisor_enabled"] = True
        attrs["param_id"] = self._param_id
        attrs["dose_head_entity_ids"] = list(
            cfg.get(opt_key(self._param_id, "heads")) or []
        )
        return attrs


# ---------------------------------------------------------------------------
# Dosing-plan sensor — surfaces the most recent ICP test's habitat-aware
# dose recommendations as importance-sorted attributes for the dashboard.
# ---------------------------------------------------------------------------
class DosingPlanSensor(SensorEntity):
    """`sensor.reef_active_dosing_plan` — timestamp of the most recent ICP
    test's sample collection date.

    State is the sample-date timestamp (TIMESTAMP device class) so the HA
    UI shows "Last test: X days ago" rather than "updated 5 minutes ago"
    (which would reflect import time, not when the sample was actually
    taken). The full importance-sorted dose plan + active habitat/problem
    + test metadata live in attributes for dashboard cards to render.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_info = _device_info()
    _attr_icon = "mdi:beaker-plus-outline"
    _attr_unique_id = "reef_active_dosing_plan"
    _attr_name = "Active Dosing Plan"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ReefDataCoordinator) -> None:
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ICP_TEST_RECORDED, self.async_write_ha_state
            )
        )

    @property
    def native_value(self) -> datetime | None:
        test = self._coordinator.latest_icp_test
        if test is None:
            return None
        date_str = test.get("sample_date")
        if not date_str:
            return None
        try:
            # Sample is dated at midnight UTC of the collection day —
            # we don't know the actual sampling time of day, only the
            # date shown on the report.
            return datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        test = self._coordinator.latest_icp_test
        if test is None:
            return {"status": "No ICP test imported yet."}
        recs = sorted(
            test.get("recommendations") or [],
            key=lambda r: -(r.get("importance_stars") or 0),
        )
        return {
            "test_id": test.get("test_id"),
            "sample_date": test.get("sample_date"),
            "imported_at": test.get("imported_at"),
            "active_habitat": test.get("active_habitat"),
            "active_problem": test.get("active_problem"),
            "rendered_for_habitat": test.get("selected_habitat"),
            "rendered_for_problem": test.get("selected_problem"),
            "url": test.get("url"),
            "recommendations_count": len(recs),
            "recommendations": recs,
        }
