# Reef Tank Tracker

A Home Assistant custom integration for tracking reef aquarium water parameters, livestock inventory, and ICP-OES test results — designed for hobbyists who want one source of truth across manual Hanna/Salifert tests, automatic sensors (KH Keeper, ATO probes, etc.), and lab-quality ICP imports.

## What it does

- **Parameter tracking** — KH, pH, Calcium, Magnesium, Nitrate, Phosphate, Salinity, Temperature and others. Each parameter gets `latest`, `latest_at`, `latest_method`, `days_since_test`, and `drift` sensors.
- **Auto-save manual entry** — type a Hanna reading into a number entity, it's recorded immediately. No save button to forget. Edit by re-entering.
- **Test-date-correct history** — when you import an ICP test sampled three weeks ago, the parameter history reflects the sample date, not the import date. Hanna readings from yesterday still beat older lab samples in the "current value" view.
- **Livestock inventory** — track every coral, fish, invert and clam that goes in or out of the tank. Pure record-keeping; no derived logic.
- **Habitat & problem context** — `tank_habitat` and `tank_problem` selects feed Triton-style ICP setpoint lookups (when the [icpimport](https://github.com/jordanduffybd/icpimport) companion is installed).

## Install via HACS

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/jordanduffybd/reeftanktracker` as an Integration
3. Search "Reef Tank Tracker", install, restart HA
4. Settings → Devices & Services → Add Integration → "Reef Tank Tracker"

## Companion repo

- **[icpimport](https://github.com/jordanduffybd/icpimport)** — Triton ICP-OES importer (paste a public showroom URL, fetches the full habitat × problem recommendation matrix into HA)

## Status

Early development. Parameter tracking + manual entry first; inventory and ICP integration to follow.
