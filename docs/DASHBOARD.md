# Dashboard

The integration auto-installs a Lovelace dashboard at **`/reef-tank-tracker`** when set up. It appears in the sidebar with a fishbowl icon.

## What's in it

Three views:

- **Test Session** — auto-saving entry rows for every input parameter (KH, Ca, Mg, …). Type a value, it's saved immediately. No save button.
- **Overview** — at-a-glance latest values + days-since-last-test for each parameter.
- **Diagnostics** — per-parameter detail rows: latest value, method, sample timestamp, drift vs auto sensor, days since manual test.

Each view is generated from the parameter list in `parameters.py`, so adding a new parameter to that file (and shipping a release) automatically extends the dashboard on next install.

## How auto-install works

On every integration setup:

1. We wait for the `homeassistant_started` event so HA's lovelace integration is fully loaded.
2. We register the dashboard with `lovelace.dashboards_collection.async_create_item` so it appears in the sidebar without an HA restart.
3. We write the dashboard's view config to `.storage/lovelace.reef_tank_tracker` via HA's `Store` helper.

If a dashboard at `/reef-tank-tracker` already exists, we skip registration but still rewrite the view config so updates ship cleanly.

## "I deleted the dashboard, now what?"

The integration honours your decision. Removing the dashboard sets a `user_removed_dashboard` flag in storage, and the integration will no longer recreate it on subsequent restarts.

To bring it back:

```yaml
# Developer Tools → Services
service: reeftanktracker.regenerate_dashboard
```

That clears the flag and re-installs a fresh layout.

## "I customised the dashboard and don't want it overwritten"

By default, every integration *update* (new release) will rewrite the auto-installed dashboard's view YAML. If you've edited it manually and want to keep your customisations:

**Option A — keep editing the auto-installed one.** Your changes live in `.storage/lovelace.reef_tank_tracker`. They'll be overwritten on next integration update / `regenerate_dashboard` call.

**Option B — copy it to a new dashboard.** Settings → Dashboards → ⋮ on Reef Tank → "Take control" or duplicate. Now the original is yours, the auto-installed one stays untouched, and you can deviate freely.

We're considering a "preserve user edits" mode for v0.2 — if you have strong feelings on this, raise an issue.

## Manual install (without the auto-installer)

If the auto-installer fails (HA Lovelace API mismatch on a new HA version, etc.), there's a YAML fallback at `dashboards/test-session.yaml` in the repo. Settings → Dashboards → Add dashboard → from YAML, point it at that file.

## Entity naming

All entities live under a single **"Reef Tank"** device for clean grouping in HA's UI. Entity IDs follow the pattern:

```
sensor.reef_tank_<param>_latest         e.g. sensor.reef_tank_kh_latest
sensor.reef_tank_<param>_latest_method
sensor.reef_tank_<param>_latest_at
sensor.reef_tank_<param>_days_since
sensor.reef_tank_<param>_drift
number.reef_tank_<param>_entry          e.g. number.reef_tank_kh_entry
select.reef_tank_habitat
select.reef_tank_problem
sensor.reef_tank_tank_habitat   (mirror of the select for read-only display)
sensor.reef_tank_tank_problem
```

If you reference these in your own dashboards/automations, use these IDs — they're stable across updates.

## Configuring auto-source sensors

Each parameter can be configured to fall back to a HA sensor's value when no fresh manual reading exists. The `*_latest` sensor uses this fallback automatically; the `*_drift` sensor uses it for manual-vs-auto comparison.

To configure: **Settings → Devices & Services → Reef Tank Tracker → Configure**. You'll see one row per input parameter with an entity selector. Defaults that we know about are pre-filled (KH Keeper, ATO probe). Leave a row empty if a parameter is manual or ICP-only.

The currently active auto-source for each parameter is exposed as the `auto_source_entity` attribute on its `*_latest` sensor — useful for debugging and in templates.

Changing the options reloads the integration automatically — no HA restart needed.
