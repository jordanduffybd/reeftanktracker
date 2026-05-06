# Dev environment

Local Home Assistant dev stack for testing the `reeftanktracker`
integration **before** installing it on prod. The integration source is
symlinked from `dev/config/custom_components/reeftanktracker` to the
real `custom_components/reeftanktracker/` — edit the integration in
place, restart the dev HA container, see changes immediately.

A small mirror service streams selected entity states from prod's HA to
dev's HA via REST so the dev environment looks realistic without
running `kh-keeper-bridge` (which would fight with prod for control of
the physical KH Keeper device).

## What's running

- **`homeassistant`** — HA Container at <http://localhost:8123>
- **`mosquitto`** — MQTT broker at `localhost:1883` (anonymous, local-only).
  Idle unless you add an MQTT integration in dev. Available so future
  MQTT-driven tests work without extra setup.
- **`mirror`** — Python service polling prod every 60 s and POSTing
  state to dev. Read-only on prod. Defaults to **45 entities**: the
  KH Keeper surface (5) plus the 5 read-only sensors (`supplement`,
  `daily_dose`, `auto_dosed_today`, `manual_dosed_today`, `status`)
  for each of the 8 heads across both RSDOSE4 units. Configurable via
  `.env`.

## First-time bootstrap

```bash
# 1. Spin up HA + Mosquitto. Mirror is built but won't start until step 4.
cd dev
docker compose up -d homeassistant mosquitto

# 2. Open http://localhost:8123, complete onboarding (any name/timezone
#    works — it's throwaway). Skip the integrations step.

# 3. Profile (bottom-left) → Security → "Long-lived access tokens"
#    → Create token → copy it.

# 4. Set up env. The HA_PROD_TOKEN is the same one used by
#    internal/scripts/expose_ha_entities.py.
cp .env.example .env
$EDITOR .env       # fill HA_PROD_TOKEN and HA_DEV_TOKEN

# 5. Start the mirror service.
docker compose up -d mirror

# 6. Watch the mirror logs to confirm it's pulling state.
docker compose logs -f mirror
```

After ~60 s, dev's `Developer Tools → States` should show the mirrored
entities (`sensor.kh_keeper_kh`, `sensor.rsdose4_1881531676_head_2_*`,
etc.).

## Setting up reeftanktracker in dev

In the dev HA UI:

1. **Settings → Devices & Services → Add Integration → "Reef Tank Tracker"**.
   Confirm the single-instance setup screen.
2. Click **Configure** on the new integration → **Alk dosing advisor**.
3. Pick the mirrored entities:
   - **Alk head entity_ids:** `sensor.rsdose4_1881531676_head_2_daily_dose`
   - **KH source:** `sensor.kh_keeper_kh`
   - **Calibration warning:** `binary_sensor.kh_keeper_calibration_due_warning`
4. Leave **Enable advisor** off until you've seeded snapshot history
   (the algorithm needs `min_samples` daily snapshots to fire). You can
   enable it once the snapshotter has run a few cycles, or seed
   snapshots manually via Developer Tools → Services →
   `reeftanktracker.record_reading` plus direct .storage edits.

The dashboard at `/reef-tank-tracker` populates automatically.

## Common operations

```bash
# Live integration code reload (after editing custom_components/reeftanktracker)
docker compose restart homeassistant

# Tail HA logs
docker compose logs -f homeassistant

# Tail mirror logs
docker compose logs -f mirror

# Stop everything
docker compose down

# Nuke everything (including HA's storage — fresh onboarding next time)
docker compose down -v
rm -rf config/.storage config/.cloud config/home-assistant_v2.db*

# Disable the mirror without removing the container
echo MIRROR_ENABLED=false >> .env
docker compose up -d mirror
```

## Adding more entities to the mirror

Edit `.env` and set `MIRROR_ENTITIES` to a comma-separated list:

```env
MIRROR_ENTITIES=sensor.kh_keeper_kh,sensor.kh_keeper_ph,binary_sensor.foo
```

Then `docker compose up -d mirror` to restart it with the new list.
The default list (when `MIRROR_ENTITIES` is empty) lives in
`tools/mirror_from_prod.py:DEFAULT_ENTITIES`.

## What the mirror doesn't do

- **Does not push to prod.** Read-only on prod via `GET /api/states`.
- **Does not preserve recorder history.** Dev HA's recorder builds its
  own history from the mirror's polling cadence — fine for testing the
  advisor's "live state" path, but the advisor's snapshot history
  (which is what the algorithm actually reads) accumulates separately
  in dev as the snapshotter fires nightly.
- **Does not run `kh-keeper-bridge` or any other prod add-on.** Those
  stay on prod. Dev mirrors the resulting entity states only.
