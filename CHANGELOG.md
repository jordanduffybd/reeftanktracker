# Changelog

## 0.1.8

- **Fix: historical data now appears on history graphs at the correct time.** Previously every recorded reading became a state change at "now", with the actual sample date only stored as an attribute. Imported 2023 data piled up at "today" and graphs showed nothing meaningful. Now `record_reading` *also* writes to HA's long-term statistics table (via `recorder.async_import_statistics`) using the real `sample_taken_at`, so the history graph card shows the proper timeline.
- **New `reeftanktracker.backfill_statistics` service.** One-shot tool to re-import every stored reading into the statistics table — use after upgrading to 0.1.8 if you previously imported data on an older version. Optionally limit to a single parameter via the `parameter` field.
- Statistics buckets are hourly; multiple readings in the same hour for the same parameter collapse to one bucket (last write wins). Fine for normal reefkeeping cadences.

## 0.1.7

- **Active Test Method select.** New `select.reef_tank_active_test_method` dropdown (Hanna ULR / Salifert / Red Sea Pro / API / Tropic Marin / Triton ICP / Refractometer / Probe / Other / Unspecified). When you enter a value via a parameter's number entity, the method label comes from this select — set once at the start of a test session and every entry that follows is correctly tagged.
- **Fix:** number entries no longer auto-stamp a method based on the parameter's `common_methods` list. Previously typing an Ammonia value silently labelled it "Salifert" because that was first in the list. Now the only source of method is the session select. "Unspecified" → method=None.
- `set_habitat` service now accepts `method` (in addition to `habitat` and `problem`).

## 0.1.6

- **Fix**: number entry entities no longer report unavailable when no reading exists yet, so you can set the very first value from the entity's detail modal (previously you had to enter it from the main screen first to "wake it up").
- **New tool: `scripts/import_history.py`** — bulk-import legacy data via HA's REST API. Supports the Aquarium Log app's CSV export (records as manual Hanna readings) and Triton ICP-OES CSV exports (records as ICP readings + stashes the full test record). Timestamps preserve sample-date so historical data lands at the correct points on the timeline.

## 0.1.5

- **Dashboard installer now tolerates HA Lovelace API changes.** Tries three strategies in order: (1) the in-memory dashboards collection on `hass.data["lovelace"]`, (2) bootstrapping a fresh `DashboardsCollection` instance from disk, (3) direct write to `.storage/lovelace_dashboards`. Strategy 2 covers HA 2024+ where the collection isn't exposed in hass.data.
- If the installer falls back to strategy 2 or 3, you'll see a `WARNING` in the log telling you to restart HA — the files are written, but HA only loads the new dashboard registry on startup.
- After restart the dashboard appears in the sidebar normally; subsequent updates rewrite the view config in place without restart.

## 0.1.4

- **Configurable auto-source sensors per parameter.** Settings → Devices & Services → Reef Tank Tracker → Configure now opens a per-parameter form letting you pick which `sensor.*` (or `input_number.*`) feeds each parameter's "auto" fallback. Hardcoded defaults (KH Keeper for KH/pH, ATO probe for Temperature) are pre-filled — confirm or override. Empty = manual/ICP-only.
- Latest sensors expose `auto_source_entity` as an attribute so you can see which entity is currently feeding them.
- Coordinator now owns the auto-source map; reload is automatic when options change.

## 0.1.3

- **New `select` entities** for habitat and problem context. Settings → Devices → Reef Tank now has `select.reef_tank_habitat` and `select.reef_tank_problem` dropdowns — change context from any dashboard card without writing automations.
- **Dashboard auto-install fix:** previously the install ran during `async_setup_entry` and could fire before HA's lovelace integration was fully ready. Now deferred to the `homeassistant_started` event, and uses HA's `dashboards_collection` API directly so the dashboard appears in the sidebar without an HA restart.
- Added `docs/RELEASING.md` (HACS picks up GitHub Releases, not bare tags), `docs/DASHBOARD.md`, and `docs/SERVICES.md`.

## 0.1.2

- **Single virtual device** — every entity is now grouped under a "Reef Tank" device, so they appear together in Settings → Devices instead of as 239 loose rows.
- **Predictable entity IDs** — `sensor.reef_tank_kh_latest`, `number.reef_tank_kh_entry`, etc. (Previously inconsistent because of `has_entity_name` without a device.)
- **Auto-install Lovelace dashboard** at `/reef-tank-tracker` with three views (Test Session / Overview / Diagnostics).
- **`regenerate_dashboard` service** — clears the "user-removed" flag and rebuilds.

## 0.1.1

- Pre-release bookkeeping.

## 0.1.0

- Initial release. 13 input parameters + 33 ICP-only parameters. Per-parameter sensors (latest, latest_method, latest_at, days_since, drift). Auto-saving number entry. Storage-backed coordinator. Services: `record_reading`, `add_inventory`, `remove_inventory`, `set_habitat`, `import_icp`. 18 unit tests passing on Python 3.11/3.12.
