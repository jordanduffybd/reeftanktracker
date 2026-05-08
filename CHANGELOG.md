# Changelog

> **Compatibility convention:** every release entry below states the HA Core (and Supervisor / HAOS, when relevant) versions it was developed and tested against. **Compatibility is verified on those versions only.** Upgrading HA past the listed version isn't guaranteed to work — check the next release for an updated compat line before upgrading. If you want to upgrade HA first and don't see a release here that lists the new version, hold off or test on a non-prod instance first.

## 0.5.1 — Calcium advisor UX polish (manual cadence + cold-start backfill + per-element potency)

**Tested against:** HA Core `2026.4.4` (dev). Focused UX pass on the 0.5.0 Calcium advisor based on real-prod feedback. **Mg / NO3 / PO4 advisors deferred to 0.5.2** — Ca needed to be solid first.

### Sparse-cadence defaults

The 0.5.0 defaults assumed daily testing (window_days=7, min_samples=5, cooldown=5d, correction_period=7d). For users testing **1–2 times per month, max 4** (the realistic manual cadence without an auto-tester), this never accumulates enough samples to compute. Reset for sparse manual testing:

| Setting | 0.5.0 (daily) | 0.5.1 (sparse manual) | Rationale |
|---|---|---|---|
| `window_days` | 7 | **90** | 90 days accumulates 3–6 readings at 1–2/month cadence |
| `min_samples` | 5 | **2** | Activates as soon as you have ONE comparison reading |
| `min_trend_days` | 3 | **2** | 2 consecutive out-of-band readings before suggesting (single noisy reading can't trigger action) |
| `min_samples_after_event` | 3 | **1** | Exit demand-change learning mode after 1 post-event reading |
| `cooldown_days` | 5 | **30** | One monthly testing cycle per cooldown |
| `correction_period_days` | 7 | **30** | Spread corrections over a month → gentler dose changes |

Auto-tester users (future Mastertronic Essential) bump min_samples and shrink window_days via the Options form for faster response cycles.

### Cold-start backfill from existing readings

The 0.5.0 advisor only captured snapshots from `record_reading` calls made AFTER the advisor was enabled, leaving `samples_used: 0` until you record a fresh reading. New `coordinator.async_backfill_advisor_snapshots(param_id)` synthesizes snapshots from existing Reading records (last 90 days, one per sample date, multiple readings same day averaged). Auto-fires once per HA session when the advisor sees zero snapshots — turns "you'll see a recommendation in a few weeks" into "you'll see one as soon as you've got history".

### Per-element supplement potency

Added `eff_per_mL_per_100L` field to the supplement profile schema. Generic float (units depend on `param_id`):
- Calcium: ppm Ca per mL per 100L (Foundation A = 2.0)
- Magnesium: ppm Mg per mL per 100L (Foundation C = 1.0)
- Nitrate / Phosphate: negative values for removers (0.5.2)

The advisor's `spec_efficiency_source` attribute now correctly says "profile: Red Sea Foundation A" instead of the misleading "default (...has no potency)" when a profile is selected with the field populated.

### Form UX

- **Per-field labels and descriptions** for the Calcium dosing advisor page in `strings.json` / `translations/en.json`. Fields now show clear titles ("Calcium dose head sensors") + helper text. Specifically warns about picking `_daily_dose` (NOT `_auto_dosed_today`) for the doser head sensor.
- **Removed unused "source" field** from the per-element advisor page. Was confusing UX and unused — the source for non-KH advisors comes from manual readings, not a real-time sensor. Will reintroduce when an auto-tester (Mastertronic Essential) lands.

### Algorithm message fixes

- `"Only 0 of 5 required KH snapshots in the 7-day window"` → `"Only 0 of 5 required Calcium snapshots in the 7-day window"` (param-aware via `cfg.param_label`).

### Migration

- Existing supplement profiles registered before 0.5.1 default to `eff_per_mL_per_100L=None`. Calcium/Magnesium advisors fall back to the parameter's built-in default potency (Foundation A=2.0; Foundation C=1.0) until the user re-registers with explicit `eff_per_mL_per_100L`.
- After install, recommend re-registering Foundation A and Foundation C with `eff_per_mL_per_100L: 2.0` and `1.0` respectively for cleaner attribute output.

## 0.5.0 — Calcium advisor + generalized per-element framework

**Tested against:** HA Core `2026.4.4` (dev).

First per-element advisor lands. Calcium uses the same algorithm engine as the alk advisor (7-day rolling median, ±10% step cap, demand-change-aware, hysteresis, cooldown, calibration warning) — parameterized so adding Magnesium / Nitrate / Phosphate is a small lift in subsequent releases.

**Phase plan (carrying forward from the 0.4.4 plan):**
- 0.5.0 (this release): Calcium advisor + framework
- 0.5.1: Magnesium advisor (trivial copy, different defaults)
- 0.5.2: Nitrate + Phosphate advisors with multi-supplement coordination + remover semantics

### What's new

- **`sensor.reef_tank_calcium_advisor_recommendation`** — same surface as the alk advisor sensor: state = suggested daily dose in mL, attributes carry the show-your-work breakdown (Calcium median, target band, current/suggested dose, change mL/%, confidence, reason, observed slope, samples used, etc.). State is `unavailable` until you opt in via Configure → "Calcium dosing advisor".
- **New "Calcium dosing advisor" Options page** under Configure. Same shape as the alk page: enabled toggle, dose head sensor(s), source sensor (for future Mastertronic Essential auto-source — leave blank for manual-only), supplement profile dropdown filtered to `param_id="calcium"` (you'll see Red Sea Foundation A here), spec efficiency, target band (default 420–440 ppm), and the algorithm tunables (window 7 days, cooldown 5 days, step cap 10%, hysteresis 5 ppm, etc.).
- **Snapshot-on-record-reading.** Without an auto-source sensor for Calcium (until Mastertronic), the advisor needs snapshots to come from manual entries. Each `reeftanktracker.record_reading` call for `parameter="calcium"` now also captures an advisor snapshot (with the current dose summed across the configured Ca heads). Same path will fire for Mg / NO3 / PO4 in subsequent releases — extends the pattern via `param_advisor.PARAM_DEFAULTS`.
- **New `param_advisor.py` module** holds the parameter-aware compute path (`compute_for_param(hass, coord, param_id)`). Wraps the existing parameter-agnostic `advisor.compute_recommendation` algorithm engine in a config + state-resolution layer that handles non-KH parameters. The alk advisor stays on its own `alk_advisor.py` code path for now (stable in prod; collapse later if/when the per-element work settles).
- **`AdvisorConfig` gains `param_label` + `value_unit`** so the algorithm's reason text adapts: "Calcium median 415.00 ppm is within target band..." instead of the hardcoded "KH median X dKH". Back-compat: defaults stay "KH" / "dKH" so the existing alk advisor text is unchanged.
- **Doser-unreachable message** generalized: "Doser daily-dose sensor is unreachable or zero (check ReefBeat / doser connectivity)..." (was "Alk doser..."). Applies to all advisors.

### Per-element defaults (Calcium, from research)

- Target band: 420–440 ppm (SPS-friendly; widen via Options for LPS-dominant)
- Step cap: ±10%
- Cooldown: 5 days
- Window: 7 days
- Hysteresis: 5 ppm (within typical test-kit noise)
- Foundation A potency: 2 ppm Ca per mL per 100L (used as default when the Foundation A profile is selected without a stored potency value)

### Future safety guards (deferred to 0.5.x)

The Ca/Mg/NO3/PO4 research (memorized at `~/.claude/projects/.../memory/reference_reef_dosing_research.md`) flagged several cross-parameter safety guards that need encoding:
- Refuse Ca-up recommendation when alk > 10 dKH OR Mg < 1200 (snowstorm risk)
- Cross-warn when Ca + alk advisors both want > 5% change in same week
- Refuse NO3/PO4 dose increase when below floor (0.5 ppm / 0.03 ppm)
- Redfield ratio warning (NO3:PO4 < 50:1 or > 200:1)
- HR → LR Nitrate Remover switch suggestion when NO3 ≤ 15 sustained 7+ days

These are scoped to 0.5.1 (single-supplement Mg + cross-Ca-alk guards) and 0.5.2 (multi-supplement NO3/PO4 with Redfield + floors).

### Tests

126 passing (unchanged). New per-element code paths tested manually end-to-end in dev (Ca advisor with 7 days of mock readings → produces real recommendation with parameter-aware reason text). Unit tests for `param_advisor` to follow in a fast follow.

### Post-install actions

1. Restart the integration so the new entities register.
2. Configure → "Calcium dosing advisor" → set enabled, pick Foundation A from supplement profile dropdown, set heads (which RSDose head doses Calcium), accept defaults for the rest.
3. Manual Calcium readings via the Test Session view will now also capture advisor snapshots — after ~5 readings you'll see the advisor produce a recommendation.

## 0.4.4 — supplement profiles get `param_id` (foundation for per-element advisors)

**Tested against:** HA Core `2026.4.4` (dev).

Foundation work for the per-element advisors (Ca, Mg, NO3, PO4) coming in 0.5.0. No user-visible feature change in this release on its own, but unblocks the per-element work cleanly.

### What changed

- **`add_supplement_profile` service** now accepts an optional `param_id` field (defaults to `"kh"` for back-compat). Pass a single string for single-target supplements OR a list (`["nitrate", "phosphate"]`) for **multi-target** supplements like Red Sea NO3:PO4-X that affect multiple parameters simultaneously.
- **`eff_dkh_per_mL_per_100L`** is now optional when `param_id` doesn't include `"kh"` (it has no meaning for ppm-based or remover supplements). When `"kh"` is included, the field remains required — the alk advisor needs a real potency to compute dose changes.
- **Coordinator** stores `param_id` always as a list internally (string input is normalized to a 1-element list). New `supplement_profiles_for(param_id)` helper checks list membership, so multi-target supplements surface in EACH per-element advisor whose parameter they affect.
- **Alk advisor's dropdown** filters to KH-targeting profiles. Non-KH supplements (e.g. Foundation A/C, NPX, the Quantum AR/LR/HR products) no longer appear in the alk Options dropdown — they'll surface in their own per-element advisor when 0.5.0 lands. Profiles claiming `"kh"` without a real `eff_dkh_per_mL_per_100L` are also excluded (defensive; partial coverage on a multi-target).
- **Auto-detect label patterns** are also filtered to KH-targeting so a phosphate / nitrate supplement label can't accidentally match against the alk doser's `_supplement` state.
- **`list_supplement_profiles`** service now groups output by `param_id` for clear at-a-glance debugging. Multi-target supplements appear in each of their target groups with an `also_targets=[...]` annotation showing the cross-param wiring. Builtin sentinels (`auto`, `custom`) are correctly tagged "(sentinel — no fixed potency)" instead of misleadingly tagged "non-KH supplement".

### Migration

- Profiles created before 0.4.4 (no `param_id` field on disk) are treated as `"kh"` everywhere — no manual migration needed.
- The 3 Quantum Aqua products registered in 0.4.3 (AR Phosphate / LR Nitrate / HR Nitrate) currently default to `"kh"` since they were registered before this release. They should be re-registered with proper `param_id` (`"phosphate"` / `"nitrate"` / `"nitrate"`) after 0.4.4 installs — call `remove_supplement_profile` then `add_supplement_profile` with the correct `param_id`.

### Tests

126 passing (was 117). Adds 6 coordinator tests (`param_id` defaults / accepts non-KH without eff / filters / legacy back-compat / multi-target appears in each / string normalizes to list) and 3 advisor tests (`all_profiles` excludes non-KH / excludes KH-targeting without eff / `all_label_patterns` excludes non-KH).

## 0.4.3 — Target ranges Options page + ICP debug-bundle parse trace + alk advisor robustness

**Tested against:** HA Core `2026.4.4` (dev).

### Alk advisor dashboard — graceful handling when devices are off

The Alk Advisor view used to render ~20 rows of "Unknown" when ReefBeat dosers were offline (because the algorithm returns None for most computed values). The user-facing dashboard now adapts:

- **Headline:** when state is `unknown` / `unavailable`, shows a "⏸️ Advisor paused" markdown banner with the actionable `reason` text large + prominent. When state is a real number, shows the regular suggested-daily-dose tile. (HA's `conditional` card switches between them automatically.)
- **Show your work:** rebuilt as a markdown card that only renders rows whose value is meaningful (not None / "unknown" / empty). Always-shown rows (target band, spec efficiency, calibration warning, confidence, reason) stay visible since those are configuration, not computed. Computed rows (KH median, current dose, suggested dose, observed slope, samples used, etc.) appear only when populated.

Result: when ReefBeat is off for maintenance, the dashboard collapses to a clean banner + 5 config rows instead of 20 rows of "Unknown". When ReefBeat returns, the full work breakdown appears automatically.

### Alk advisor robustness against transient ReefBeat outages

Caught during the user's 2026-05-08 ReefBeat maintenance window: when the alk doser's `daily_dose` sensor went briefly unavailable during an integration reload, the advisor sensor returned `state=None` (rendered as "unknown" in HA) AND failed to auto-recover when the doser came back. Two fixes:

- **Reorder advisor's exit branches.** `current_dose_mL` is now checked at the top of `compute_recommendation`. If the doser is unreachable, the user sees an actionable message ("Alk doser daily-dose sensor is unreachable or zero (check ReefBeat connectivity). Advisor will resume automatically when the sensor returns.") regardless of whether they're in learning mode, in-band, or trending. Previously a transient outage during learning mode would leave a stale "Learning mode after demand change..." reason while the actual cause was the doser sensor.
- **Subscribe to upstream state changes.** `AlkAdvisorSensor.async_added_to_hass` now also subscribes to state-change events on the configured `alk_head` sensors, the `kh_source` sensor, and the `calibration_warning` binary sensor (in addition to the existing `SIGNAL_ADVISOR_UPDATED` dispatcher). Without this, the sensor stayed stuck on the stale `None` until something else (an ack/dismiss/snapshot) fired the dispatcher. Now the moment ReefBeat comes back, the advisor recomputes and shows the real recommendation.

Regression test added covering the "current_dose=None during learning mode" scenario.



### Target ranges Options page

New "Target ranges" entry in Settings → Devices → Reef Tank Tracker → Configure. Per-parameter min/max override for the 10 home-testable parameters (KH, pH, Calcium, Magnesium, Nitrate, Nitrite, Ammonia, Phosphate, Salinity, Temperature). Defaults pre-fill from `parameters.py`; empty fields fall back to defaults. Stored in `entry.options` as `target_<param_id>_min` / `target_<param_id>_max`.

New `coordinator.get_target_range(param_id) -> (min, max)` helper resolves user override with graceful fallback:
- Both override values set → use them
- Partial override (only min OR only max) → fall back to both defaults (avoids inverted bands)
- Invalid override (non-numeric) → fall back to defaults
- Unknown parameter → `(None, None)`

Every `_latest` sensor now exposes three new attributes:
- `target_min` — effective min (user override or static default)
- `target_max` — effective max
- `in_target_band` — `True` / `False` / `None` (`None` when value or band unknown)

This is the foundation for: per-element advisors (Ca, Mg) reading their own bands, ICP Phase 2 (auto-set targets per habitat from Triton's matrix), and dashboard tile-color hints. The alk advisor's existing `OPT_TARGET_MIN/MAX` is unchanged — KH targets continue to live on the advisor page for now.

### ICP debug-bundle parse trace

The `_write_debug_bundle` path (called when `parse_triton_showroom` raises `ParserError`) now writes a `parse_trace.txt` alongside the existing `url.txt`, `page.html`, `error.txt`. The trace records what the parser COULD identify before giving up: html_bytes, contains_triton_marker, element_row_count, dose_group_count, rule_library_present, habitat_dropdown_present, and the first 10 element symbols seen. If Triton's HTML ever shifts, the trace tells us at a glance which selectors stopped matching.

### Tests

116 tests passing (was 108). Adds 5 coordinator tests for `get_target_range` (default fallback, override wins, partial override falls back, unknown param, invalid override) and 3 importer tests for `_build_parse_trace` (real fixture, empty input, non-Triton page).

## 0.4.2 — inline ICP import form on the Dosing Plan dashboard

**Tested against:** HA Core `2026.4.4` (dev).

Replaces the "go to Developer Tools → Actions to import a Triton ICP" workflow with an inline form on the Dosing Plan dashboard view. Mirrors the existing water-change / demand-change form pattern.

**New form entities:**
- `text.reef_advisor_form_icp_url` — Triton showroom URL
- `select.reef_advisor_form_icp_habitat` — habitat dropdown (defaults to current tank habitat)
- `select.reef_advisor_form_icp_problem` — problem dropdown (defaults to current tank problem)
- `text.reef_advisor_form_icp_sample_date` — optional ISO-format date (`YYYY-MM-DD`); blank → service defaults to today UTC

**New service:** `reeftanktracker.submit_icp_import_form` — reads the four form entity states server-side and forwards to `import_triton_url`. Raises `HomeAssistantError` if the URL field is blank. Habitat/problem fall back to the persistent tank state if not changed in the form, so the user just types a URL and clicks Import for the common case.

**Dashboard layout:** the Dosing Plan view now has a 3-column section with the import form on the left, the active scenario summary in the middle, and the help card on the right. Below that, the dose plan markdown card spans the full width. The `Last test taken` headline tile remains at the top.

The `import_triton_url` service is unchanged and still callable from Developer Tools for power users / scripting.

## 0.4.1 — alk advisor activity log on the dashboard

**Tested against:** HA Core `2026.4.4` (dev).

Adds an "Activity log" section at the bottom of the Alk Advisor dashboard view showing the last 7 days of:

- Acknowledgements + dismissals of the dosing recommendation
- Water-change and demand-change events the user logged
- Manual snapshot captures
- KH Keeper drop-test calibration changes (state changes on `number.kh_keeper_calibrate_kh_from_drop_test` and `sensor.kh_keeper_kh_adjustment`)

Implementation: HA Logbook card filtered to the alk advisor sensor (the integration already fires `logbook_entry` events tagged to that sensor for every user action) plus the two KH Keeper calibration entities. No new code paths — purely surfaces what's already being recorded.

After install, the integration's auto-installed dashboard regenerates and the new section appears at the bottom of the Alk Advisor view. No restart-blocking changes.

## 0.4.0 — ICP importer + Dosing Plan view + sensor/parameter cleanup

**Tested against:** HA Core `2026.4.4` (dev + prod), Supervisor `2026.04.2`, HAOS `17.2`.

Headline: paste a Triton public-showroom URL, pick the habitat + problem you want plan guidance for, get habitat-aware dose recommendations recomputed via Triton's own dose-calc API, and act on them from a new Dosing Plan dashboard view.

### ICP importer
- **`reeftanktracker.import_triton_url` service:** ingests a Triton showroom URL (e.g. `https://www.triton-lab.de/en/showroom/icp-oes/229019`), parses 39 element analyses + dose recommendations + the embedded ECS rule library + the share-time selected habitat/problem. Writes element readings via `record_reading(source=icp)` and persists the full test record (deduped by `test_id`). Deterministic parser — no LLM fallback. Optional `sample_date` arg overrides the parser default (which falls back to today's UTC date).
- **Habitat-aware dose recalc (Phase 1.5):** service accepts optional `habitat` + `problem` arguments. For elements whose setpoint changes under the chosen habitat (currently P + TNb per Triton's `eval.js`), POSTs to Triton's public `:1024` API (`https://www.triton-lab.de:1024/api/eval_a/get_dosage_info`) and overwrites the rendered `corrective_dose` with the recomputed value. Tank volume sourced from the alk advisor's `OPT_TANK_VOLUME_L` (default 425L). Other elements share one setpoint across habitats so their rendered dose is preserved. API failures fall back to the rendered dose silently.

### Dosing Plan
- **`sensor.reef_active_dosing_plan`** — TIMESTAMP device class. State is the **sample collection date** of the latest imported test (so HA renders it as "Last test: X days ago" rather than reflecting the import time). Importance-sorted recommendations + active scenario metadata (test_id, sample_date, imported_at, active_habitat, active_problem, rendered_for_habitat, rendered_for_problem, source URL, recommendations_count) exposed as attributes. Refreshes on `SIGNAL_ICP_TEST_RECORDED`.
- **New "Dosing Plan" dashboard view:** markdown card with importance-sorted plan (star ratings + corrective-dose strings + product references) + summary card showing the active scenario + headline TIMESTAMP tile for "last test taken." Re-running the import service with different habitat/problem args overwrites the active plan in place — useful when changing tank direction (e.g. Mixed Reef → SPS) or addressing a transient issue (e.g. Cyanobacteria).

### Sensor / parameter cleanup
- Dropped redundant `TankHabitatSensor` + `TankProblemSensor` (the `select.reef_tank_habitat` / `select.reef_tank_problem` entities are the single source of truth — orphaned `sensor.reef_tank_tank_habitat` / `sensor.reef_tank_tank_problem` registry entries from prior installs need a one-time prune in Settings → Devices).
- Moved `iodine` + `strontium` from `INPUT_PARAMETERS` → `ICP_ONLY_PARAMETERS` — neither is home-testable, so their number-input entities were misleading.
- Dropped `drift` / `days_since_test` / `last_method` sensors for ICP-only params (no manual data → meaningless).
- ICP-only sensors get an **"ICP" prefix** in friendly name AND entity_id (`sensor.reef_tank_icp_<element>_latest`, `..._last_sampled`) for visual separation from home-test data in entity lists and dashboards.

### Other
- Dev mirror script (`dev/tools/mirror_from_prod.py`) picks up the new kh-keeper-bridge sensors (`ph_pure_tank_water`, `ph_kh_test_water_reagent`, `refresh_ph_phase`, `refresh_ph_phase_eta`) — bumps the default mirror set from 45 → 49 entities.
- **108 unit tests** (was 75 in 0.3.0). New tests cover the per-habitat setpoint resolver, `latest_icp_test` sort + missing `imported_at` handling, rule-library extraction, ICP-test deduplication. Real Triton showroom HTML fixture (795 KB, captured 2026-05-06) drives the parser tests.

### Post-install actions
1. **Restart the integration** so the new entities register.
2. **One-time orphan prune:** Settings → Devices → Reef Tank Tracker → look for `sensor.reef_tank_tank_habitat` / `sensor.reef_tank_tank_problem` showing as Unavailable, delete them.
3. **Swap pH `auto_source`:** Options → Auto-source sensors, change `pH` from `sensor.kh_keeper_ph` → `sensor.kh_keeper_ph_pure_tank_water` (per kh-keeper-bridge ≥ 0.1.13).
4. **Drop-test calibrate the KH Keeper** before flipping `Enable advisor` ON — the alk advisor correctly downgrades confidence to "low" while the KH source's calibration warning is on.

### Deferred
Phase 2 (habitat × problem matrix → recommended target ranges) and Phase 3 (full 39-element ICP test viewer dashboard). The ECS rule-library identifier filter is a no-op for typical Triton data — every habitat × problem combo references ~43 element ids — so per-element setpoint recompute via the `:1024` API is the only useful per-habitat differentiator.

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
