# Calibration Backtest — Requirements (v0.1 DRAFT)

**Subsystem name:** `calibration_backtest`
**Codename:** The Reckoning
**Spec status:** DRAFT (CAST opened in Loom v0.53.0 against OT-003)
**Threat context:** STANDARD (financial-decision-support; consumes only on-disk data; no network egress; no operator capital exposure beyond what the existing v1 pipeline already exposes)
**Lifecycle stage:** SPECIFYING

---

## 1. Why this exists

OT-003 (HIGH priority, OPEN since Loom v0.1.0): *"System must paper-trade against historical Polymarket resolutions before any real capital deployed. Need resolved-contract historical data from Polymarket to validate model predictions retroactively."*

Today the v1 system has:
- a daily-cadence pipeline that produces model probabilities and compares them to live Polymarket prices,
- a forward-going calibration loop (mispricing_detector's linkage pass writes `comparison_resolutions` rows as Polymarket markets resolve over time),
- a per-sector Brier score and reliability diagram in the daily report (calibration + reliability sections),
- a `pattern_library/classes/polymarket_resolution_calibration.py` meta-class scaffold flagged as "the linchpin for OT-006."

What is missing — and what this subsystem provides — is a **historical replay**: take a frozen point in time T₀, ask "what would the system have predicted then?", record those predictions, then check them against the Polymarket resolutions that subsequently occurred between T₀ and T₁. The forward-going calibration only scores predictions made on-or-after the day the system started running. The backtest scores predictions the system *would have made* if it had been running across the entire historical window covered by the operator's data corpus.

The v1 system is paper-analysis only (per OT-004). Even so, the claim that the system is decision-support — that its probabilities are worth the operator's attention — is unsupported until the backtest runs and produces a defensible calibration record. The backtest is a **gating artifact** for any operator reliance on the system, not a feature.

This subsystem is the **operator-driven** companion to the daily forward calibration. It runs on demand, parameterised, and produces a structured calibration record per run. It does not replace the daily report's reliability section; it complements it with a much wider historical window.

---

## 2. Scope and non-goals

### In scope (v1)

- A `razor-rooster calibration-backtest run` CLI subcommand parameterised by:
  - `--since DATETIME` and `--until DATETIME` defining the replay window (default: as far back as `polymarket_resolutions` has data, until 30 days before now to allow resolution settlement);
  - `--class-id <id>` (repeatable) restricting which event classes are replayed (default: every class registered in `pattern_library.registry`);
  - `--sector <name>` (repeatable) restricting by sector;
  - `--venue {polymarket}` (default: `polymarket`; future: `kalshi` once Kalshi has a comparable resolutions table);
  - `--lag-days N` (default: 7) requiring at least N days between a prediction's timestamp and the corresponding resolution's timestamp — this avoids contaminating the backtest with "predictions" from a moment that already includes settlement information;
  - `--output {terminal,json,markdown,html}` (default: terminal; markdown / html mirror the daily report's render conventions).
- A historical replay loop that, for each (event_class, ground-truth resolution) pair in the window:
  - reconstructs the data state at the prediction timestamp by querying `data_ingest` tables with a `WHERE source_publication_ts <= prediction_ts` filter,
  - invokes the existing `signal_scanner` posterior computation against that frozen state,
  - records the resulting model probability + reasoning trace metadata,
  - joins to the corresponding `polymarket_resolutions` row to get the observed outcome,
  - emits one `backtest_predictions` row per (class_id, condition_id, prediction_ts) triple.
- Aggregate reporting per backtest run:
  - overall Brier score across all scoreable predictions,
  - per-sector Brier (subset to sectors with ≥1 scoreable resolution),
  - per-class Brier (subset to classes with ≥1 scoreable resolution),
  - reliability diagram per sector (mirroring the daily report's bin structure for direct comparison),
  - count of skipped predictions per skip reason (insufficient lag, missing resolution, invalid resolution, insufficient data at prediction_ts).
- Persistence of every run as an append-only `backtest_runs` row with deterministic `run_id`, plus all `backtest_predictions` rows referencing that run.
- A `razor-rooster calibration-backtest list` subcommand listing past runs with summary metrics.
- A `razor-rooster calibration-backtest show <run_id>` subcommand re-rendering the run's summary report from persistence.
- A `razor-rooster calibration-backtest compare <run_id_a> <run_id_b>` subcommand surfacing how Brier / per-sector Brier / per-class Brier shifted between two runs (useful when the operator re-runs after a class definition or signature change).
- An upgrade of `pattern_library/classes/polymarket_resolution_calibration.py` from "empty-frame stub" to a real `_occurrences` query that joins `comparison_resolutions` to `polymarket_resolutions` so the meta-class produces real outputs, closing OT-006's scaffolding gap.
- A `/calibration-backtest` route in the operator GUI listing recent runs and their headline metrics, mirroring the existing `/watch`, `/digest`, and `/calibration` read-only patterns.

### Out of scope (v1)

- **No live order placement, no wallet integration, no trading SDK.** v1 remains paper-analysis only per OT-004. A backtest result that says the system is well-calibrated does not change the v1 contract; trading is v2 territory.
- **No Kalshi backfill** until `kalshi_connector` ships a `kalshi_resolutions` table. The CLI flag `--venue kalshi` is reserved but rejects with a typed error in v1.
- **No imputation of missing data.** If the data corpus at `prediction_ts` is too sparse to compute a posterior (e.g., a class with no observed precursor data before `prediction_ts`), the prediction is skipped and counted; it is never imputed.
- **No retraining or hyperparameter sweeps.** The backtest replays the system as it currently exists; comparing different system configurations is the operator's concern via separate runs.
- **No automated decision policy from backtest output.** The backtest is reporting; the operator decides whether the calibration is acceptable.
- **No streaming / incremental backtest.** Every run is a complete batch; partial runs are deferred to v2 if needed.

---

## 3. Tech stack

Same as the rest of the project:
- Python 3.11+, package `razor_rooster.calibration_backtest`.
- DuckDB for persistence (sharing the `data/trough.duckdb` store).
- pandas / numpy for the score aggregation arithmetic.
- click for the CLI surface.
- Jinja2 for the markdown / HTML rendering (mirroring `report_generator`).
- pytest + hypothesis for tests.
- mypy --strict on the subsystem; ruff lint + format.

Storage budget: 100 MB out of the 100 GB global cap, in line with `report_generator`'s allocation.

Migrations live under `calibration_backtest/persistence/migrations/` with version numbers in the **6001+** range (clear of data_ingest 1-999, polymarket_connector 1001-1999, pattern_library 2001-2999, signal_scanner 3001-3999, mispricing_detector 4001-4999, position_engine 5001-5999).

---

## 4. Threat context

`calibration_backtest` is **STANDARD**, not FULL:

- It reads only from on-disk DuckDB tables (`data_ingest.*`, `polymarket_resolutions`, `pl_event_classes`, `comparisons`, `comparison_resolutions`).
- It writes only to two new tables (`backtest_runs`, `backtest_predictions`) and the optional `backtest_traces` table (rendered text, separated for query performance).
- It performs no network egress, no authenticated API calls, no operator-input processing beyond CLI flags.
- It does not produce recommendations or position sizing; its outputs are descriptive statistics about the system's historical accuracy.

A **misuse** scenario is the operator using a favorable backtest result to over-rely on the system. The mitigation is the v1 framing already in place: every operator-facing rendered output runs through the imperative-language linter (REQ-RG-FRAME-001 carries forward), and the backtest report explicitly states that paper-analysis remains the v1 contract regardless of calibration outcome.

A **second misuse** is the operator running the backtest with a too-small lag and getting an artificially good score because the predictions implicitly contain settlement-window information. The mitigation is a hard `--lag-days` floor (default 7, configurable down to 1, never 0) plus a structured warning whenever the lag is below the recommended value.

---

## 5. Requirements

EARS-style with stable IDs and verification notes. Cross-references to Loom registry use the subsystem name, never file paths.

### 5.1 Run orchestration (REQ-CB-RUN-*)

**REQ-CB-RUN-001 — Deterministic run identifier**

When the operator invokes `razor-rooster calibration-backtest run` with a given parameter set, the subsystem shall compute a deterministic `run_id` from a SHA-256 hash of the canonicalized parameter tuple `(since_ts, until_ts, lag_days, sorted(class_ids), sorted(sectors), sorted(venues), library_version, system_revision)` so identical re-runs produce identical `run_id`s.
- Verification: unit test confirms that two invocations with the same parameters and the same on-disk corpus produce the same `run_id`; varying any input changes the `run_id`.

**REQ-CB-RUN-002 — Window enforcement**

The subsystem shall reject runs with `until_ts > now() - 30 days` to ensure the resolution window is mature, unless `--allow-recent` is explicitly passed.
- Verification: integration test rejects a run with `until=now()`, accepts the same run with `--allow-recent`.

**REQ-CB-RUN-003 — Library / system identity capture**

Every `backtest_runs` row shall record the `library_version` (from `pattern_library.version.LIBRARY_VERSION`) and the `system_revision` (a placeholder string today; computed from `git rev-parse HEAD` at run time once the project is under the workspace's version control).
- Verification: unit test confirms `library_version` is captured; the `system_revision` field is populated with a non-empty string.

**REQ-CB-RUN-004 — Idempotent persistence**

A second invocation with the same `run_id` shall not duplicate `backtest_runs` rows or `backtest_predictions` rows; instead it shall return the prior summary unchanged with a structured "cached" log message.
- Verification: integration test runs the same backtest twice; second run returns in <1 s and emits a cached-result log line; row counts in both tables remain stable.

**REQ-CB-RUN-005 — Failure isolation**

A failure to compute a posterior for one (class_id, condition_id) pair shall be recorded as a skipped prediction with reason in `{insufficient_data, exception}` and shall not abort the run.
- Verification: integration test forces an exception in the posterior pass for one class; the run completes; the skipped prediction is counted; aggregate metrics ignore that prediction.

### 5.2 Data freezing (REQ-CB-FREEZE-*)

**REQ-CB-FREEZE-001 — Time-honest source filter**

For each candidate prediction at `prediction_ts`, the subsystem shall query `data_ingest` source tables with a `source_publication_ts <= prediction_ts` filter so the posterior does not see data that was not yet observable.
- Verification: property-based test (`hypothesis`) generates synthetic source rows at varied timestamps and confirms that no row with `source_publication_ts > prediction_ts` ever appears in the posterior input.

**REQ-CB-FREEZE-002 — Lag enforcement between prediction and resolution**

For each (prediction_ts, resolution_ts) pair, the subsystem shall require `resolution_ts - prediction_ts >= lag_days` (default 7) before scoring the prediction; pairs failing this check are skipped with reason `insufficient_lag`.
- Verification: unit test rejects a synthetic pair with 3-day lag at default settings; accepts with `--lag-days 1`.

**REQ-CB-FREEZE-003 — Library and class definition pinning**

A run shall record the active `library_version` and the `definition_version` of every replayed class. If any class's `definition_version` has changed since a prior run, the new run shall **not** reuse that prior run's `run_id` even if other parameters match — the canonicalized parameter tuple in REQ-CB-RUN-001 includes `library_version`.
- Verification: unit test bumps a class's `definition_version`; confirms the recomputed `run_id` differs from the prior run's.

### 5.3 Replay loop (REQ-CB-REPLAY-*)

**REQ-CB-REPLAY-001 — Resolved markets as ground truth**

The replay loop shall enumerate `polymarket_resolutions` rows in `[since_ts, until_ts]` ordered by `resolution_ts ASC` and produce one prediction attempt per (class_id, condition_id) pair where an `class_market_mappings` row exists.
- Verification: integration test seeds three resolved markets with two mapped classes each; confirms six prediction attempts.

**REQ-CB-REPLAY-002 — Posterior reuse**

The replay shall invoke the existing `signal_scanner` posterior computation (not a fork or copy). Any change to scanner posterior logic must propagate to the backtest automatically.
- Verification: monkey-patched scanner posterior in tests confirms the backtest calls into it; no shadow implementation exists.

**REQ-CB-REPLAY-003 — Polarity-corrected outcome**

The observed outcome (`outcome_observed` from `comparison_resolutions`, or polarity-corrected from `polymarket_resolutions.winning_outcome_label` for new resolutions not yet linked) shall be projected into [0, 1] with the same polarity convention as `mispricing_detector.engines.linkage` so a `polarity = 'inverted'` mapping flips the observed outcome before scoring.
- Verification: unit test covers the four (model_prob ∈ {0.3, 0.7}) × (polarity ∈ {'aligned', 'inverted'}) combinations and confirms the squared error is computed against the polarity-corrected observed outcome in each case.

**REQ-CB-REPLAY-004 — Invalid resolutions excluded**

Resolutions where `polymarket_resolutions.invalidated = true` (per the existing linkage's `resolution_outcome = 'invalid'` convention) shall be excluded from scoring; they are not skipped silently but counted under reason `invalid_resolution` so the operator sees the rate.
- Verification: unit test seeds an invalid resolution; confirms it's counted under `invalid_resolution` and not scored.

### 5.4 Score aggregation (REQ-CB-SCORE-*)

**REQ-CB-SCORE-001 — Overall Brier score**

The subsystem shall compute the overall Brier score as `sum((model_p - observed)^2) / count` across every scoreable prediction in the run.
- Verification: unit test against a hand-computed reference of three predictions yields the expected Brier score within 1e-9.

**REQ-CB-SCORE-002 — Per-sector Brier score**

For each sector with ≥1 scoreable prediction, the subsystem shall compute that sector's Brier score using the same formula. Sectors with zero scoreable predictions shall be omitted from the per-sector aggregate but counted under a `zero_resolutions_sectors` field.
- Verification: unit test seeds two sectors with predictions, one with zero, confirms only the two appear in the per-sector aggregate and the zero-prediction sector appears in `zero_resolutions_sectors`.

**REQ-CB-SCORE-003 — Per-class Brier score**

For each class with ≥1 scoreable prediction, the subsystem shall compute that class's Brier score. Same omission convention as REQ-CB-SCORE-002.
- Verification: unit test parallel to REQ-CB-SCORE-002 across classes.

**REQ-CB-SCORE-004 — Reliability diagram per sector**

The subsystem shall produce a per-sector reliability diagram with `bin_count` bins (default 10, configurable via `--bin-count`) using the same binning convention as the daily `report_generator.engines.section_assemblers.reliability` assembler so the two outputs can be compared directly.
- Verification: integration test runs both the daily reliability assembler and the backtest reliability over the same input data and confirms bit-equal output for the overlapping time window.

**REQ-CB-SCORE-005 — Calibration shift highlights**

The compare subcommand shall surface, for every `(sector, class)` cell that exists in both runs:
- the absolute Brier delta,
- the percent change,
- a flag if the delta crosses the configured `brier_miscalibration_threshold` from the existing report config.
- Verification: integration test runs the same backtest twice, perturbs one class definition between runs, confirms the affected (sector, class) cell is flagged in the compare output.

### 5.5 Persistence (REQ-CB-PERSIST-*)

**REQ-CB-PERSIST-001 — Append-only run log**

A `backtest_runs` row shall be inserted at run start with `status = 'in_progress'`. On successful completion the row's `status` shall transition to `complete`; on failure it shall transition to `failed` with a captured `error_summary`. The append-only contract: rows are never deleted.
- Verification: integration test simulates a crash mid-run; confirms the row is in `in_progress`; a second run picks up the same `run_id` (per REQ-CB-RUN-004) and overwrites the run completion fields without orphaning the prior partial state.

**REQ-CB-PERSIST-002 — Per-prediction trace separation**

Per-prediction reasoning traces (rendered text + structured JSON) shall live in a separate `backtest_traces` table keyed on `(run_id, prediction_id)` so summary queries don't drag the trace blobs.
- Verification: query plan inspection confirms the run summary query reads from `backtest_runs` + `backtest_predictions` only, never `backtest_traces`.

**REQ-CB-PERSIST-003 — Disk budget enforcement**

A run that would push `calibration_backtest`'s disk footprint above 100 MB shall fail with a typed error before starting; the operator must explicitly raise the cap or prune older runs first.
- Verification: integration test sets a 1 KB budget, attempts a run that would exceed it, confirms the typed pre-flight rejection.

### 5.6 Operator surface (REQ-CB-CLI-*)

**REQ-CB-CLI-001 — Default-runnable**

`razor-rooster calibration-backtest run` with zero arguments shall produce a useful run against the default time window (full corpus minus 30-day tail) and the full registered class set. The operator should not need to read the docs to get a first result.
- Verification: integration test runs the bare command on a populated test corpus and confirms a non-empty run summary is produced.

**REQ-CB-CLI-002 — Imperative-language linter on every output**

Every operator-facing render (terminal, markdown, html) shall pass through `position_engine.frame.linter.check_text` before reaching stdout / disk / HTTP, mirroring the existing `report_generator` and `gui` framing contract.
- Verification: existing linter test pattern from `report_generator.tests.test_linter` is replicated for the backtest renderer.

**REQ-CB-CLI-003 — JSON output is machine-readable**

`--output json` shall produce a complete dump (run metadata, every aggregate, every prediction row) usable as input to a downstream notebook or external tool. JSON output bypasses the imperative-language linter (its consumer is a tool, not an operator) but the renderer still includes the standard disclaimer block as a top-level `disclaimer` field.
- Verification: integration test `json.loads`'s the output and confirms the schema matches the documented contract; presence of `disclaimer` field asserted.

**REQ-CB-CLI-004 — GUI surface**

The GUI shall expose `/calibration-backtest` listing recent runs (run_id, parameters, headline Brier, run timestamp) and `/calibration-backtest/{run_id}` showing one run's full summary. Read-only; no operator input beyond URL parameters.
- Verification: parallel to existing `gui/tests/test_routes.py::test_*` patterns; assert routes return 200 with seeded backtest data.

### 5.7 Pattern-library integration (REQ-CB-PL-*)

**REQ-CB-PL-001 — Real occurrences for the meta-class**

`pattern_library/classes/polymarket_resolution_calibration.py`'s `_occurrences` function shall be upgraded from the empty-frame stub to a real query joining `comparison_resolutions` -> `comparisons` -> `polymarket_resolutions`, returning resolved markets that had a logged comparison. The function preserves the `OccurrenceQuery` protocol signature `(conn) -> DataFrame` (no bind parameters); time-window filtering happens downstream via `pattern_library.engines.refresh._count_in_window`. The meta-class also bumps its `definition_version` from 1 to 2 so the semantic change propagates through `compute_run_id` per REQ-CB-FREEZE-003.
- Verification: three test surfaces — (a) unit test against a seeded `comparison_resolutions` + `comparisons` + `polymarket_resolutions` row confirms the upgraded `_occurrences` returns it; (b) polarity matrix test (4 cells: direct/inverted × yes/no) confirms `(model_p, observed)` is computed correctly without reading `cr.outcome_observed`; (c) integration count check seeds N rows and confirms `_occurrences` returns N (with exact midpoint split when filtered downstream). The empty-frame fallback path is removed from production code (AST grep returns zero matches).

**REQ-CB-PL-002 — No circular dependency**

`calibration_backtest` may consume from `pattern_library`, `signal_scanner`, `mispricing_detector`, `polymarket_connector`, `data_ingest`, `report_generator`, and `position_engine`. None of those **seven** subsystems may import from `calibration_backtest`. The pattern_library meta-class upgrade in REQ-CB-PL-001 must not introduce such a back-edge — the meta-class queries DuckDB directly, not through `calibration_backtest`.

The 7-package list reconciles a prior drift between this requirement (5 packages) and `CALIBRATION_BACKTEST_DESIGN.md` §4.2 (7 packages — added `report_generator` and `position_engine`). The 7-package list is canonical going forward; T-CB-044 and T-CB-054's import-graph tests both reference this list.
- Verification: import graph test confirms no `from razor_rooster.calibration_backtest` import appears in any of the seven listed modules.

### 5.8 Performance (REQ-CB-PERF-*)

**REQ-CB-PERF-001 — Wall-clock**

A backtest run replaying 5 years of Polymarket resolutions across the full v1 seed library (8 classes, ~100 resolutions per year mapped to those classes for an upper-bound estimate of ~4000 prediction attempts) shall complete in under 5 minutes on the operator's reference hardware (EliteBook G8: i7-8665U, 16 GB DDR4, NVMe SSD).
- Verification: smoke test on a synthetic corpus sized to the upper bound; failing the budget triggers a `pytest.warning` (not a fail) so the test surfaces but doesn't block CI on slow runners; the actual hardware budget is verified manually per OT-005.

**REQ-CB-PERF-002 — Memory**

Peak resident memory during a run shall stay under 2 GB on the reference hardware so the backtest doesn't swap or compete with concurrent daily-cycle work.
- Verification: same smoke test, instrumented with `resource.getrusage`; warning-not-fail at 2 GB threshold.

---

## 6. Open questions (OQ-CB-*)

These ride into the design phase, not the requirements phase.

**OQ-CB-001 — Scanner posterior at frozen time**

`signal_scanner.engines.scanner` currently computes posteriors against "now"-shaped state. Does it need refactoring to accept an explicit `as_of_ts` parameter, or can the backtest precompute the time-frozen state and call the existing entry point unchanged? Resolved in the design phase by reading the scanner's actual signature.

**OQ-CB-002 — Class-market mapping at prediction time**

`class_market_mappings` is operator-curated and changes over time. Should the backtest replay use the mapping that was active at `prediction_ts` (requires schema bump on `class_market_mappings` to track historical state), or use the current mapping (faster, but may produce predictions for class-market pairs the operator wouldn't have made at `prediction_ts`)? Resolved in design.

**OQ-CB-003 — Backfill compare with absolute Brier vs. delta**

When the operator runs `compare A B`, should the report rank cells by absolute Brier change, by percent change, or by a configurable knob? Resolved in design after the operator UX is fleshed out.

**OQ-CB-004 — Trace verbosity**

Per-prediction traces include the full reasoning chain. For a 4000-prediction run that's significant disk. Should traces be opt-in via `--with-traces`, sampled at a fixed rate, or always-on with aggressive compression? Resolved in design after measuring trace size on a real corpus.

**OQ-CB-005 — Polarity convention compatibility**

Confirmed during DRAFT review: `mispricing_detector.engines.linkage` records `polarity_at_comparison` on `comparison_resolutions` rows, but the live `class_market_mappings` polarity may have flipped since. The backtest must use the polarity that was active at `prediction_ts`, not the current one (per OQ-CB-002 resolution). Document the reconciliation in design.

**OQ-CB-006 — Bin alignment with daily reliability**

The daily `report_generator.engines.section_assemblers.reliability` uses `reliability_bin_count` from config (default 10). The backtest must produce a comparable diagram. Should the two share a bin-count source (from `report.yaml`), or allow the backtest to override per run? Resolved in design.

---

## 7. Success criteria

A v1 ship of this subsystem succeeds if all of the following hold:

1. The operator can run `razor-rooster calibration-backtest run` and get a structured calibration record covering the full historical Polymarket-resolutions corpus in under 5 minutes on reference hardware.
2. The output is a defensible answer to the OT-003 question: "Has this system, as currently configured, been historically well-calibrated against Polymarket-resolution ground truth?" The operator can read the per-sector Brier breakdown and reliability diagrams and form a view.
3. The backtest's reliability diagrams are bit-equal to the daily report's reliability diagrams over the overlapping window (REQ-CB-SCORE-004), so the operator never has two contradictory calibration stories.
4. OT-006's pattern_library scaffolding (`polymarket_resolution_calibration` meta-class) is no longer a stub; it produces real occurrences (REQ-CB-PL-001).
5. Every operator-facing render passes the imperative-language linter (REQ-CB-CLI-002), preserving the v0.2.0 educational framing end-to-end.
6. The mispricing_detector / position_engine / report_generator subsystems require zero changes (other than the pattern_library scaffold upgrade) to ship the backtest. The backtest is purely a downstream consumer.
7. The persistence schema sits cleanly in the 6001+ migration range; no FRAY against existing subsystems' schemas; no pin cascade.

The v1 ship explicitly does **not** require:

- A favourable calibration result. A backtest that reveals poor calibration is a successful run; the system was built precisely to surface this.
- Trading integration of any kind. v1 remains paper-analysis only per OT-004.

---

## 8. References

- LOOM v0.53.0 — `razorrooster.md`. Adds `calibration_backtest` to the subsystem registry under SPECIFYING when this requirements document is approved.
- OT-003 — open thread "Backtesting calibration — model accuracy before live capital" (HIGH). Status: `CAST_OPENED` in v0.53.0; advances to `IN_DESIGN` when the design phase begins.
- OT-006 — open thread "Calibration backtest — model probability vs. observed outcomes" (MEDIUM). This subsystem is the operator-driven historical companion; the daily report's reliability section remains the forward-going version.
- `mispricing_detector` Requirements/Design/Tasks v0.1.0 — for `comparison_resolutions` schema (read-only consumer).
- `pattern_library` Requirements/Design/Tasks v0.1.0 — for the `polymarket_resolution_calibration` meta-class scaffold to be upgraded per REQ-CB-PL-001.
- `signal_scanner` Requirements/Design/Tasks v0.1.0 — for the posterior computation entry point reused per REQ-CB-REPLAY-002.
- `polymarket_connector` Requirements/Design/Tasks v0.1.0 — for the `polymarket_resolutions` table that anchors the ground truth.
- `report_generator` Multi-Venue Calibration Supplement (v0.36–v0.43 evolution-log entries) — for the Brier / reliability conventions the backtest mirrors.

Content drawn from the in-tree `razorrooster.md` evolution log, `specs/MISPRICING_DETECTOR.md`, `specs/PATTERN_LIBRARY.md`, `specs/SIGNAL_SCANNER.md`, and `specs/POLYMARKET_CONNECTOR.md`.
