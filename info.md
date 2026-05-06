# Reef Tank Tracker

Track every reef aquarium parameter in one place — manual Hanna/Salifert readings, automatic sensors, and ICP-OES lab results.

After install, add the integration via **Settings → Devices & Services → Add Integration → Reef Tank Tracker**.

Each parameter you enable creates entities like:

```
sensor.reef_kh_latest          # most recent value (manual, auto, or ICP)
sensor.reef_kh_latest_at       # timestamp of the sample
sensor.reef_kh_latest_method   # "Hanna ULR" / "Triton ICP" / etc.
sensor.reef_kh_days_since_test # int days since last manual test
sensor.reef_kh_drift           # manual vs auto delta
number.reef_kh_entry           # type a Hanna reading here, auto-saves
```

A `reeftanktracker.record_reading` service lets you log readings programmatically.

Pair with the **icpimport** custom integration to auto-ingest Triton lab reports.
