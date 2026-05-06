# Tests

Pytest suite for the coordinator and parameter declarations. Runs without a
real Home Assistant install — `conftest.py` injects lightweight stubs for
the `homeassistant.*` modules and `voluptuous` so the integration code can
be imported in isolation.

## Run

```bash
pip install pytest pytest-asyncio
pytest tests/
```

## What's covered

- **`test_parameters.py`** — internal consistency of the parameter list
  (unique ids, sane ranges, ICP symbol map round-trips, etc.)
- **`test_coordinator.py`** — the actual storage logic:
  - default state
  - record_reading basics + invalid source rejection
  - `latest_reading()` picks by `sample_taken_at` (the date-correct rule
    that lets a recent Hanna reading beat an older ICP that just landed)
  - `latest_manual()` ignores ICP entries
  - habitat / problem validation
  - inventory add + remove + missing-id error
  - ICP test dedupe by `test_id` (importing the same lab report twice
    overwrites instead of duplicating)
  - readings round-trip through `Store.async_save` / `async_load`

## What's NOT covered

- HA framework wiring (sensor/number platform setup, dispatch signals,
  config flow). Those tests would need
  [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
  which pulls in a real HA install. Worth adding later if we get bugs at
  the integration boundary.
- Lovelace YAML — validated only via `yaml.safe_load`.

## CI

`.github/workflows/tests.yml` runs the suite on Python 3.11 and 3.12 on
every push to main and on PRs.
