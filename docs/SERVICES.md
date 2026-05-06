# Services

All services live under the `reeftanktracker.*` namespace. Call them from
**Developer Tools → Services**, automations, or scripts.

## `record_reading`

Log a single parameter reading (manual, ICP, or auto-source).

```yaml
service: reeftanktracker.record_reading
data:
  parameter: kh           # required, parameter id (e.g. kh, calcium, phosphate)
  value: 8.4              # required
  unit: dKH               # optional — defaults to parameter's unit
  method: Hanna ULR       # optional — what test method
  source: manual          # manual (default) | auto | icp
  sample_taken_at: "2026-04-01T08:30:00+10:00"   # optional, defaults to now
  test_id: B-KJAZM8       # optional, ICP test reference
  notes: "After water change"   # optional
```

`sample_taken_at` is the **canonical timestamp**. For ICP imports use the lab's
sample date, not the import date — the dashboard shows readings on a
sample-date timeline so a Hanna run yesterday correctly outranks an ICP
sampled three weeks ago even if the ICP was just imported today.

## `add_inventory`

Register a coral, fish, invert, clam, or other inhabitant.

```yaml
service: reeftanktracker.add_inventory
data:
  name: "Acropora millepora 'purple'"   # required
  category: coral                       # required: coral | fish | invertebrate | clam | anemone | macroalgae | other
  type: SPS                             # optional sub-type
  added_at: "2024-08-15"                # optional, defaults to today
  count: 1
  notes: "Top-right rockwork, high light"
  photo: "/local/corals/acro_mille_purple.jpg"
```

## `remove_inventory`

Mark an inventory entry as removed (does **not** delete history — `removed_at`
is set instead).

```yaml
service: reeftanktracker.remove_inventory
data:
  id: <uuid from inventory list>
  removed_at: "2025-01-10"   # optional, defaults to today
```

## `set_habitat`

Update the active habitat or problem context. Affects which Triton
recommendation slice the dashboard renders (once `icpimport` is in place).

```yaml
service: reeftanktracker.set_habitat
data:
  habitat: "Mixed Reef"   # one of HABITATS in const.py
  problem: "None"         # one of PROBLEMS in const.py
```

You can also use the **Habitat** / **Problem** select dropdowns on the dashboard
— same effect.

## `import_icp`

Stash a full ICP test record (called by the icpimport companion). You won't
normally invoke this manually.

```yaml
service: reeftanktracker.import_icp
data:
  test_record:
    test_id: B-KJAZM8
    sample_date: "2025-04-01"
    source_url: "https://www.triton-lab.de/en/showroom/icp-oes/229019"
    elements: { ... }
    matrix: { ... }
```

## `regenerate_dashboard`

Force a fresh build of the auto-installed Reef Tank dashboard.

```yaml
service: reeftanktracker.regenerate_dashboard
```

Clears the "user-removed" flag (so calling this after manually deleting the
dashboard brings it back) and rewrites the view YAML based on the current
parameter list. Useful after upgrading the integration if the auto-install
didn't pick up new parameters.
