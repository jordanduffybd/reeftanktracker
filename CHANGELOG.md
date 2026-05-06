# Changelog

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
