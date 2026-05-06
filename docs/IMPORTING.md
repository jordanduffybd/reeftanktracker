# Importing historical data

`scripts/import_history.py` is a one-shot Python tool that pushes legacy
readings into Reef Tank Tracker via Home Assistant's REST API. It runs
locally (no HACS install needed) — no add-on, no integration changes.

## What it imports

- **Aquarium Log app CSVs** — the iOS/Android app's export format:
  ```
  parameter,value,createdAt,note
  Calcium,482,2023-12-05 17:45:30,
  KH,10.1,2023-12-05 17:45:30,
  ...
  ```
  Every row is recorded as a **manual Hanna ULR reading** (configurable
  via `--method`).

- **Triton ICP-OES CSVs** — the per-test export from your Triton
  account (`Reef tank - <date> (<id>).csv`). Each element row becomes
  an ICP reading; we also stash a full ICP test record via the
  `import_icp` service.

Sample-date timestamps are preserved end-to-end. Hanna readings from
2023 land at 2023 in HA's history, not "now". An ICP sampled three
weeks before its results came back lands on the sample date.

## Setup

1. Get a long-lived access token from HA:
   - Profile (bottom-left avatar) → **Long-Lived Access Tokens** → **Create Token**
   - Name it whatever (e.g. "reef import")
   - Copy the token — HA shows it once

2. Set environment variables (so they don't end up in shell history):

   ```bash
   export HA_URL='https://hass.your-domain'
   export HA_TOKEN='ey...your-long-token...'
   ```

## Usage

### Dry run first (always)

```bash
python3 scripts/import_history.py \
    --aquarium-log /path/to/Reef_measurements.csv \
    --triton "/path/to/Reef tank - 16.04.2025 (B-KJAZM8).csv" \
    --triton-sample-date 2025-04-16 \
    --triton-test-id B-KJAZM8 \
    --tz Australia/Melbourne \
    --dry-run
```

Lines starting with `DRY` show what *would* be sent. Counts at the end
tell you how many would import vs skip. **Review the output.**

### Real run

Drop `--dry-run` to push to HA:

```bash
python3 scripts/import_history.py \
    --aquarium-log /path/to/Reef_measurements.csv \
    --triton "/path/to/Reef tank - 16.04.2025 (B-KJAZM8).csv" \
    --triton-sample-date 2025-04-16 \
    --triton-test-id B-KJAZM8 \
    --tz Australia/Melbourne
```

Each row is sent via `POST /api/services/reeftanktracker/record_reading`.
You'll see one `✓` line per imported reading. Errors (auth, network)
will surface as Python exceptions — fix and re-run.

## Timezone handling

The Aquarium Log CSV stores `2023-12-05 17:45:30` with **no timezone**.
We need to attach one before sending to HA so the recorder gets the
correct moment in history.

Pass `--tz Australia/Melbourne` (or whatever your reefing zone is) and
the script will tag each timestamp with the right offset, **honouring
DST changes across the date range** (so Nov 2023 entries get +11:00
and June 2024 entries get +10:00, automatically).

If you skip `--tz`, the script falls back to the system timezone of the
machine running the import — fine if you're running on the same box
that did the recording, brittle otherwise.

## What about duplicates?

The integration doesn't dedupe automatically — calling `record_reading`
twice with the same value at the same timestamp creates two history
entries. To avoid this:

- **Don't run the importer twice on the same CSV** (or pass `--dry-run`)
- If you do, you can clean up via the storage file at
  `.storage/reeftanktracker_data` — it's just JSON, the `readings` list
  contains all entries. Stop HA, edit, restart.

ICP test records (the full lab dump) **are** deduplicated by
`test_id`, so re-running an ICP import overwrites cleanly.

## Re-running just the Triton CSV

```bash
python3 scripts/import_history.py \
    --triton path/to.csv \
    --triton-sample-date 2025-04-16 \
    --triton-test-id B-KJAZM8
```

Just run with `--triton` and skip `--aquarium-log`.

## Idempotency / future use

For ongoing ICP imports, prefer the **icpimport companion integration**
(coming in a separate repo) — it pulls from a Triton public showroom
URL with full habitat × problem matrix, no manual CSV download. This
script is here for legacy data and one-off backfills.

## Troubleshooting

- `urllib.error.HTTPError: 401 Unauthorized` — bad or missing token. Re-create one in HA.
- `urllib.error.HTTPError: 400 Bad Request` — usually a parameter mapping miss; the CSV has a parameter label we don't know. Add it to `AQ_LOG_MAP` in the script and re-run.
- `urllib.error.URLError: Cannot connect` — wrong `HA_URL`, or the URL needs `https://` and a port. Test with `curl $HA_URL/api/` and a `Bearer` header.
- Timestamps off by a few hours — pass the right `--tz`, or the system clock on the importing machine is wrong.
