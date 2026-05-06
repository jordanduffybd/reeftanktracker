# Changelog

## 0.3.0

Headline: an **alkalinity dosing advisor** that watches your KH against a target band, your alk doser's programmed daily dose, and a rolling 7-day window of snapshots — and suggests a small daily-dose change when it sees a persistent drift. Recommendation-only in v1 (never writes to your doser). Built around five user-stated stability rules: median over window (no reaction to single readings), trends only (must be persistently outside the band), cooldown after each acknowledged change, ±10 % step cap, and a confidence gate that downgrades to "low" when the KH source's calibration is overdue.

- **Sensor:** `sensor.reef_tank_alk_advisor_recommendation` exposes the suggested daily dose (or `unavailable` when disabled / insufficient data) plus ~30 show-your-work attributes — KH median, target band, current/suggested dose, change in mL and %, confidence, full reason text, observed slope, spec efficiency, samples used, cooldown timestamp, calibration warning, last/days-since demand change, last/days-since water change, samples excluded for water-change settling, and observed-vs-spec empirical potency.
- **Math:** uses manufacturer spec potency (Foundation B = 0.1 dKH/mL/100L by default) rescaled to tank volume, spreads the deficit over `correction_period_days` (default 7), and caps the per-cycle change at `step_cap_pct` (default 10 %). The math is intentionally not Bayesian — values transparency over peak accuracy.
- **Auto-detect supplement profile:** reads `sensor.<doser>_head_<N>_supplement` from your alk head and label-matches against builtin profiles (Red Sea Foundation B, Tropic Marin Alkalinity Plus). User-managed custom profiles register via `reeftanktracker.add_supplement_profile` and merge with builtins at every read site (Options dropdown, auto-detect patterns, efficiency resolution).
- **Observed-vs-spec diagnostic:** when you acknowledge a dose change, the advisor estimates the actual potency from before/after slope: `(slope_after − slope_before) / dose_change`. Surfaces `empirical_potency_dkh_per_mL`, `empirical_to_spec_ratio`, and a `spec_drift_warning` flag when the ratio drifts more than the configured threshold (default ±50 %). Diagnostic only — math still uses spec.
- **Demand-change events:** new `reeftanktracker.log_demand_change` service. When you add or remove livestock, log it and the advisor truncates the rolling window from that point and enters "learning mode" for `min_samples_after_event` days before it'll suggest again. Pre-event efficiency is no longer extrapolated.
- **Water-change events:** new `reeftanktracker.log_water_change` service. Snapshots within `wc_settling_hours` (default 24) of a logged water change are excluded from the slope/median calculation, but the rolling window itself is NOT truncated (a water change is a dilution event, not a persistent rate shift, in contrast to demand changes).
- **Inline forms on the dashboard:** the new "Alk Advisor" Lovelace view has two form sections (Water change, Demand change) with editable fields and Submit buttons. Plus Acknowledge / Dismiss buttons that auto-fill from the current recommendation and live alk-head dose. Heavier admin operations (custom supplement CRUD, manual snapshots) live as services in Developer Tools.
- **Manual snapshot service:** `reeftanktracker.capture_snapshot_now` lets you stamp a baseline (e.g. right after calibrating the KH Keeper) or seed historical snapshots for testing.
- **Logbook + bus events:** every user-initiated advisor action (water change, demand change, ack, dismiss, snapshot) fires both a Logbook entry and a `reeftanktracker_<action>_logged` bus event, so the activity is browsable at `/logbook` and automations can hook in.
- **Options-flow gets a menu:** Settings → Devices & Services → Configure now opens a two-page menu — "Auto-source sensors" (existing) and "Alk dosing advisor" (new). The advisor page surfaces every tunable: enabled toggle, alk head multi-select, KH source, calibration warning entity, target band, supplement profile, tank volume, window/min-samples/cooldown/step-cap/hysteresis/empirical-drift/wc-settling-hours and snapshot timing.
- **Supplement default corrected:** previous `0.36 dKH/mL/100L` default is wrong for Foundation B. Vendor-published value is `0.1`. Correction is masked by the step cap on small changes but matters for diagnostics.
- **KH default target band:** updated `7.5 / 9.0` → `8.5 / 8.9` (per Jordan's confirmed reef target).
- **Entity-selector filtering** on the Options page: alk-head picker filters to `device_class=volume` sensors (the daily_dose entities), calibration warning to `device_class=problem` binary_sensors. Less guessing, faster setup.
- **75 unit tests** cover the algorithm (stability rules, demand event window truncation, calibration warning, hysteresis, cooldown, observed-vs-spec match/divergence/skip), coordinator round-trip + slug-collision tests for supplement profiles, water-change settling/no-truncate logic.
- **Local dev environment** under `dev/`: docker-compose stack with HA Container + Mosquitto + a prod-mirror Python service that streams the alk-advisor entity surface via REST. Bind-mounts the integration source for live edits, named volume for HA state. A `dev/tools/seed_from_prod.py` script pulls 7 days of recorder history from prod and seeds dev's snapshot storage. See `dev/README.md`.

## 0.1.14

- **Dashboard live-update no longer requires HA restart.** `regenerate_dashboard` (and the auto-install refresh path) used to call `Store.async_save` directly on `.storage/lovelace.reef_tank_tracker`, which updated disk but bypassed HA's in-memory `LovelaceStorage` cache and never fired `EVENT_LOVELACE_UPDATED`. Result: the new layout was on disk but invisible until HA was restarted. Now we look up the `LovelaceStorage` instance from `hass.data['lovelace'].dashboards[<url_path>]` and call `async_save(config)` on it — the cache updates and connected clients reload automatically. We retain the direct-Store fallback for install Strategies 2/3 (where the dashboard was registered without being wired into the live `dashboards` dict), and that fallback now logs loudly that a restart is needed.
- **Auto-source readings now flow into the parameter log.** Previously the per-parameter `auto_source` was a passive display fallback only — `sensor.kh_keeper_kh` could be updating every measurement cycle and `reeftanktracker` would never notice. The integration now subscribes to state changes on each configured auto-source entity and records a `source="auto"` reading via the coordinator on every meaningful change. Readings flow into HA's long-term statistics at the upstream's `last_updated` timestamp, so KH Keeper / ReefBeat / probe history accumulates on the same graphs as Hanna runs. Manual readings still win in the Latest sensor when their `sample_taken_at` is more recent. The `Drift (manual − auto)` sensor now also re-renders on every upstream tick because it subscribes to `SIGNAL_READING_RECORDED`.
- Throttling is conservative: per parameter we skip a state change if (a) the value delta is below the parameter's `step` (e.g. 0.05 dKH for KH, 0.1 °C for temperature) AND (b) the previous auto reading was less than 5 minutes ago. The 5-minute throttle is bypassed if no auto reading has been recorded in the last 24 hours, so we always keep at least one reading per day per configured parameter.
- On integration setup we capture the current state once so a fresh install doesn't have to wait for the next upstream tick before any auto reading appears.

## 0.1.13

- **Declare `recorder` as `after_dependencies`.** The coordinator imports `recorder.statistics` lazily (try/except) and falls back gracefully when it's not loaded, but declaring the dependency means HA always loads recorder *before* this integration. Removes a startup race where the first reading after HA boot could miss its statistics write if recorder wasn't ready yet.
- **CI** — pytest (Python 3.11 + 3.12), ruff, hassfest, and HACS validation now run on every push and PR. Future regressions in manifest, schema, or config-flow surface in CI before release. See `.github/workflows/`.
- **Repo hygiene.** Added `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)` per HA convention (silences hassfest warning, no user-visible effect). Removed dead imports flagged by ruff. Sorted `manifest.json` keys per hassfest. Cleaned up `hacs.json` (dropped `domains` / `iot_class` — those keys belong in `manifest.json`, not the HACS manifest).

## 0.1.12

- **Fix Options flow validation error.** Saving the auto-source-sensor configuration with any unset parameter raised "Entity is neither a valid entity ID nor a valid UUID" because EntitySelector rejects `""` as a default value. Now uses `add_suggested_values_to_schema` to pre-fill current/default selections without putting voluptuous in a position to validate empty strings. Unset parameters can be left blank and the form will save cleanly.

## 0.1.11

- **Fix `backfill_statistics` silent no-op.** Was writing to `sensor.reef_tank_<param>_latest` regardless of the actual entity ID in your install. On installs that started on v0.1.0 (entities are `sensor.<param>_latest`) every write was discarded by the recorder because the statistic_id didn't match a registered entity, and there were no logs to explain why. Now uses entity-registry lookup by `unique_id` (same approach the dashboard generator uses), and logs at WARNING level: counts of points written per parameter, skipped parameters with no registered entity, and a final total. Run the service again after upgrading.
- Statistics writes during normal `record_reading` use the same registry lookup, so live charts work regardless of which version first registered the entities.

## 0.1.10

- **Fix the "Entity not found" issue in the auto-installed dashboard.** The dashboard generator now looks up actual entity IDs from HA's entity registry by `unique_id`, instead of guessing from the device-grouped naming pattern. Installs that started on v0.1.0 (entity IDs like `sensor.kh_latest`) will now get a working dashboard, as will installs that started on v0.1.2+ (entity IDs like `sensor.reef_tank_kh_latest`) — same logic, both work.
- Tank context section uses the `select` entities (Habitat / Problem / Active Test Method) when available, so they're tappable directly from the dashboard. Falls back to read-only sensors on older installs that don't have selects yet.
- Deprecated the static `dashboards/test-session.yaml` in the repo — entity IDs in static YAML can't anticipate which version first registered the entities. Always use the auto-installed dashboard.

## 0.1.9

- **Integration icon.** A custom `icon.png` (256×256, plus 512×512 @2x) ships with the integration so it no longer appears as "icon not available" in HACS / Devices & Services.
- **Dashboard install loud-mode.** Every milestone in the install path now logs at WARNING level (visible by default). You'll see exactly which strategy succeeded — Strategy 1 (in-memory collection), Strategy 2 (bootstrap), or Strategy 3 (direct file write) — or which one raised, with traceback.
- **New `reeftanktracker.diagnose_dashboard` service.** Dumps current install state to the log: what's on `hass.data["lovelace"]`, contents of the dashboards registry, whether the dashboard content blob is on disk, and the user-removed flag. Run this when the dashboard isn't appearing and paste the log output to debug.

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
