"""Pytest config + lightweight HA stubs.

We don't depend on the full Home Assistant test harness — we only need
enough surface area to exercise the coordinator logic. Stubs live here.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# Make the integration importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components"))


# ---------------------------------------------------------------------------
# Minimal homeassistant stubs — enough for `coordinator.py` to import
# ---------------------------------------------------------------------------
def _install_ha_stubs() -> None:
    """Inject a barely-functional `homeassistant.*` package tree + voluptuous.

    The custom_components package's __init__ imports voluptuous and a few HA
    helpers. We stub all of them so tests can run without HA installed.
    """
    if "homeassistant" in sys.modules:
        return

    # voluptuous — only need the call-shape, not validation behaviour
    if "voluptuous" not in sys.modules:
        vol = types.ModuleType("voluptuous")
        class _Marker:
            def __init__(self, key, default=None): self.key = key; self.default = default
            def __repr__(self): return f"_Marker({self.key!r})"
        def _passthrough(x): return x
        vol.Schema = lambda *a, **kw: MagicMock(return_value={})
        vol.Required = vol.Optional = lambda key, default=None: _Marker(key, default)
        vol.In = lambda options: _passthrough
        vol.Coerce = lambda t: _passthrough
        vol.All = lambda *a, **kw: _passthrough
        vol.Any = lambda *a, **kw: _passthrough
        vol.Range = lambda **kw: _passthrough
        class _Invalid(Exception): pass
        vol.Invalid = _Invalid
        sys.modules["voluptuous"] = vol

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []  # marks it as a package so submodules can be imported
    sys.modules["homeassistant"] = ha

    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    sys.modules["homeassistant.const"] = const

    core = types.ModuleType("homeassistant.core")
    class HomeAssistant:  # noqa: D401
        """Stub HA instance."""
    class ServiceCall:
        def __init__(self, data): self.data = data
    class State:
        def __init__(self, entity_id, state, last_updated=None, attributes=None):
            self.entity_id = entity_id
            self.state = state
            self.last_updated = last_updated
            self.attributes = attributes or {}
    class Event:
        def __init__(self, data):
            self.data = data
    def callback(fn): return fn  # decorator no-op
    core.HomeAssistant = HomeAssistant
    core.ServiceCall = ServiceCall
    core.State = State
    core.Event = Event
    core.callback = callback
    sys.modules["homeassistant.core"] = core

    config_entries = types.ModuleType("homeassistant.config_entries")
    class ConfigEntry: pass
    class ConfigFlow:
        def __init_subclass__(cls, **kw): pass
        async def async_set_unique_id(self, *a, **kw): pass
        def _abort_if_unique_id_configured(self): pass
        def async_create_entry(self, **kw): return kw
        def async_show_form(self, **kw): return kw
    class OptionsFlow:
        def __init_subclass__(cls, **kw): pass
        def async_create_entry(self, **kw): return kw
        def async_show_form(self, **kw): return kw
        def add_suggested_values_to_schema(self, schema, suggested):
            return schema
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    sys.modules["homeassistant.config_entries"] = config_entries

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    sys.modules["homeassistant.helpers"] = helpers

    selector = types.ModuleType("homeassistant.helpers.selector")
    selector.EntitySelector = lambda *a, **kw: lambda v: v
    selector.EntitySelectorConfig = lambda **kw: kw
    selector.BooleanSelector = lambda *a, **kw: lambda v: v
    selector.NumberSelector = lambda *a, **kw: lambda v: v
    selector.NumberSelectorConfig = lambda **kw: kw
    selector.SelectSelector = lambda *a, **kw: lambda v: v
    selector.SelectSelectorConfig = lambda **kw: kw
    sys.modules["homeassistant.helpers.selector"] = selector

    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    class _Registry:
        entities: dict = {}
    def _async_get(_hass): return _Registry()
    entity_registry.async_get = _async_get
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry

    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = MagicMock()
    sys.modules["homeassistant.helpers.dispatcher"] = dispatcher

    event = types.ModuleType("homeassistant.helpers.event")
    # Default no-op subscriber; tests can monkeypatch this to capture
    # the registered handler for synthetic state-change events.
    event.async_track_state_change_event = lambda hass, ids, cb: (lambda: None)
    event.async_track_time_change = lambda hass, cb, *, hour=0, minute=0, second=0: (lambda: None)
    # async_call_later is used by the toast helper in __init__.py to
    # schedule a delayed dismiss. The stub is a no-op — tests that
    # exercise toast behaviour can monkeypatch this if needed.
    event.async_call_later = lambda hass, delay, action: (lambda: None)
    sys.modules["homeassistant.helpers.event"] = event

    # `homeassistant.util.dt` is used for ISO timestamp generation in
    # the toast helper's notification_id. Stub utcnow() to return a
    # fixed instant — tests don't need accurate time here.
    util_pkg = types.ModuleType("homeassistant.util")
    util_dt = types.ModuleType("homeassistant.util.dt")
    from datetime import datetime, timezone as _tz
    util_dt.utcnow = lambda: datetime.now(_tz.utc)
    util_dt.UTC = _tz.utc
    def _parse_datetime(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None
    util_dt.parse_datetime = _parse_datetime
    util_pkg.dt = util_dt
    sys.modules["homeassistant.util"] = util_pkg
    sys.modules["homeassistant.util.dt"] = util_dt

    # `homeassistant.components.sensor` — required for sensor.py to
    # import. Stubs the entity base + device-class enums.
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    sys.modules["homeassistant.components"] = components

    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    class _SensorEntity:
        _attr_native_unit_of_measurement = None
        _attr_state_class = None
        _attr_device_class = None
        _attr_has_entity_name = True
        _attr_unique_id = None
        _attr_name = None
        _attr_translation_key = None
        _attr_extra_state_attributes: dict = {}
        _attr_icon = None
        _attr_entity_category = None
        _attr_should_poll = False
        async def async_added_to_hass(self): pass
        def async_write_ha_state(self): pass
    class _Enum:
        def __init__(self, name): self.name = name
        def __getattr__(self, k): return _Enum(k)
    sensor_mod.SensorEntity = _SensorEntity
    sensor_mod.SensorDeviceClass = _Enum("SensorDeviceClass")
    sensor_mod.SensorStateClass = _Enum("SensorStateClass")
    sys.modules["homeassistant.components.sensor"] = sensor_mod

    device_registry = types.ModuleType(
        "homeassistant.helpers.device_registry",
    )
    device_registry.DeviceInfo = lambda **kw: kw
    sys.modules["homeassistant.helpers.device_registry"] = device_registry

    entity_platform = types.ModuleType(
        "homeassistant.helpers.entity_platform",
    )
    entity_platform.AddEntitiesCallback = type(None)
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform

    dispatcher2 = sys.modules["homeassistant.helpers.dispatcher"]
    if not hasattr(dispatcher2, "async_dispatcher_connect"):
        dispatcher2.async_dispatcher_connect = (
            lambda *a, **kw: (lambda: None)
        )

    const_mod = sys.modules.get("homeassistant.const")
    if const_mod is not None and not hasattr(const_mod, "EntityCategory"):
        const_mod.EntityCategory = _Enum("EntityCategory")

    cv = types.ModuleType("homeassistant.helpers.config_validation")
    cv.string = str
    cv.config_entry_only_config_schema = lambda _domain: lambda config: config
    cv.ensure_list = lambda v: v if isinstance(v, list) else [v] if v else []
    sys.modules["homeassistant.helpers.config_validation"] = cv

    storage = types.ModuleType("homeassistant.helpers.storage")
    class Store:
        """Stub Store that round-trips through an in-memory dict."""
        def __init__(self, hass, version, key):
            self._key = key
            self._payload = None

        async def async_load(self):
            return self._payload

        async def async_save(self, data):
            self._payload = data
    storage.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage


_install_ha_stubs()


@pytest.fixture
def hass() -> MagicMock:
    """A bare minimum HA stub passed to the coordinator."""
    return MagicMock()


@pytest.fixture
def event_loop():
    """Create an event loop for async tests (matches asyncio_mode=auto)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
