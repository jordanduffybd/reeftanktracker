"""Watch for user-initiated dose changes on the doser's daily_dose entities.

When the user bumps a dose in the ReefBeat mobile app (or any other
out-of-band path), the daily-dose sensor state changes. Before this
listener, the advisor stayed blind to that change until the user also
clicked the dashboard's Acknowledge button — which they often forget
to do, leaving the advisor stuck in pre-change cooldown logic.

This listener treats a dose change as an implicit acknowledgement when
the new value sits within tolerance of the current suggestion. The
direction-aware logic:

  * NEW ≈ SUGGESTED (within tolerance)  → implicit Ack
  * NEW moved toward SUGGESTED         → implicit Ack (partial),
                                          flagged with the gap
  * NEW moved AWAY from SUGGESTED       → implicit DemandChange
                                          (user is overriding advice)

Tolerance defaults: 10% relative OR 0.1 mL absolute, whichever is
larger. That handles both small doses (e.g. 0.5 mL Ca where 10% is
0.05 mL — too tight) and large ones (e.g. 11 mL alk where 10% is 1.1 mL).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from .coordinator import ReefDataCoordinator

_LOGGER = logging.getLogger(__name__)

# Debounce: ignore a second dose change within this window. ReefBeat
# sometimes settles with a few intermediate values when the user types
# a new daily dose. Without the debounce we'd record an ack for every
# transient value.
DEBOUNCE = timedelta(seconds=60)

# Minimum dose change that counts as a deliberate action — anything
# smaller is treated as noise (rounding, sensor jitter).
MIN_CHANGE_ML = 0.05

# Tolerance for "near the suggestion".
TOLERANCE_PCT = 10.0   # relative, %
TOLERANCE_MIN_ML = 0.1  # absolute floor


def _within_tolerance(actual: float, target: float) -> tuple[bool, float]:
    """Return (within, abs_gap_pct). abs_gap_pct uses target as base,
    or 1 mL floor if target is 0 to avoid div-by-zero."""
    if target == 0:
        gap = abs(actual)
    else:
        gap = abs(actual - target)
    base = max(abs(target), 1.0)
    gap_pct = (gap / base) * 100.0
    tol_abs = max(abs(target) * TOLERANCE_PCT / 100.0, TOLERANCE_MIN_ML)
    return abs(actual - target) <= tol_abs, gap_pct


class DoseChangeListener:
    """One instance per ConfigEntry. The integration reloads on options
    change, so this object is recreated cleanly when the head map
    changes."""

    def __init__(self, hass: HomeAssistant, coordinator: ReefDataCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        # entity_id → param_id (which parameter's advisor owns it)
        self._entity_to_param: dict[str, str] = {}
        # entity_id → (last_value, last_at) for debounce
        self._last_seen: dict[str, tuple[float, datetime]] = {}
        # entity_id → last recorded value at integration startup, so the
        # very first state-change after boot is comparable.
        self._initial_value: dict[str, float] = {}
        self._unsub: Any = None

    async def async_start(self) -> None:
        self._build_entity_map()
        if not self._entity_to_param:
            _LOGGER.debug("No dose-change tracking targets configured")
            return
        entity_ids = list(self._entity_to_param.keys())
        _LOGGER.info(
            "Dose-change listener tracking %d head entities: %s",
            len(entity_ids), ", ".join(entity_ids),
        )
        # Seed initial values so the first detected change has a
        # comparison point. Live HA states only.
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            try:
                self._initial_value[entity_id] = float(state.state)
            except (TypeError, ValueError):
                continue
        self._unsub = async_track_state_change_event(
            self._hass, entity_ids, self._handle_state_change,
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _build_entity_map(self) -> None:
        """Map every configured head entity → its owning parameter."""
        # Lazy imports break the circular dep with the advisor modules.
        from .alk_advisor import OPT_ALK_HEADS, _opt as alk_opt
        from . import param_advisor as pa

        for eid in alk_opt(self._coordinator, OPT_ALK_HEADS) or []:
            if isinstance(eid, str) and eid:
                self._entity_to_param[eid] = "kh"
        for param_id in pa.PARAM_DEFAULTS:
            heads = pa._opt(
                self._coordinator, pa.opt_key(param_id, "heads"), [],
            ) or []
            for eid in heads:
                if isinstance(eid, str) and eid:
                    # If the same head is shared across parameters
                    # (unusual but possible), the first param wins.
                    self._entity_to_param.setdefault(eid, param_id)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")
        if new_state is None:
            return
        self._hass.async_create_task(
            self._process(new_state, old_state)
        )

    async def _process(self, new_state: State, old_state: State | None) -> None:
        eid = new_state.entity_id
        param_id = self._entity_to_param.get(eid)
        if param_id is None:
            return
        if new_state.state in {"unknown", "unavailable", "none", ""}:
            return
        try:
            new_value = float(new_state.state)
        except (TypeError, ValueError):
            return
        # Prefer the old state's value; fall back to the boot-seeded
        # initial value so the first change after startup is meaningful.
        prev_value: float | None = None
        if old_state is not None and old_state.state not in {"unknown", "unavailable", "none", ""}:
            try:
                prev_value = float(old_state.state)
            except (TypeError, ValueError):
                prev_value = None
        if prev_value is None:
            prev_value = self._initial_value.get(eid)

        if prev_value is None:
            # First-ever observation — seed and bail.
            self._initial_value[eid] = new_value
            return

        change = new_value - prev_value
        if abs(change) < MIN_CHANGE_ML:
            return

        now = datetime.now(timezone.utc)
        last = self._last_seen.get(eid)
        if last is not None and (now - last[1]) < DEBOUNCE:
            _LOGGER.debug(
                "Dose change on %s debounced (Δt %.1fs)",
                eid, (now - last[1]).total_seconds(),
            )
            return
        self._last_seen[eid] = (new_value, now)

        await self._classify_change(param_id, eid, prev_value, new_value)

    async def _classify_change(
        self,
        param_id: str,
        head_entity_id: str,
        prev: float,
        new: float,
    ) -> None:
        """Compare the dose change against the active recommendation
        and record an implicit ack or demand-change accordingly."""
        suggested = self._current_suggestion(param_id)
        if suggested is None:
            _LOGGER.debug(
                "Dose change on %s recorded but no active recommendation "
                "for %s — skipping implicit ack",
                head_entity_id, param_id,
            )
            return

        # For multi-head parameters (alk has multiple Foundation B heads
        # in some setups, or per-element with multiple dosers), the
        # suggestion is for the SUM of head doses, not any one head.
        # Compare the change in TOTAL dose, not single-head value.
        live_total = self._live_total_dose(param_id)
        if live_total is None:
            return
        # Reconstruct: prev_total = live_total - (new - prev). The current
        # event's head moved by `new - prev`, so prev_total is the total
        # before that move.
        prev_total = live_total - (new - prev)
        new_total = live_total

        within, gap_pct = _within_tolerance(new_total, suggested)
        direction = (
            "toward"
            if (new_total - prev_total) * (suggested - prev_total) > 0
            else "away"
        )

        if within:
            await self._coordinator.async_record_advisor_acknowledgment(
                param_id,
                applied_value_mL=new_total,
                prev_value_mL=prev_total,
                implicit=True,
                suggested_value_mL=suggested,
                tolerance_pct=gap_pct,
            )
            _LOGGER.info(
                "Implicit ack for %s: prev_total=%.2f new_total=%.2f "
                "(suggested=%.2f, gap=%.1f%%) via %s",
                param_id, prev_total, new_total, suggested, gap_pct,
                head_entity_id,
            )
            return

        if direction == "toward":
            # Partial: user moved the right way but didn't apply the
            # full suggestion. Still cooldown the advisor — they made
            # a deliberate change.
            await self._coordinator.async_record_advisor_acknowledgment(
                param_id,
                applied_value_mL=new_total,
                prev_value_mL=prev_total,
                implicit=True,
                suggested_value_mL=suggested,
                tolerance_pct=gap_pct,
            )
            _LOGGER.info(
                "Partial implicit ack for %s: prev_total=%.2f new_total=%.2f "
                "(suggested=%.2f, %.1f%% short) via %s",
                param_id, prev_total, new_total, suggested, gap_pct,
                head_entity_id,
            )
            return

        # Moved away from suggestion — user is overriding the advice.
        # Log as a demand change so the advisor knows tank dynamics
        # may have shifted.
        magnitude_pct = abs(new_total - prev_total) / max(prev_total, 1.0) * 100.0
        expected_direction = (
            "rising" if (new_total > prev_total) else "falling"
        )
        await self._coordinator.async_record_advisor_demand_change(
            param_id,
            reason=(
                f"Auto-detected: user changed dose {prev_total:.2f} → "
                f"{new_total:.2f} mL/day (advisor had suggested "
                f"{suggested:.2f}); treating as demand change."
            ),
            expected_direction=expected_direction,
            magnitude_hint_pct=magnitude_pct,
        )
        _LOGGER.info(
            "Detected dose override for %s: prev_total=%.2f new_total=%.2f "
            "(suggested=%.2f) — recorded as demand change",
            param_id, prev_total, new_total, suggested,
        )

    def _current_suggestion(self, param_id: str) -> float | None:
        """Read the advisor's current suggested_dose_mL for a parameter.

        Uses the same compute paths as the existing acknowledge_advisor
        service so we never disagree with the dashboard's shown value.
        """
        try:
            from .alk_advisor import compute_for_entity as alk_compute
            from . import param_advisor as pa
            if param_id == "kh":
                rec = alk_compute(self._hass, self._coordinator)
            else:
                rec = pa.compute_for_param(self._hass, self._coordinator, param_id)
            if rec is None:
                return None
            return (
                float(rec.suggested_dose_mL)
                if rec.suggested_dose_mL is not None else None
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Could not compute suggestion for %s: %s",
                param_id, exc,
            )
            return None

    def _live_total_dose(self, param_id: str) -> float | None:
        """Sum the live `_daily_dose` across all configured heads for a
        parameter — same path the explicit Ack handler uses."""
        try:
            from .alk_advisor import (
                OPT_ALK_HEADS, _opt as alk_opt, _sum_dose_mL as alk_sum,
            )
            from . import param_advisor as pa
            if param_id == "kh":
                heads = list(alk_opt(self._coordinator, OPT_ALK_HEADS) or [])
                return alk_sum(self._hass, heads)
            heads = list(
                pa._opt(self._coordinator, pa.opt_key(param_id, "heads"), [])
                or []
            )
            return pa._sum_dose_mL(self._hass, heads)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Could not read live dose for %s: %s", param_id, exc,
            )
            return None
