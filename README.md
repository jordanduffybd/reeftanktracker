# Reef Tank Tracker

A Home Assistant custom integration for reef aquarium husbandry — tracks water parameters, livestock inventory, and ICP-OES test results, then turns that data into per-element dosing recommendations that respect the realities of manual testing (sparse cadence, kit noise, dosing-pump drift).

Designed around one running tank (a Red Sea Reefer 425 G2) but generalises to any reef setup using ReefBeat-style RSDose 4 dosers, KH Keeper, and Hanna/Salifert chemistry kits.

Current version: **0.5.3** — see [CHANGELOG.md](CHANGELOG.md) for the per-release history.

## What it does

### Parameter tracking
- KH, pH, Calcium, Magnesium, Nitrate, Phosphate, Salinity, Temperature, Ammonia, Nitrite plus all major ICP-OES elements (heavy metals, macros, traces).
- Each parameter exposes `latest`, `latest_at`, `latest_method`, `days_since_test`, and `drift` (manual − auto-source) sensors.
- **Auto-save manual entry** — type a Hanna reading into a number entity on the Test Session dashboard view, it's recorded immediately.
- **Auto-source listener** — when an external sensor (KH Keeper, ATO probe, etc.) fires a state change, it's recorded into history with `source=auto` so the manual-vs-auto drift is visible.
- **Test-date-correct history** — ICP test sampled three weeks ago shows up in history at the sample date, not the import date. Recent Hanna readings still beat older lab samples in "current value" lookups.

### Per-element dosing advisors
Five recommendation engines, one per macronutrient:

| Advisor | Param | Default target | Supplements (built-in patterns) |
|---|---|---|---|
| Alkalinity | KH | 8.0–9.0 dKH | Foundation B, Tropic Marin Alkalinity Plus |
| Calcium | Ca | 420–440 ppm | Foundation A |
| Magnesium | Mg | 1300–1350 ppm | Foundation C |
| Nitrate | NO3 | 1–10 ppm | NPX, NO3:PO4-X, HR/LR Nitrate Remover |
| Phosphate | PO4 | 0.03–0.10 ppm | Quantum AR Phosphate, lanthanum, NPX |

Each advisor:
- Takes daily snapshots of the parameter + the sum of programmed dose across configured RSDose heads
- Suggests a daily-dose change with a reason ("KH median 7.2 dKH below band — increase to 4.2 mL/day")
- **Recommendation-only** — never writes to your doser. You apply changes in ReefBeat manually and acknowledge.
- **Sparse-cadence defaults** for manual testing: 90-day rolling window, min 2 readings, 30-day cooldown — tuned for 1–2 readings per month rather than auto-tester daily polling.
- **Auto-detect doser heads** by reading each RSDose head's `_supplement` label and matching against per-param patterns (e.g. "Foundation A" → Calcium advisor heads).

### Cross-parameter safety guards
- **Snowstorm guard** (Calcium): refuses to recommend raising Ca dose when alk > 10 dKH or Mg < 1200 ppm — both create CaCO3 precipitation risk. Holmes-Farley + BRStv consensus is "fix Mg first, then alk + Ca settle."
- **Floor guard** (NO3 / PO4): refuses to recommend more removal dose when NO3 ≤ 0.5 ppm or PO4 ≤ 0.03 ppm — stripping below these is the canonical dinoflagellate-outbreak setup.
- **Redfield-ratio warning**: alerts when NO3:PO4 mass ratio is outside [50:1, 200:1] — soft warning for cyano risk (low ratio) or dino risk (high ratio).
- **Water-change settling**: snapshots within 24h of a logged water change are excluded from slope calculations — handles the dilution step without truncating the rolling window.
- **Demand-change learning mode**: logging "added 3 SPS frags" enters the advisor into a learning period until N post-event snapshots accumulate.

### Auto-installed Lovelace dashboard
The integration registers a `reef-tank-tracker` dashboard on first run with 9 views:

1. **Test Session** — auto-saving entry rows for all parameters, current habitat/problem context
2. **Overview** — at-a-glance latest values + days-since-test
3. **Alk Advisor** — recommendation, KH history graph (7d), action buttons (acknowledge/dismiss/log water change/log demand change), show-your-work breakdown, recent activity log
4. **Calcium Advisor** — same shape as alk, 90-day Ca history graph
5. **Magnesium Advisor**
6. **Nitrate Advisor** — adds Redfield ratio + floor-guard surface
7. **Phosphate Advisor**
8. **Dosing Plan** — most-recent ICP test's habitat-aware dose plan (sorted by importance), with inline re-import form
9. **Diagnostics** — per-parameter method/drift/timestamp diagnostics

After a version upgrade with new views, call `reeftanktracker.regenerate_dashboard` to surface them.

### Action feedback
Every dashboard action (acknowledge, dismiss, water change, demand change, ICP import) fires a 10-second auto-dismissing toast in the bell-icon dropdown AND a `logbook_entry` event so the action shows up in the dashboard's "Recent activity" card. No more checking system logs to confirm a button worked.

### Livestock inventory + tank context
- Track every coral / fish / invert / clam by category, with added/removed-at timestamps and notes.
- `tank_habitat` and `tank_problem` selects feed Triton's habitat × problem dose-recommendation matrix when ICP tests are imported.

### ICP importer
- Paste a public Triton showroom URL → fetches the full habitat × problem recommendation matrix → writes test-date-correct history for every ICP-OES element + renders an active dose plan sorted by importance.

## Install via HACS

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/jordanduffybd/reeftanktracker` as an Integration
3. Search "Reef Tank Tracker", install, restart HA
4. Settings → Devices & Services → Add Integration → "Reef Tank Tracker"

After install, configure via Settings → Devices & Services → Reef Tank Tracker → **Configure**:
- **Auto-source sensors** — pick the sensor whose value should populate the parameter's `_latest` (e.g. KH Keeper for KH)
- **Target ranges** — per-parameter target band overrides
- **Alk dosing advisor** — enable, pick alk heads, supplement profile, target band, tunables
- **Calcium / Magnesium / Nitrate / Phosphate dosing advisors** — same shape, per-element

### Recorder retention

The advisor's empirical-potency learner reads up to `OPT_WINDOW_DAYS` (default 7) of dose / KH history from HA's recorder. The per-element advisors use `OPT_WINDOW_DAYS` defaults up to 90 days. HA's default recorder retention is **10 days** — short enough that the per-element advisors can't see their own full window. To use the full advisor windows AND retain enough trend data to answer "why has consumption changed over the last two months", bump retention in `configuration.yaml`:

```yaml
recorder:
  purge_keep_days: 90
```

(60 also works if disk is tight. Numeric advisor data is small — a 90-day bump typically adds <100 MB.) Restart HA to apply.

## Companion repos

- **[kh-keeper-bridge](https://github.com/jordanduffybd/kh-keeper-bridge)** — Home Assistant add-on that bridges Reef Factory KH Keeper readings + cuvette empty/fill/measure procedures into MQTT auto-discovered entities
- **[icpimport](https://github.com/jordanduffybd/icpimport)** — earlier standalone Triton ICP importer (now subsumed by this integration's built-in importer)

## Documentation

- **[docs/RELEASING.md](docs/RELEASING.md)** — how to cut a release HACS picks up (TL;DR: GitHub Releases, not bare tags). Auto-cut on `main` push when the manifest version bumps.
- **[docs/DASHBOARD.md](docs/DASHBOARD.md)** — auto-install behaviour, regenerating, customising
- **[docs/SERVICES.md](docs/SERVICES.md)** — every `reeftanktracker.*` service and its fields
- **[docs/IMPORTING.md](docs/IMPORTING.md)** — bulk-import historical CSVs (Aquarium Log app + Triton ICP exports)
- **[CHANGELOG.md](CHANGELOG.md)** — what changed per release, with HA Core compatibility lines
- **[tests/README.md](tests/README.md)** — running the 162-test suite

## Compatibility

Tested against **Home Assistant Core 2026.4.4**. Every CHANGELOG entry states the HA Core (and HAOS, when relevant) version it was verified on. Upgrading HA past the listed version isn't guaranteed to work — check the next release for an updated compat line first.

Python 3.11 and 3.12 supported (CI runs both).

## Status

Production-ready for the maintainer's tank (Red Sea Reefer 425 G2 with RSDose 4, KH Keeper, ReefBeat). The dosing-advisor algorithms are conservative-by-default (recommendation-only, sparse-cadence, ±10% step caps) and have been running in prod for several months across the alk advisor and weeks across the per-element advisors.

Other tank setups should work but haven't been exhaustively validated. File an issue on GitHub if your dosing pump / KH source / supplement names don't match the built-in patterns — auto-detect can be widened.
