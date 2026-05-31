# CALIBRATION_BACKTEST — Implementation Tasks

**Subsystem:** `calibration_backtest`
**Codename:** The Reckoning
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Threat context:** STANDARD (financial-decision-support; on-disk data only; no network egress; no operator capital exposure beyond v1 pipeline)
**Last updated:** 2026-05-30
**Companion specs:**
- Requirements: `CALIBRATION_BACKTEST.md` v0.1.0
- Design: `CALIBRATION_BACKTEST_DESIGN.md` v0.1.0

**Hard prerequisites:**
- `data_ingest` Phase 0–4 tasks must be DONE — the backtest reuses DuckDBStore, staging-merge, migrations, source-publication-ts columns, and structured logging.
- `polymarket_connector` resolution-ingestion tasks must be DONE — the replay loop iterates `polymarket_resolutions` rows.
- `pattern_library` Phases 0–7 must be DONE — the backtest invokes the registered class predicates and reuses the `polymarket_resolution_calibration` meta-class.
- `signal_scanner` posterior computation must be importable — the replay loop calls `signal_scanner.engines.posterior.posterior_with_ci()` directly.
- `report_generator` reliability binning must be importable — score aggregation calls `report_generator.engines.section_assemblers.reliability` for bit-equal bin alignment.
- `position_engine.frame.linter.check_text` must be importable — every operator-facing render passes through the framing linter.

Task IDs prefixed `T-CB-NNN`.

---

## Phase 1 — Bootstrap

> Bootstrap establishes the foundational infrastructure for `calibration_backtest`: package skeleton, versioning, error types, core data models, run-id determinism, and system-state capture. Tasks T-CB-001..T-CB-004 execute with no external phase dependencies. T-CB-005 (CLI scaffolding) depends on T-CB-001..T-CB-003. T-CB-006 runs verification gates and depends on all prior tasks in this phase.

### T-CB-001 — Scaffold package structure and initialization
**Depends on:** none.
**References:** design §3.1, §3.2.
**Deliverables:**
- `razor_rooster/calibration_backtest/` directory tree with subdirs: `engines/`, `persistence/`, `persistence/migrations/`, `config/`, `tests/`, `tests/fixtures/`.
- Root `__init__.py` with public API placeholder exports (`run_backtest`, `compare`, `list_runs`, `show_run`).
- `version.py` with `SUBSYSTEM_VERSION` constant (e.g., `'0.1.0-bootstrap'`) and stub `compute_run_id()` signature.
- `errors.py` with exception classes: `RecentWindowError`, `DiskBudgetError`, `NoPolarityError`, `InsufficientPrecursorData` (each with `__repr__` for structured logging).
- `config/backtest.yaml` with defaults: `default_lag_days=7`, `minimum_lag_days=1`, `disk_cap_mb=100`, `max_workers=4`, `trace_compression=zstd`, `trace_compression_level=3`.
- Empty `__init__.py` stubs in `engines/`, `persistence/`, `tests/` subdirectories.
**Verification:** `python -c "import razor_rooster.calibration_backtest"` succeeds; pytest discovers and runs zero tests.
**Out of scope:** any logic beyond the skeleton.

### T-CB-002 — Implement version and run-id computation logic
**Depends on:** T-CB-001.
**References:** REQ-CB-RUN-001, REQ-CB-RUN-003, REQ-CB-FREEZE-003, design §3.4, §3.5.
**Deliverables:**
- `version.py` extended with `resolve_system_revision()` that: (1) attempts `git rev-parse HEAD`; (2) falls back to env `RAZOR_ROOSTER_SYSTEM_REVISION`; (3) falls back to `pkg:<version>` from `importlib.metadata`; (4) returns `'unversioned'` as final fallback. Wraps `GitNotInstalledError`, `NotAGitRepoError`, `GitCommandError`.
- `compute_run_id(params: RunParameters, library_version: int, system_revision: str) -> str` canonicalizing input as JSON with exact key order and sorted arrays per design §3.4, returning SHA-256 hex digest. The canonicalized tuple includes per-class `definition_version` values resolved from `pattern_library` so any change to a class's `definition_version` propagates into the hash (REQ-CB-FREEZE-003).
- `RunParameters` dataclass with fields: `since_ts`, `until_ts`, `lag_days`, `class_ids: set[str]`, `sectors: set[str]`, `venues: set[str]`, `allow_recent: bool`.
- `resolve_class_definition_versions(class_ids: set[str]) -> dict[str, int]` calling `pattern_library.list_classes()` to capture each replayed class's current `definition_version`; the returned mapping feeds into the canonicalized hash input alongside `library_version` (REQ-CB-FREEZE-003).
**Verification:**
- Unit test: `compute_run_id` produces identical hash for permuted input lists (e.g., `class_ids` in different order).
- Unit test: hash changes when any single parameter mutates (`since_ts`, `library_version`, etc.).
- Unit test: mock git command failure and verify fallback chain (env var → package version → `'unversioned'`).
- Unit test (REQ-CB-FREEZE-003): hold all other parameters constant; bump a single class's `definition_version` from 1 → 2; assert `compute_run_id` returns a different hex digest from the prior run, confirming definition-version pinning propagates through the hash.
**Out of scope:** wiring into replay loop (covered in Phase 3).

### T-CB-003 — Define core data models and dataclasses
**Depends on:** T-CB-001.
**References:** design §3.3, §3.13.
**Deliverables:**
- `models.py` with frozen dataclasses: `BacktestRun` (run_id, since_ts, until_ts, lag_days, class_ids_json, sectors_json, venues_json, library_version, system_revision, started_at, completed_at, status, error_summary, predictions_total, predictions_scored, predictions_skipped, overall_brier, summary_json, bin_count_global, bin_count_per_sector_json, fallback_polarity_count, allow_recent, disclaimer_version), `BacktestPrediction` (run_id, prediction_id, class_id, condition_id, venue, sector, prediction_ts, resolution_ts, model_p, observed, polarity, polarity_source, mapping_mismatch_warning, definition_version, status, skip_reason, brier_contribution), `BacktestTrace` (run_id, prediction_id, trace_json_compressed, compression_algorithm, decompressed_size_bytes).
- `ScoreSummary` dataclass (overall_brier, per_sector, per_class, zero_resolutions_sectors, fallback_polarity_count, fallback_polarity_rate).
- `CompareCell` dataclass (sector, class_id, brier_a, brier_b, delta_absolute, delta_percent, crossed_miscalibration_threshold, present_in, optional trace_diff_summary).
- `__post_init__` validation on `BacktestRun`: `status in {'in_progress', 'complete', 'failed'}`; `predictions_scored <= predictions_total`.
- `__post_init__` validation on `BacktestPrediction`: `status in {'scored', 'skipped'}`; if `skipped` then `skip_reason` non-null.
**Verification:**
- Unit test: instantiate each model with valid inputs; verify frozen invariant (no `__setattr__` after init).
- Unit test: invalid status values raise `ValueError` with structured message.
**Out of scope:** persistence operations (Phase 2).

### T-CB-004 — Implement freezer and lag-validation logic
**Depends on:** T-CB-001.
**References:** REQ-CB-FREEZE-002, design §3.5.
**Deliverables:**
- `engines/freezer.py` with `freeze(prediction_ts: datetime) -> FrozenState | None` that queries `data_ingest` source tables (concretely: `bls_jolts_observations`, `bls_ces_observations`, `bea_personal_income_observations`, `fred_observations`, `census_retail_observations`, and any registered ingest source declaring `source_publication_ts`; canonical list in `data_ingest` spec §3.2 / `DATA_INGEST_DESIGN.md` §3.3) with `WHERE source_publication_ts <= prediction_ts`; returns `FrozenState` wrapping a read cursor; returns `None` if any queried source lacks `source_publication_ts` (logs `source_data_not_frozen`).
- `FrozenState` class implemented as a context manager with `close()` releasing resources.
- `derive_prediction_ts(resolution_ts, lag_days)` returning `resolution_ts - timedelta(days=lag_days)`.
- `validate_lag(resolution_ts, prediction_ts, lag_days)` returning `(resolution_ts - prediction_ts).days >= lag_days`.
**Verification:**
- Unit test: synthetic source rows at varied `source_publication_ts`; `freeze()` admits rows with `source_pub_ts <= T`, rejects `> T` (boundary equality preserved).
- Unit test: `validate_lag` rejects pair with 3-day lag at default 7-day setting; accepts with `--lag-days 1`.
- Unit test: source lacking `source_publication_ts` column → `freeze()` returns `None` without exception.
**Out of scope:** orchestration into replay loop (Phase 3).

### T-CB-005 — Wire skeleton CLI entry points and argument parsing
**Depends on:** T-CB-001, T-CB-002, T-CB-003.
**References:** REQ-CB-CLI-001, design §3.9.
**Deliverables:**
- `cli.py` with Click command group `calibration-backtest` and stub subcommands `run`, `list`, `show`, `compare`, `prune` returning `'Not implemented'` placeholders.
- `run` command parses: `--since`, `--until`, `--lag-days` (default 7, min 1), `--class-id` (repeatable), `--sector` (repeatable), `--venue` (default `polymarket`), `--bin-count`, `--bin-count-per-sector` (repeatable `sector=N`), `--allow-recent` (flag), `--output` (choice `terminal|markdown|html|json`, default `terminal`).
- `run` builds a `RunParameters` instance and calls placeholder `run_backtest(params)` which logs input and returns an empty `BacktestRun`.
- `list`, `show`, `compare`, `prune` parse flags per design §3.9 and return stub responses.
**Verification:**
- Unit test: every CLI argument combination parses without error into a valid `RunParameters`.
- Unit test: Click validates `--lag-days >= 1` and enum choices for `--output`, `--venue`, `--compare-rank-by`.
**Out of scope:** runtime logic (Phases 3–5).

### T-CB-006 — Run Bootstrap phase verification gates (mypy, ruff, pytest)
**Depends on:** T-CB-001, T-CB-002, T-CB-003, T-CB-004, T-CB-005.
**References:** REQ-CB-RUN-001, REQ-CB-RUN-003, REQ-CB-FREEZE-002, design §3.1, §4.1, §4.2.
**Deliverables:**
- `mypy --strict` passes on `razor_rooster/calibration_backtest/` (excluding tests); all signatures annotated with return types.
- `ruff check` and `ruff format` clean across the package; imports ordered.
- Pytest: `tests/unit/test_version.py`, `tests/unit/test_models.py`, `tests/unit/test_freezer.py`, `tests/unit/test_cli_parsing.py` pass at 100%.
- Coverage: `version.py` >90%, `models.py` >85%, `freezer.py` >85%, `cli.py` >75%.
- `BOOTSTRAP_BLOCKERS.md` at repo root documents deferred warnings (signal_scanner integration pending, etc.).
- `/tmp/bootstrap_summary.txt` summarizing files created, requirements addressed, tests passing, lints clean.
**Verification:** all gates green; CI reproduces locally.
**Out of scope:** runtime semantics not yet implemented.

## Phase 2 — Persistence

### T-CB-007 — Scaffold schemas.py with DDL for backtest tables
**Depends on:** Phase 1 (T-CB-001..T-CB-006).
**References:** REQ-CB-PERSIST-001, REQ-CB-PERSIST-002, design §3.3, §3.13.
**Deliverables:**
- `persistence/schemas.py` with Python dataclasses mirroring tables: `BacktestRunSchema`, `BacktestPredictionSchema`, `BacktestTraceSchema`.
- DDL strings per design §3.3: `run_id VARCHAR PRIMARY KEY` for `backtest_runs` with indexes on `(status, started_at)` and `(library_version, system_revision)`; composite PK `(run_id, prediction_id)` for `backtest_traces`.
- Constraint definitions: `status` in `('in_progress', 'complete', 'failed')`; `skip_reason` in the closed enumeration per §3.13; `polarity_source` in `('comparison_resolutions', 'current_mapping_fallback')`; `polarity` in `('direct', 'inverted')`.
- Migration version helpers (`version_6001`, `version_6002`) so migrations can reference schema versions.
- Schema-validation function ensuring all three tables exist with correct column structure at runtime.
**Verification:** unit test confirms DDL parses on DuckDB; schema-validation function detects missing columns.
**Out of scope:** migration execution (T-CB-009).

### T-CB-008 — Implement trace_codec.py with zstd encode/decode round-trip
**Depends on:** Phase 1 (T-CB-001..T-CB-006).
**References:** REQ-CB-PERSIST-002, design §3.11, D4.
**Deliverables:**
- `engines/trace_codec.py` with `encode(trace_dict) -> bytes` calling `json.dumps(..., sort_keys=True, separators=(',', ':'))` then `zstd.compress(level=config.trace_compression_level)`.
- `decode(blob, algorithm) -> dict` branching on algorithm (`'zstd'` for v1) and decompressing.
- `compression_info(blob, algorithm) -> tuple[int, int]` returning `(decompressed_size_bytes, compressed_size_bytes)`.
- Configuration loader reading `config/backtest.yaml` fields `trace_compression`, `trace_compression_level` (default zstd level 3).
**Verification:** unit test pickles a representative scanner Trace object, round-trips through encode/decode, and asserts `decode(encode(t)) == t` under sorted-key normalization.
**Out of scope:** persisting BLOBs (T-CB-010).

### T-CB-009 — Create migrations m6001 and m6002
**Depends on:** T-CB-007.
**References:** REQ-CB-PERSIST-001, REQ-CB-PERSIST-002, design §3.3, OQ-CB-002.
**Deliverables:**
- `persistence/migrations/m6001_calibration_backtest_initial.py` with `upgrade(conn)` and `downgrade(conn)` functions using the DuckDB migration pattern from `data_ingest` (concretely: each migration module declares `VERSION: int` constant, exports `upgrade(conn)` and `downgrade(conn)` taking an open DuckDB connection, runs DDL via `conn.execute(...)`, and the harness records the version in the `schema_versions` registry table; reference implementation is `data_ingest/persistence/migrations/m1001_*.py`). Upgrade creates `backtest_runs`, `backtest_predictions`, `backtest_traces` with all columns and constraints from §3.3; downgrade drops all three.
- `persistence/migrations/m6002_polarity_source_columns.py` (stub for v1; will add `polarity_source` and `mapping_mismatch_warning` to `backtest_predictions` if not already in m6001).
- Migration version numbers in the 6001+ range (clear of data_ingest, polymarket_connector, pattern_library, signal_scanner, mispricing_detector, position_engine).
- `persistence/__init__.py` registry entry so migrations auto-discover and run on schema setup.
**Verification:** unit test applies migrations in order; verifies tables exist with correct schema; rolls back m6002 and verifies columns removed; verifies version-tracking table updated.
**Out of scope:** read/write helpers (T-CB-010).

### T-CB-010 — Implement idempotent insert operations in operations.py
**Depends on:** T-CB-007, T-CB-009.
**References:** REQ-CB-RUN-004, REQ-CB-PERSIST-001, design §3.5, §3.8.
**Deliverables:**
- `persistence/operations.py` with `insert_run(run_id, since_ts, until_ts, lag_days, ...)` upserting `backtest_runs` row with `status='in_progress'`, `started_at=now()`; if `run_id` exists with `status='complete'`, no-op return (idempotent per REQ-CB-RUN-004).
- `insert_prediction(...)` writing into `backtest_predictions` with all fields from §3.3.
- `insert_skip(...)` helper for common skip-path insertion with `status='skipped'`.
- `insert_trace(run_id, prediction_id, trace_json_compressed, compression_algorithm, decompressed_size_bytes)` writing BLOB to `backtest_traces`.
- `complete_run(run_id, summary_json, status, error_summary=None)` updating `completed_at`, `status`, `summary_json`, `error_summary`; preserves `started_at` and counts (append-only per REQ-CB-PERSIST-001).
- `get_run(run_id) -> BacktestRun | None` hydrating from `backtest_runs` plus aggregated counts from `backtest_predictions`.
**Verification:** unit tests for each helper; idempotent re-insert produces no duplicates; `complete_run` second call preserves existing summary.
**Out of scope:** budget guard (T-CB-011).

### T-CB-011 — Implement disk footprint estimation and pre-flight budget check
**Depends on:** T-CB-007, T-CB-010.
**References:** REQ-CB-PERSIST-003, design §3.8.
**Deliverables:**
- `estimate_disk_footprint(params: RunParameters) -> float` in `persistence/operations.py` that: (a) counts predictions in window via SQL `SELECT COUNT` over pre-filtered `iter_mapped_resolutions`, (b) computes `projected_bytes = estimated_predictions * (raw_row_bytes + raw_trace_bytes * compression_ratio) + summary_overhead_mb` using §3.8 constants (`raw_row_bytes=1024`, `raw_trace_bytes=4096`, `compression_ratio=0.25`), (c) returns `projected_mb = projected_bytes / (1024*1024) + 2`.
- Configuration loader reading `config/backtest.yaml` field `disk_cap_mb` (default 100).
- `DiskBudgetError` exception class in `errors.py` with fields `projected_mb`, `disk_cap_mb`, `recommendations`.
- Pre-flight check in replay orchestration entry: if `estimate_disk_footprint(params) > disk_cap_mb`, raise `DiskBudgetError` before any rows inserted.
**Verification:** unit test sets cap to 1 KB; projection that exceeds it raises `DiskBudgetError`; under-cap projection passes; error message includes projection and cap.
**Out of scope:** replay-loop wiring (Phase 3).

### T-CB-012 — Add caching and summary retrieval helpers for fast idempotent replay
**Depends on:** T-CB-007, T-CB-010.
**References:** REQ-CB-RUN-004, REQ-CB-CLI-001, design §3.5.
**Deliverables:**
- `get_run_by_id(run_id) -> BacktestRun | None` querying `backtest_runs` and hydrating fields including parsed `summary_json`.
- `get_run_summary_for_render(run_id) -> dict` returning `summary_json` plus metadata (status, started_at, completed_at, prediction counts, fallback rate, bin counts) without fetching prediction rows.
- `list_runs(limit=50, offset=0, status_filter=None) -> list[BacktestRun]` ordered by `started_at DESC`.
- `count_predictions_by_status(run_id) -> dict[str, int]` returning `{status: count}`.
**Verification:** unit tests confirm `get_run` returns complete object; fast-path metadata matches full hydration; `list_runs` respects limit/offset; status filter works.
**Out of scope:** CLI rendering (Phase 5).

### T-CB-013 — Integration test: persistence layer with idempotent re-run contract
**Depends on:** T-CB-007, T-CB-008, T-CB-009, T-CB-010, T-CB-011, T-CB-012.
**References:** REQ-CB-RUN-004, REQ-CB-PERSIST-001, REQ-CB-PERSIST-002, REQ-CB-PERSIST-003, design §3.5, §3.13.
**Deliverables:**
- `tests/test_persistence_integration.py` exercising the full lifecycle: insert_run with `status='in_progress'`; insert multiple predictions and skips; insert traces; verify counts; complete_run with `summary_json`; verify status `complete`; re-run identical parameters and verify cached summary returned in <1 s without new inserts.
- Append-only test: call `complete_run` twice with different summaries; second call ignored or raises informative error; original summary persists.
- Disk-budget test: mock `estimate_disk_footprint` to exceed cap; verify `DiskBudgetError` raised before `insert_run` fires.
- Trace round-trip: `insert_trace` with zstd-compressed blob; read back via operations layer; verify decompression succeeds and `decompressed_size_bytes` matches.
- Skip-reason enumeration: attempt insert with invalid reason; verify constraint or application validation rejects.
**Verification:** integration test green; rollback-on-failure semantics verified.
**Out of scope:** replay-loop semantics (Phase 3).

## Phase 3 — Replay

### T-CB-014 — Implement freezer engine with source_publication_ts guards
**Depends on:** Phase 1 (T-CB-001..T-CB-006), Phase 2 (T-CB-007..T-CB-013).
**References:** REQ-CB-FREEZE-001, OQ-CB-001, design §3.5.

> **Scout amendment (2026-05-31):** `data_ingest` does NOT expose per-source observation tables. It uses 4 canonical tables (`time_series`, `event_stream`, `document_docket`, `geospatial_indicator`) discriminated by `source_id`. Of the design's named sources (bls_jolts, bls_ces, bea_personal_income, fred, census_retail), only `fred` is registered today; BLS/BEA/Census connectors are deferred. Freezer queries canonical tables.

**Deliverables:**
- `engines/freezer.py` extended with `freezer.freeze(prediction_ts) -> Optional[FrozenState]` entry point.
- `FrozenState` dataclass carrying `(source_publication_ts_boundary, frozen_flag, registered_sources: frozenset[str])`; returns `None` when any precursor source lacks `source_publication_ts`.
- Discover registered source_ids dynamically by querying the `sources` operational table — do not hard-code names. All canonical-schema sources inherit `source_publication_ts` from the provenance prefix, so the column-presence check is implicit for canonical tables.
- WHERE-clauses on canonical tables: `SELECT ... FROM time_series WHERE source_id IN (:registered_sources) AND source_publication_ts <= :prediction_ts AND superseded_at IS NULL` (and analogous queries on `event_stream`, `document_docket`, `geospatial_indicator` for hot-path canonical schemas).
- Add migration `m6003_freezer_indexes` creating `idx_time_series_source_publication_ts ON time_series (source_publication_ts DESC, source_id)` plus analogues on the other canonical tables to keep `freeze()` under the 500ms budget on multi-million-row corpora.
- Document in `freezer.py` module docstring: BLS, BEA, Census coverage is deferred until those connectors land in `data_ingest`; tests use `fred` plus mocked `source_id` rows to exercise the freeze logic.
**Verification:**
- Unit test: synthetic `time_series` rows at varied timestamps; no row with `source_publication_ts > prediction_ts` enters frozen state (boundary equality preserved).
- Unit test: register a synthetic source whose canonical-schema metadata simulates a missing-column scenario → `freeze()` returns `None` with structured log `source_data_not_frozen`.
- Performance test: seed 1M rows across 5 source_ids; assert `freeze()` p95 latency ≤ 500ms with the new indexes in place.
**Out of scope:** orchestration wrapper (T-CB-017).

### T-CB-015 — Implement polarity resolution with comparison_resolutions preference
**Depends on:** Phase 1, Phase 2.
**References:** REQ-CB-REPLAY-003, OQ-CB-002, OQ-CB-005, design §3.5.

> **Scout amendment (2026-05-31):** `comparison_resolutions` lacks `class_id` (must derive via FK on `comparison_id` to the `comparisons` table) and the polarity tier queries must filter `venue` and `removed_at IS NULL` and `resolution_outcome != 'invalid'`. Tier 1 ordering is correctness-critical: ASC (earliest resolution after `prediction_ts`) — not DESC.

**Deliverables:**
- `engines/polarity.py` with `polarity.resolve(conn, prediction_ts, condition_id, class_id, *, venue: str = 'polymarket') -> tuple[str, str]` returning `(polarity_value, source)`.
- **Tier 1 (comparison_resolutions):** `SELECT cr.polarity_at_comparison FROM comparison_resolutions cr JOIN comparisons c USING (comparison_id) WHERE c.condition_id = ? AND c.class_id = ? AND cr.venue = ? AND cr.resolution_ts > ? AND cr.resolution_outcome != 'invalid' ORDER BY cr.resolution_ts ASC LIMIT 1`. On hit return `(polarity_at_comparison, 'comparison_resolutions')`.
- **Tier 2 (current_mapping_fallback):** `SELECT polarity FROM class_market_mappings WHERE class_id = ? AND condition_id = ? AND venue = ? AND removed_at IS NULL`. On hit return `(polarity, 'current_mapping_fallback')`; caller sets `mapping_mismatch_warning=True`.
- **Tier 3:** raise `NoPolarityError(prediction_ts=..., condition_id=..., class_id=...)`; caller catches and inserts skip with `reason='no_polarity_resolution'`.
- `NoPolarityError` defined in `errors.py` (already present from Phase 1; verify signature accepts kwargs).
**Verification:**
- Unit test: Tier 1 hit when `comparison_resolutions` row exists for `(condition_id, class_id, venue)` with `resolution_ts > prediction_ts`.
- Unit test: Tier 2 hit when no `comparison_resolutions` row but `class_market_mappings` row exists with `removed_at IS NULL`.
- Unit test: Tier 3 raises when neither tier resolves.
- **Ordering test (correctness-critical):** seed 3+ resolutions for the same `(condition_id, class_id, venue)` at varied `resolution_ts` values, assert `resolve()` returns the polarity from the **earliest** `resolution_ts > prediction_ts` row.
- Test: `removed_at IS NOT NULL` rows in Tier 2 are excluded.
- Test: `resolution_outcome = 'invalid'` rows in Tier 1 are excluded (no phantom polarities).
- Test: cross-venue collision (Polymarket+Kalshi same `condition_id`) — `venue` filter prevents pollution.
**Out of scope:** orchestration wiring (T-CB-018).

### T-CB-016 — Implement lag enforcement and derive_prediction_ts
**Depends on:** Phase 1.
**References:** REQ-CB-FREEZE-002, design §3.5.
**Deliverables:**
- `engines/freezer.py` adds `derive_prediction_ts(resolution, lag_days) -> datetime` returning `resolution.resolution_ts - timedelta(days=lag_days)`.
- Lag-validation gate: if `(resolution.resolution_ts - prediction_ts).days < lag_days`, skip with `reason='insufficient_lag'`.
- `lag_days` flows from CLI through `RunParameters` into the replay loop per §3.5.
**Verification:**
- Unit test: 3-day lag rejected at default 7-day setting.
- Unit test: 3-day lag accepted with `--lag-days 1`.
**Out of scope:** main replay orchestration (T-CB-018).

### T-CB-017 — Implement evaluate_class_at_frozen_time orchestration wrapper
**Depends on:** Phase 1.
**References:** REQ-CB-REPLAY-002, OQ-CB-001, design §3.5.

> **Scout amendment (2026-05-31):** `signal_scanner.engines.posterior.posterior_with_ci()` is public and reusable unchanged ✓. But `_evaluate_precursors()` is **private** (underscore prefix) and hard-codes its lookback window to `[scan_started_at - 30d, scan_started_at]` with no `as_of_ts` parameter. To preserve D1's "reuse unchanged" principle and avoid drift, expose precursor evaluation as a public wrapper in signal_scanner first.

**Prerequisite sub-task (signal_scanner):**
- Add public function `signal_scanner.engines.posterior.evaluate_precursors_at_time(store, cls, signatures, as_of_ts: datetime, lookback_days: int = 30) -> tuple[dict[str, float | None], bool]` that wraps the existing `_evaluate_precursors` logic but accepts `as_of_ts` in place of `scan_started_at` and adds `WHERE source_publication_ts <= as_of_ts` to all underlying precursor queries. Export from `signal_scanner.engines.posterior.__all__`.
- Contract test: with `as_of_ts == now` and a fixed corpus, `evaluate_precursors_at_time(...)` returns the **same** `current_values` dict as the live scanner path (`_evaluate_precursors` via `scanner.run_scan()`). This locks in non-divergence.

**Deliverables:**
- `engines/replay.py::evaluate_class_at_frozen_time(class_id, prediction_ts, frozen) -> tuple[float, dict]` that:
  1. Calls `signal_scanner.engines.posterior.evaluate_precursors_at_time(store, cls, cls.signatures, as_of_ts=prediction_ts)` to obtain a frozen `current_values` dict.
  2. Calls `signal_scanner.engines.posterior.posterior_with_ci(base_rate=..., signatures=cls.signatures, current_values=current_values, ...)` (unchanged, per OQ-CB-001).
  3. Extracts `model_p = posterior_result.posterior` (the float scalar; the rest of `PosteriorResult` is not needed).
- Returns `(model_p, trace)` where `trace` is the dict representation of scanner's Trace object.
- Raises `InsufficientPrecursorData` if `evaluate_precursors_at_time` yields fewer rows than `cls.min_support`; caller skips with `reason='insufficient_data'`.
- `InsufficientPrecursorData` defined in `errors.py`.
**Verification:**
- Unit test mocks `signal_scanner.posterior` and confirms wrapper passes `prediction_ts` as `as_of_ts` and returns `model_p` plus trace.
- Contract test (anti-divergence): asserts the live-scan and backtest-with-as_of_ts=now paths produce identical `current_values` dicts on a fixed corpus.
**Out of scope:** loop orchestration (T-CB-018).

### T-CB-018 — Implement main replay loop with resolution enumeration
**Depends on:** T-CB-014, T-CB-015, T-CB-016, T-CB-017.
**References:** REQ-CB-REPLAY-001, REQ-CB-REPLAY-004, REQ-CB-RUN-002, REQ-CB-RUN-005, design §3.5, §3.13, §5.1.
**Deliverables:**
- `engines/replay.py::run_backtest(params: RunParameters) -> BacktestRun` orchestrating the full replay per §3.5 pseudocode.
- Window-enforcement guard at replay entry (REQ-CB-RUN-002): before any persistence call, compute `cutoff = now() - timedelta(days=30)`; if `params.until_ts > cutoff` and `params.allow_recent` is `False`, raise `RecentWindowError(until_ts, cutoff, recommended_until_ts=cutoff)`. The error carries a structured message and exits the CLI with non-zero status. When `params.allow_recent` is `True`, the run proceeds and the run row records `allow_recent=True` for auditability.
- `iter_mapped_resolutions(since_ts, until_ts, venues, class_ids)` iterator pre-filtering SQL to resolutions with at least one active mapping for in-scope classes.
- Per-resolution loop: `derive_prediction_ts`, `freeze`, `polarity.resolve`, `evaluate_class_at_frozen_time`, polarity-correct outcome, insert prediction + trace.
- Honor `resolution.invalidated`: skip with `reason='invalid_resolution'` (REQ-CB-REPLAY-004).
- Wrap inner loop in `try/except` catching `NoPolarityError`, `InsufficientPrecursorData`, and a final `Exception` for failure isolation.
- Bounded `ThreadPoolExecutor` (default 4 workers from `config/backtest.yaml`) for embarrassingly-parallel per-prediction work.
**Verification:**
- Integration test seeds 3 resolved markets with 2 mapped classes each and confirms 6 prediction attempts (REQ-CB-REPLAY-001).
- Unit test (REQ-CB-RUN-002, default settings): construct `RunParameters` with `until_ts=now()` and `allow_recent=False`; assert `run_backtest` raises `RecentWindowError` and no row is inserted into `backtest_runs`.
- Unit test (REQ-CB-RUN-002, override): same `until_ts=now()` with `allow_recent=True`; assert the run proceeds past the guard, persists `allow_recent=True` on the row, and completes.
- Unit test (REQ-CB-RUN-002, boundary): `until_ts = now() - 30 days` exactly with `allow_recent=False` is accepted (boundary equality).
**Out of scope:** persistence wiring (T-CB-019).

### T-CB-019 — Wire replay loop to persistence and add trace encoding
**Depends on:** T-CB-018.
**References:** REQ-CB-RUN-005, design §3.5, §3.11.
**Deliverables:**
- `run_backtest()` calls `persistence.insert_run(run_id, ..., status='in_progress')` before the inner loop.
- Each scored/skipped prediction triggers `persistence.insert_prediction(...)`; scored rows additionally call `persistence.insert_trace(...)`.
- `engines/trace_codec.py` extended with `encode(trace_dict) -> bytes` compressing via zstd at `config.trace_compression_level`.
- Persistence stores BLOBs as `trace_json_compressed` with `compression_algorithm='zstd'` and `decompressed_size_bytes` recorded.
- After loop: `persistence.complete_run(run_id, summary_json, status='complete')`.
- Uncaught exceptions: `insert_skip(reason='exception', error_summary=str(exc))`.
**Verification:** integration test verifies scored and skipped rows inserted; trace rows inserted only for scored predictions.
**Out of scope:** Brier aggregation (Phase 4).

### T-CB-020 — Run Replay phase verification gates (mypy, ruff, pytest)
**Depends on:** T-CB-014, T-CB-015, T-CB-016, T-CB-017, T-CB-018, T-CB-019.
**References:** design §4.
**Deliverables:**
- `mypy --strict` clean across `engines/*.py`; no implicit `Any` or unguarded `Optional`.
- `ruff check` and `ruff format` clean across the package.
- Pytest: freezer, polarity, lag, orchestration, main loop, persistence wiring, trace codec — all green.
- Integration: full replay, skip-reason coverage, trace storage, idempotency, determinism hash.
- Document deferred technical debt with `DEFER` comments.
**Verification:** all gates green.
**Out of scope:** scoring (Phase 4).

## Phase 4 — Score aggregation

### T-CB-021 — Implement core Brier score arithmetic in engines/scoring.py
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020).
**References:** REQ-CB-SCORE-001, REQ-CB-SCORE-002, REQ-CB-SCORE-003, design §3.6.
**Deliverables:**
- `engines/scoring.py` with `compute_brier_overall(predictions: list[BacktestPrediction]) -> float` computing `sum((model_p - observed) ** 2) / count`; empty list returns explicit `0.0`.
- `compute_brier_per_sector(run_id, bin_count_global, bin_count_per_sector) -> dict[str, float]` filtering predictions by sector.
- `compute_brier_per_class(run_id, bin_count_global, bin_count_per_sector) -> dict[str, float]` parallel to per-sector grouping by `class_id`.
- Zero-sector and zero-class detection populating `zero_resolutions_sectors` and `zero_resolutions_classes`.
**Verification:**
- Unit test: hand-computed reference of three predictions yields expected Brier within 1e-9.
- Unit test: empty-list edge cases return 0.0 without raising.
**Out of scope:** reliability bins (T-CB-022).

### T-CB-022 — Implement reliability diagram binning via report_generator reuse
**Depends on:** T-CB-021.
**References:** REQ-CB-SCORE-004, design §3.6, D6.
**Deliverables:**
- `compute_reliability_diagrams_per_sector(run_id, bin_count_global, bin_count_per_sector) -> dict[str, ReliabilityDiagram]`.
- For each sector with scored predictions, invoke `report_generator.engines.section_assemblers.reliability.compute_bins()` directly with `(model_p, observed)` pairs grouped by sector. Confirm signature against `REPORT_GENERATOR_DESIGN.md` §3 reliability assembler before integration; if the public signature differs from `compute_bins(pairs, bin_count)`, introduce a thin adapter in `engines/scoring.py` rather than blocking on upstream changes.
- Pass `bin_count` resolved from `bin_count_per_sector[sector]` if present else `bin_count_global`; match report_generator's binning convention exactly (equal-probability bins).
- Return dict mapping sector → serializable `ReliabilityDiagram` with bin edges, counts, calibration metrics.
**Verification:** integration test runs backtest on synthetic corpus overlapping a daily report window; reliability bins match within 1e-9 for all matching sectors.
**Out of scope:** summary assembly (T-CB-023).

### T-CB-023 — Assemble aggregate summary JSON for backtest_runs.summary_json
**Depends on:** T-CB-021, T-CB-022.
**References:** REQ-CB-SCORE-001, REQ-CB-SCORE-002, REQ-CB-SCORE-003, REQ-CB-SCORE-004, design §3.6.
**Deliverables:**
- `aggregate_run_summary(run_id, bin_count_global, bin_count_per_sector) -> ScoreSummary` orchestrating overall, per-sector, per-class Brier and per-sector reliability diagrams.
- `models.py::ScoreSummary` extended with: `overall_brier`, `per_sector_brier`, `per_class_brier`, `zero_resolutions_sectors`, `zero_resolutions_classes`, `reliability_diagrams`, `fallback_polarity_rate`, `fallback_polarity_count`.
- `ScoreSummary.to_json()` using `json.dumps(sort_keys=True)` for determinism.
- `persistence/operations.py::complete_run(run_id, summary: ScoreSummary)` converts summary to JSON and persists to `backtest_runs.summary_json`.
**Verification:** unit tests confirm round-trip through JSON serialization/deserialization; `zero_resolutions_*` populated correctly.
**Out of scope:** compare engine (T-CB-024).

### T-CB-024 — Implement compare engine with cell-level delta computation
**Depends on:** T-CB-023.
**References:** REQ-CB-SCORE-005, design §3.7, D3.
**Deliverables:**
- `engines/compare.py::compare_runs(run_a_id, run_b_id, rank_by) -> list[CompareCell]`.
- Query `backtest_runs` and join `backtest_predictions` for both runs on `(sector, class_id)`; full outer join captures asymmetric cells.
- Per cell: `brier_a`, `brier_b`, `delta_absolute = brier_b - brier_a`, `delta_percent = 100 * (brier_b - brier_a) / brier_a if brier_a > 0 else None`, `present_in in {'both','a_only','b_only'}`.
- `crossed_miscalibration_threshold`: load threshold from config; flag `True` only when both `brier_a` and `brier_b` are non-None and `abs(delta_absolute) >= threshold`.
- `models.py::CompareCell` populated with full field set.
**Verification:** unit tests: self-compare yields zero deltas; asymmetric cells carry `present_in` flag; threshold crossing flag fires correctly.
**Out of scope:** ranking (T-CB-025).

### T-CB-025 — Implement compare ranking and sorting by absolute/percent delta
**Depends on:** T-CB-024.
**References:** REQ-CB-SCORE-005, design §3.7, D3.
**Deliverables:**
- `engines/compare.py::rank_compare_cells(cells, rank_by) -> list[CompareCell]`.
- `rank_by='absolute'`: stable-sort by `abs(delta_absolute)` descending; asymmetric cells (`present_in != 'both'`) at bottom.
- `rank_by='percent'`: stable-sort by `abs(delta_percent)` descending; asymmetric cells at bottom.
- Tie-stability preserved across both modes.
- CLI flag `--compare-rank-by {absolute|percent}` (default `absolute`) routes to this function.
**Verification:** integration test compares two runs (one cell improves, one degrades); sort order respects flag; asymmetric cells appear at bottom.
**Out of scope:** CLI rendering (Phase 5).

### T-CB-026 — Wire bin-count resolution and CLI flags for score aggregation
**Depends on:** T-CB-021, T-CB-022.
**References:** REQ-CB-SCORE-004, design §3.6, D6.
**Deliverables:**
- `engines/scoring.py::resolve_bin_counts(params: RunParameters) -> tuple[int, dict[str, int]]`.
- Resolution order: (1) CLI `--bin-count N` and `--bin-count-per-sector S=N`; (2) `report.yaml thresholds.reliability_bin_count_per_sector[sector]`; (3) `report.yaml thresholds.reliability_bin_count`; (4) module default 10.
- Loads `report.yaml` via `report_generator.config.loader.load_config()`; missing config falls back to module default.
- `bin_count_global` and `bin_count_per_sector_json` stored on `backtest_runs` row for auditability; excluded from `run_id` hash.
- CLI `run` command accepts `--bin-count N` and `--bin-count-per-sector SECTOR=N` (repeatable).
**Verification:** unit tests confirm resolution order; CLI overrides config; missing config falls back gracefully.
**Out of scope:** rendering (Phase 5).

### T-CB-027 — Run Score-aggregation phase verification gates and integration tests
**Depends on:** T-CB-021, T-CB-022, T-CB-023, T-CB-024, T-CB-025, T-CB-026.
**References:** REQ-CB-SCORE-001, REQ-CB-SCORE-002, REQ-CB-SCORE-003, REQ-CB-SCORE-004, REQ-CB-SCORE-005, design §3.6, §3.7, D3, D6.
**Deliverables:**
- `mypy --strict` clean on `engines/scoring.py`, `engines/compare.py`, and `models.py` additions.
- `ruff check` and `ruff format` clean on new code.
- Pytest: `tests/test_scoring.py`, `tests/test_compare.py` green.
- Integration test executing a full backtest end-to-end, calling `aggregate_run_summary`, and verifying JSON serialization matches schema.
- Bit-equality integration test against a synthetic window overlapping a daily report; reliability bins match within 1e-9.
- No regressions in upstream subsystems; no circular dependency introduced.
**Verification:** all gates green.
**Out of scope:** CLI surfaces (Phase 5).

## Phase 5 — CLI

### T-CB-028 — Scaffold CLI module and run command skeleton
**Depends on:** Phase 1 (T-CB-001..T-CB-006), Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-CLI-001, design §3.9.
**Deliverables:**
- `razor_rooster/calibration_backtest/cli.py` with module-scope `@click.group()` and `@run.command()` subcommand.
- CLI decorators wired for `--since`, `--until`, `--lag-days`, `--class-id` (repeatable), `--sector` (repeatable), `--venue` (repeatable), `--bin-count`, `--bin-count-per-sector` (repeatable), `--allow-recent`, `--output` (default `terminal`) per §3.9.
- Imports `api.run_backtest()` (from Phase 3) and wires the command.
- `models.py::RunParameters` finalized with `since_ts`, `until_ts`, `lag_days`, `class_ids: list[str]`, `sectors: list[str]`, `venues: list[str]`, `allow_recent: bool`.
- Run command parses CLI args into `RunParameters` and calls `api.run_backtest(params)`, capturing `BacktestRun`.
- `__main__.py` entry wiring `razor-rooster calibration-backtest` group.
**Verification:** `razor-rooster calibration-backtest run --help` shows all flags; `--output` enum validates against `{'terminal', 'markdown', 'html', 'json'}`.
**Out of scope:** rendering (T-CB-029, T-CB-030).

### T-CB-029 — Implement terminal and markdown output formatters
**Depends on:** T-CB-028.
**References:** REQ-CB-CLI-002, design §3.9, §3.12.
**Deliverables:**
- `razor_rooster/calibration_backtest/renderers.py`.
- `render_terminal(run: BacktestRun) -> str` formatting: run header (run_id, since_ts, until_ts, lag_days, library_version, system_revision, status), disclaimer block from `calibration_backtest.frame.DISCLAIMER`, prediction counts, overall_brier, per-sector and per-class Brier tables, fallback_polarity rate (with note when >5%).
- `render_markdown(run: BacktestRun) -> str` mirroring structure with Markdown tables; reliability diagrams as indented code blocks or SVG embeds.
- `render_html(run: BacktestRun) -> str` generating minimal HTML with embedded CSS; imports `report_generator.engines.section_assemblers.reliability` to produce bit-equal SVG diagrams (REQ-CB-SCORE-004).
- All three renderers frame text in conditional language only ("would", "could", "might", "if the operator chose"); validated via the linter prior to return.
- Each renderer calls `position_engine.frame.linter.check_text(text, config_path='config/forbidden_phrases.yaml')` and raises `FramingError` on rejection.
**Verification:** unit tests for each renderer with synthetic `BacktestRun`; linter integration test confirms rejection of sample forbidden phrases; HTML render produces valid SVG for reliability bins.
**Out of scope:** JSON output (T-CB-030).

### T-CB-030 — Implement JSON output formatter and disclaimer field
**Depends on:** T-CB-028.
**References:** REQ-CB-CLI-003, design §3.9.
**Deliverables:**
- `renderers.py` extended with `render_json(run: BacktestRun) -> str`.
- JSON schema: top-level dict with `run_id`, `since_ts` (ISO8601), `until_ts`, `lag_days`, `library_version`, `system_revision`, `started_at`, `completed_at`, `status`, prediction counts, `overall_brier`, `fallback_polarity_count`, `fallback_polarity_rate`, `bin_count_global`, `bin_count_per_sector`, full `summary_json`, `predictions` (array), `disclaimer` (string constant).
- JSON output bypasses framing linter (consumer is a tool); `disclaimer` field is the canonical disclaimer text.
- `json.dumps(..., indent=2, sort_keys=True)` for determinism.
**Verification:** unit test `json.loads()` validates schema; asserts `disclaimer` field present and correct; round-trip ensures no data loss.
**Out of scope:** dispatch (T-CB-031).

### T-CB-031 — Wire run command output formatting and test default-runnable
**Depends on:** T-CB-028, T-CB-029, T-CB-030.
**References:** REQ-CB-CLI-001, REQ-CB-CLI-002, design §3.9.
**Deliverables:**
- `cli.py` run command dispatches to the appropriate renderer based on `--output`.
- Run command calls `renderers.render_*` with the `BacktestRun` from `api.run_backtest()`; prints to stdout for terminal/markdown/html or writes JSON to stdout.
- Bare-run defaults: when none of `--since`, `--until`, `--class-id`, `--sector`, `--venue` are provided, defaults to `since_ts = earliest in polymarket_resolutions`, `until_ts = now - 30 days`, `lag_days = 7`, `class_ids = pattern_library.list_classes()`, `sectors = []`, `venues = ['polymarket']`.
- Integration test running `razor-rooster calibration-backtest run` with zero arguments on a populated test corpus: command exits 0; stdout contains summary; text passes linter.
- Unit tests for each output format against canned fixtures.
**Verification:** integration test confirms bare-command works; output rendered; JSON valid with disclaimer.
**Out of scope:** other subcommands (T-CB-032).

### T-CB-032 — Implement list, show, compare, and prune CLI commands
**Depends on:** T-CB-028, T-CB-029.
**References:** design §3.9.
**Deliverables:**
- `list` command parses `--since ISO`, `--limit N` (default 50); queries `backtest_runs` ordered by `started_at DESC`; renders table with run_id (12-char), started_at, library_version, truncated system_revision, lag_days, prediction counts, overall_brier, fallback_polarity_rate, status; passes through linter.
- `show` command accepts positional `RUN_ID` and optional `--output FORMAT`; raises `RunNotFoundError` when missing; renders via appropriate formatter.
- `compare` command accepts positional `RUN_A`, `RUN_B`, optional `--compare-rank-by {absolute|percent}` (default `absolute`), optional `--top N`; calls `engines.compare.compare_runs()` and renders ranked table with sector, class_id, brier_a, brier_b, delta_absolute, delta_percent, crossed_miscalibration_threshold, present_in; passes through linter.
- `prune` command requires `--before ISO` and `--confirm`; cascade-deletes from `backtest_runs`, `backtest_predictions`, `backtest_traces`; emits row-deletion summary.
- All non-JSON outputs pass through the linter; conditional language enforced.
**Verification:** unit tests for each command with synthetic fixtures; integration test against seeded database; `prune` refuses without `--confirm`.
**Out of scope:** GUI (Phase 6).

### T-CB-033 — Integrate framing linter, build disclaimer constant, and audit CLI compliance
**Depends on:** T-CB-029, T-CB-030, T-CB-032.
**References:** REQ-CB-CLI-002, REQ-CB-CLI-003, design §3.10, §3.12.
**Deliverables:**
- `razor_rooster/calibration_backtest/frame.py` with module constant `DISCLAIMER` (exact text from §3.12) and `FOOTER_NOTE` (footer text from §3.12).
- Helper `check_cli_framing(text: str) -> None` wrapping `position_engine.frame.linter.check_text` and raising `FramingError` on rejection.
- Renderers include disclaimer block at top of terminal/markdown/html renders.
- Terminal/markdown append footer note at end; HTML places both in semantic sections with CSS classes.
- `tests/test_cli_framing.py::test_all_renders_pass_linter` runs all five CLI commands against seeded database, captures outputs, asserts each passes `check_text()`, confirms disclaimer block present, confirms JSON includes disclaimer field.
**Verification:** linter integration test passes; all command outputs carry expected disclaimer; no forbidden phrases in fixtures.
**Out of scope:** GUI framing (Phase 6).

### T-CB-034 — Run CLI phase verification gates (mypy, ruff, pytest)
**Depends on:** T-CB-028, T-CB-029, T-CB-030, T-CB-031, T-CB-032, T-CB-033.
**References:** REQ-CB-CLI-001, REQ-CB-CLI-002, REQ-CB-CLI-003.
**Deliverables:**
- `mypy --strict razor_rooster/calibration_backtest/cli.py razor_rooster/calibration_backtest/renderers.py razor_rooster/calibration_backtest/frame.py` clean.
- `ruff check --select=E,W,F,I` clean across the package; imports sorted.
- Pytest selectors `-k 'test_cli or test_render or test_framing'` green.
- `tests/test_cli_framing.py` green.
- `razor-rooster calibration-backtest --help` and `razor-rooster calibration-backtest run --help` execute without errors.
**Verification:** all gates green; no type errors; no lint violations; pytest pass rate 100%.
**Out of scope:** GUI (Phase 6).

## Phase 6 — GUI

> The GUI phase implements two read-only routes for listing and viewing calibration backtest runs. All renders pass through the framing linter (REQ-CB-CLI-002), include standard disclaimer blocks (REQ-CB-CLI-003), and reuse operator auth from `report_generator`. Tasks address REQ-CB-CLI-004 (GUI surface).

### T-CB-035 — Scaffold GUI module and routes foundation
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-CLI-004, design §3.14, §3.15.
**Deliverables:**
- `razor_rooster/calibration_backtest/gui/__init__.py` with empty module and entry-point registration.
- `gui/routes.py` Flask blueprint `calibration_backtest_bp` registering `/calibration-backtest` and `/calibration-backtest/<run_id>`.
- `gui/auth.py` importing operator-auth decorators from `report_generator.gui.auth` (no changes to `report_generator`; reuse existing).
- `gui/templates/` directory with base template inheriting from `report_generator`'s frame template.
- `tests/gui/test_routes.py` scaffold with fixtures for seeded `backtest_runs` and `backtest_predictions`.
- Import sanity: `position_engine.frame.linter`, `report_generator.engines.section_assemblers.reliability`, and operator-auth all accessible without circular dependency.
**Verification:** module imports cleanly; blueprint registers; tests collect without error.
**Out of scope:** route handlers (T-CB-036, T-CB-037).

### T-CB-036 — Implement list view (`/calibration-backtest` route)
**Depends on:** T-CB-035.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.12.
**Deliverables:**
- `GET /calibration-backtest` handler in `routes.py` querying `backtest_runs` ordered by `started_at DESC`, default `LIMIT 50`.
- Columns: run_id (12-char prefix linked to detail), started_at, library_version, system_revision (16-char prefix), lag_days, predictions_total, predictions_scored, overall_brier, fallback_polarity_rate (computed; `NULL` when zero scored).
- Status badge (in_progress | complete | failed) with conditional styling.
- Jinja2 template `calibration_backtest/list.html` with table rows; disclaimer block from `calibration_backtest.frame.DISCLAIMER` at page top.
- `position_engine.frame.linter.check_text` applied to all non-data strings (headings, labels, instructions) before rendering.
- Pagination handler for `--limit N` query param; default 50, max 200.
**Verification:** route returns 200; table rows present; linter passes on all chrome strings.
**Out of scope:** detail view (T-CB-037).

### T-CB-037 — Implement detail view (`/calibration-backtest/{run_id}` route)
**Depends on:** T-CB-035, T-CB-036.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.6, §3.12, OQ-CB-002.
**Deliverables:**
- `GET /calibration-backtest/{run_id}` handler fetching `backtest_runs` by `run_id` (404 if missing).
- Deserializes `summary_json` for per-sector Brier, per-class Brier, fallback rate, resolved bin counts (`bin_count_global`, `bin_count_per_sector_json`).
- Queries `backtest_predictions` paginated by status (scored | skipped) with optional `skip_reason` filter.
- Header section: run parameters, library_version, system_revision, run status, prediction counts (total, scored, skipped by reason).
- Per-sector and per-class Brier tables.
- Reliability diagrams generated by invoking `report_generator.engines.section_assemblers.reliability(..., bin_count=...)`; embedded as inline SVG.
- Fallback-polarity-rate banner: when rate >5%, render highlight note (linter-checked; conditional language).
**Verification:** detail route renders for seeded run; banner appears at >5% rate; reliability SVGs match daily-report binning.
**Out of scope:** predictions table (T-CB-038).

### T-CB-038 — Implement predictions table with pagination and filtering
**Depends on:** T-CB-035, T-CB-037.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.7, §3.14.
**Deliverables:**
- Detail-view predictions section rendering `backtest_predictions` rows in a paged table.
- Columns: prediction_id (truncated), class_id, condition_id, venue, sector, prediction_ts, resolution_ts, model_p, observed, polarity, polarity_source, status, skip_reason (when skipped).
- Filter tabs/dropdown: 'All', 'Scored', 'Skipped', `Skip reason: {reason}` per unique reason.
- Pagination: 20 rows per page; prev/next links.
- Optional `trace_diff_summary` column lazy-loaded via separate AJAX endpoint (deferred to v2 if time-boxed).
- Linter applied to column headers and explanatory text; data cells bypass linter.
**Verification:** filter tabs return correct subsets; pagination respects page size; linter passes on chrome.
**Out of scope:** trace decompression (deferred).

### T-CB-039 — Integrate framing linter and disclaimer rendering
**Depends on:** T-CB-035.
**References:** REQ-CB-CLI-002, REQ-CB-CLI-004, design §3.9, §3.12.
**Deliverables:**
- `gui/frame.py` exports `DISCLAIMER` constant (re-exported from `calibration_backtest.frame.DISCLAIMER` to avoid duplication).
- `render_disclaimer()` helper returning HTML fragment for page top.
- `render_footer_note()` helper returning footer fragment (HTML/template routes only; not JSON).
- All template strings (headings, labels, instructions, badge text) wrapped with `linter.check_text()` before rendering.
- Error handler: missing `forbidden_phrases.yaml` raises `FramingLinterError` (do NOT skip the check); operator log entry recorded.
- `gui/tests/test_framing.py` confirms linter is invoked on all list and detail view strings.
**Verification:** unit test asserts linter invoked on all chrome; missing config raises; disclaimer renders at page top on both routes.
**Out of scope:** route logic (T-CB-036..T-CB-038).

### T-CB-040 — Add comprehensive GUI route tests with seeded data
**Depends on:** T-CB-035, T-CB-036, T-CB-037, T-CB-038, T-CB-039.
**References:** REQ-CB-CLI-004, design §3.14, §4.2.
**Deliverables:**
- `tests/gui/test_routes.py` integration fixtures seeding `backtest_runs`, `backtest_predictions`, `backtest_traces` into a test DuckDB instance.
- List-route test: 200; table rows; run_id link format; linter passes.
- Detail-route test: 200; metadata; per-sector Brier; reliability SVGs; predictions paginated.
- Missing-run test: 404 on unknown run_id.
- Fallback-polarity-banner test: seeded run with >5% fallback rate; banner present and linter-passed.
- Pagination test: 50+ predictions; page 1 = 20 rows; page 2 = remainder.
- Filter test: `?status=skipped` and `?skip_reason=insufficient_lag` return only matching rows.
- Auth test: route without operator session yields 401 or redirect (mirroring `report_generator` patterns).
**Verification:** all GUI tests green.
**Out of scope:** CLI tests (Phase 5).

### T-CB-041 — GUI verification gates (mypy, ruff, pytest, no-circular)
**Depends on:** T-CB-035, T-CB-036, T-CB-037, T-CB-038, T-CB-039, T-CB-040.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.15.
**Deliverables:**
- `mypy --strict razor_rooster/calibration_backtest/gui/` clean.
- `ruff check razor_rooster/calibration_backtest/gui/ --select E,W,F,I` clean.
- `ruff format razor_rooster/calibration_backtest/gui/` applied.
- `pytest razor_rooster/calibration_backtest/tests/gui/ -v --tb=short` green, including framing and auth tests.
- Static check: `grep -r "from calibration_backtest" razor_rooster/report_generator razor_rooster/gui` returns zero matches (no circular dependency).
- Document deferred GUI enhancements (DEFER-CB-005 items: JS interactivity, trace diffs) in CALIBRATION_BACKTEST_DESIGN.md §7.
**Verification:** all gates green.
**Out of scope:** non-GUI surfaces.

## Phase 7 — Pattern-Library upgrade

### T-CB-042 — Implement polymarket_resolution_calibration._occurrences SQL query
**Depends on:** Phase 1 (T-CB-001..T-CB-006).
**References:** REQ-CB-PL-001, design §3.16, OQ-CB-002, OQ-CB-005.

> **Scout amendment (2026-05-31):** `comparison_resolutions` does NOT have a `class_id` column — it must be derived by joining through the `comparisons` table on `comparison_id`. The query is therefore a **three-table** join, not two. Note: until `mispricing_detector` linkage matures, some predictions may not flow into `comparison_resolutions`; treat coverage as expected partial, not a defect.

**Deliverables:**
- `pattern_library/classes/polymarket_resolution_calibration.py::_occurrences` replaces the empty-frame stub with a real DuckDB query.
- **Three-table join:** `comparison_resolutions cr JOIN comparisons c USING (comparison_id) JOIN polymarket_resolutions pr USING (condition_id)`.
- Filters: `pr.resolution_ts BETWEEN :since_ts AND :until_ts`, `pr.invalidated = FALSE`, `c.class_id = :class_id`, `pr.superseded_at IS NULL`.
- Selects: `c.condition_id`, `c.class_id`, `cr.polarity_at_comparison`, `pr.winning_outcome_label`, `pr.resolution_ts AS occurrence_ts`, `pr.invalidated`.
- Computes `(model_p, observed)` pair using `polarity_at_comparison` per design §3.16.
- Returns DataFrame with the documented column set.
- Empty-frame fallback removed.
- Module docstring documents linkage-coverage caveat (some comparisons may lack `comparison_resolutions` rows until linkage pass matures — partial coverage is expected, not a defect).
**Verification:** seeded fixture produces non-empty DataFrame; column set matches spec; `class_id` correctly derived from `comparisons` table.
**Out of scope:** test fixture (T-CB-043).

### T-CB-043 — Add unit test for polymarket_resolution_calibration._occurrences upgrade
**Depends on:** T-CB-042.
**References:** REQ-CB-PL-001, design §4.2.
**Deliverables:**
- `pattern_library/tests/` fixture seeding `comparison_resolutions` and `polymarket_resolutions` rows.
- Seed `comparison_resolutions` with `condition_id`, `class_id`, `polarity_at_comparison='direct'`, `resolution_outcome='yes'`.
- Seed matching `polymarket_resolutions` row with `invalidated=FALSE`, `winning_outcome_label='yes'`.
- Invoke `polymarket_resolution_calibration._occurrences(_conn)` against the fixture.
- Assert returned DataFrame contains exactly one row with correct fields.
- Assert empty-frame fallback no longer reachable (no conditional returning empty DataFrame).
- Add test for `invalidated=TRUE` confirming filtering.
**Verification:** test green.
**Out of scope:** circular-dependency check (T-CB-044).

### T-CB-044 — Validate no circular dependency in pattern_library meta-class
**Depends on:** T-CB-042.
**References:** REQ-CB-PL-002, design §3.15, §3.2.
**Deliverables:**
- Static import check: `grep -r 'from razor_rooster.calibration_backtest' pattern_library/` returns zero matches.
- AST-level check: no `import calibration_backtest` statements in `pattern_library/`.
- `_occurrences` does not invoke any `calibration_backtest.*` symbols.
- DuckDB connection (`_conn`) is the sole external dependency passed to `_occurrences`.
- Inline comments document REQ-CB-PL-002 compliance and the §3.2 reuse pattern.
- Test asserts no back-edge exists (greps for both `from calibration_backtest` and `import calibration_backtest`).
**Verification:** static and AST checks pass; no back-edge introduced.
**Out of scope:** other meta-classes.

### T-CB-045 — Verify polarity-correction semantics in meta-class output
**Depends on:** T-CB-043.
**References:** REQ-CB-PL-001, REQ-CB-REPLAY-003, design §3.16, OQ-CB-005.
**Deliverables:**
- Test cases covering `polarity_at_comparison in {'direct','inverted'}` paired with `winning_outcome_label in {'yes','no'}`.
- For `polarity='direct'`: `'yes'` → `observed=1.0`, `'no'` → `observed=0.0`.
- For `polarity='inverted'`: `'yes'` → `observed=0.0`, `'no'` → `observed=1.0`.
- Asserts the DataFrame returned by `_occurrences` honors this convention.
- `(model_p, observed)` pairs computable from the returned row per §3.16.
- Compares against hand-computed reference data.
**Verification:** all four polarity × outcome combinations match expected `observed` values.
**Out of scope:** integration testing (T-CB-046).

### T-CB-046 — Integration test: meta-class occurrence count matches comparison_resolutions join
**Depends on:** T-CB-043.
**References:** REQ-CB-PL-001, design §4.2.
**Deliverables:**
- Seed `comparison_resolutions` with N rows spanning a known `resolution_ts` range and specific `class_id`.
- Seed `polymarket_resolutions` with matching `invalidated=FALSE` rows.
- Invoke `_occurrences` with `since_ts/until_ts` matching the seed; assert row count == N.
- Re-invoke with narrowed `until_ts` excluding half the data; assert row count is `floor(N/2) ± 1`.
- Mix in a single `invalidated=TRUE` row; assert it is filtered out.
**Verification:** all assertions pass.
**Out of scope:** lint gates (T-CB-047).

### T-CB-047 — Lint and type-check pattern_library upgrade
**Depends on:** T-CB-042, T-CB-043, T-CB-044, T-CB-045, T-CB-046.
**References:** REQ-CB-PL-001.
**Deliverables:**
- `mypy --strict pattern_library/classes/polymarket_resolution_calibration.py` clean.
- `ruff check` and `ruff format` clean on the upgraded file.
- No new import statements introduce circular dependencies (mypy import-graph check if available).
- Function signatures conform to the `EventClass` `occurrence_query` protocol.
- `pytest pattern_library/tests/` green; no regressions in other classes.
**Verification:** all gates green.
**Out of scope:** acceptance regression suite (T-CB-048).

### T-CB-048 — Run pattern_library test suite; verify no regressions
**Depends on:** T-CB-047.
**References:** REQ-CB-PL-001, REQ-CB-PL-002, design §4.2.
**Deliverables:**
- Execute `pytest pattern_library/tests/` (unit + integration).
- All existing `pattern_library` tests continue to pass (no broken imports, no schema mismatches).
- Static import-graph test (§4.2 "No circular dependency") passes.
- Meta-class is callable within the `pattern_library` refresh loop without errors.
- Coverage report exercises new `_occurrences` paths.
- CHANGELOG entry records REQ-CB-PL-001 and REQ-CB-PL-002 gate completion.
**Verification:** suite green; coverage non-regressed.
**Out of scope:** acceptance gate (Phase 8).

## Phase 8 — Acceptance

### T-CB-049 — Implement P-CB-001..003 property tests (determinism, time honesty, polarity coherence)
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-RUN-001, REQ-CB-RUN-003, REQ-CB-FREEZE-001, REQ-CB-REPLAY-003, design §3.17, §4.1.
**Deliverables:**
- `tests/test_properties.py::P-CB-001` (hypothesis): generated `(params, library_version, system_revision)` tuples; `compute_run_id` produces stable hash across permutations; idempotent re-run yields bit-equal `summary_json`.
- `P-CB-002` (hypothesis): synthetic source rows at varied timestamps; no precursor with `source_publication_ts > prediction_ts` reaches posterior; `freezer.freeze(prediction_ts)` rejects future data.
- `P-CB-003`: four `(model_p ∈ {0.3, 0.7}) × (polarity ∈ {'direct','inverted'})` combinations; every scored prediction carries non-null `polarity_source`; `observed` is polarity-corrected `polymarket_resolutions.outcome`.
- Uses `@hypothesis.given` for property-based generation; fixtures seeded at precise timestamps for reproducibility.
**Verification:** all three properties green across hypothesis runs.
**Out of scope:** bin alignment / skip transparency (T-CB-050).

### T-CB-050 — Implement P-CB-004..005 property tests (bin alignment, skip transparency)
**Depends on:** T-CB-049.
**References:** REQ-CB-SCORE-004, design §3.17, §3.13, §4.1.
**Deliverables:**
- `P-CB-004`: integration test running `backtest_runs` over a known window; calls `report_generator.engines.section_assemblers.reliability` on the same predictions; per-sector bins bit-equal within `numpy.isclose(atol=1e-9)`.
- `P-CB-005`: iterates all `backtest_predictions` rows with `status='skipped'`; every `skip_reason` belongs to the closed enumeration (`insufficient_lag`, `invalid_resolution`, `source_data_not_frozen`, `no_polarity_resolution`, `insufficient_data`, `exception`); raises on unknown reason.
- Test corpus seeded over a 90-day window exercising every skip reason at least once.
**Verification:** properties green; skip enumeration coverage validated.
**Out of scope:** persistence / framing (T-CB-051).

### T-CB-051 — Implement P-CB-006..007 property tests (append-only persistence, framing linter)
**Depends on:** T-CB-050.
**References:** REQ-CB-CLI-002, REQ-CB-CLI-003, REQ-CB-PERSIST-001, design §3.17, §3.12, §3.9.
**Deliverables:**
- `P-CB-006`: create `backtest_runs` row with `status='complete'`; attempt UPDATE; confirm rejection or no-op per application logic; assert only INSERT for new runs (REQ-CB-PERSIST-001).
- `P-CB-007`: iterate all CLI output paths (terminal, markdown, html); pass each through `position_engine.frame.linter.check_text`; assert every string passes; confirm JSON output includes `disclaimer` field.
- Forbidden-phrase list verified: `place an order`, `execute the trade`, `you should buy`, `you should sell`, `guaranteed profit`, `will profit` — linter rejects each.
**Verification:** properties green.
**Out of scope:** performance gates (T-CB-052).

### T-CB-052 — Implement performance gates (REQ-CB-PERF-001, REQ-CB-PERF-002)
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-PERF-001, REQ-CB-PERF-002, design §4.3, §6.
**Deliverables:**
- `tests/test_performance.py` synthetic corpus with 4000 prediction attempts (sized to v1 seed library upper bound: 8 classes, ~500 resolutions across 5 years).
- REQ-CB-PERF-001 smoke test: measures wall-clock for `run_backtest(params)`; asserts <5 minutes; emits `pytest.warning` (not fail) if exceeded so slow CI runners don't block.
- REQ-CB-PERF-002 smoke test: instruments `run_backtest` with `resource.getrusage`; captures peak resident memory; asserts <2 GB; emits `pytest.warning` at threshold.
- Reference hardware (EliteBook G8: i7-8665U, 16 GB DDR4, NVMe SSD) documented in test docstring.
- Marker `@pytest.mark.perf` so test can be skipped on CI when needed.
**Verification:** smoke tests run; warnings or assertion-failures recorded.
**Out of scope:** golden-data audit (T-CB-053).

### T-CB-053 — Implement end-to-end smoke test and golden-data calibration audit
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-RUN-004, REQ-CB-SCORE-004, design §4.2, §4.3.
**Deliverables:**
- `tests/test_e2e.py` end-to-end smoke seeding all required tables (`polymarket_resolutions`, `comparison_resolutions`, `class_market_mappings`, data_ingest precursors); invokes `run_backtest` with default parameters; asserts `status='complete'` and non-null `summary_json`.
- Verifies precursor freezing, polarity resolution, Brier aggregation (non-NaN `overall_brier`), reliability bins.
- Idempotent re-run: same parameters twice; second returns cached result in <1 s; row counts stable.
- Golden-data audit on a 90-day real test corpus; per-sector Brier and reliability diagrams compared to manually computed reference values within tolerance.
**Verification:** e2e green; golden-data tolerances met.
**Out of scope:** dependency audit (T-CB-054).

### T-CB-054 — Implement no-side-channels audit and no-circular-dependency static check
**Depends on:** T-CB-048 (Phase 7 complete; pattern_library upgrade landed and meta-class verified before back-edge audit runs).
**References:** REQ-CB-PL-002, design §3.15, §3.2.
**Deliverables:**
- `tests/test_dependencies.py` static AST check: `grep -r 'from calibration_backtest'` across `razor_rooster/{pattern_library,signal_scanner,mispricing_detector,report_generator,position_engine,data_ingest,polymarket_connector}` returns zero matches; same for `import calibration_backtest`.
- Verifies `pattern_library.classes.meta.polymarket_resolution_calibration` queries DuckDB directly (no `calibration_backtest.*` calls).
- Side-channel audit: backtest produces no network egress, no writes outside `backtest_runs/backtest_predictions/backtest_traces`, no upstream-state mutation.
**Verification:** all checks pass.
**Out of scope:** evolution-log update (T-CB-055).

### T-CB-055 — Run full test suite and update evolution log; verify mypy/ruff/pytest gates
**Depends on:** T-CB-049, T-CB-050, T-CB-051, T-CB-052, T-CB-053, T-CB-054.
**References:** REQ-CB-PERF-001, REQ-CB-PERF-002, design §4, §7.
**Deliverables:**
- `pytest tests/ --strict` with all markers enabled (unit, integration, e2e, properties, performance).
- `mypy --strict razor_rooster/calibration_backtest/` zero errors.
- `ruff check` and `ruff format` clean.
- All seven property tests (P-CB-001..007), all REQ-CB-* acceptance criteria, and performance gates pass.
- `razorrooster.md` evolution-log entry updated for this subsystem: v0.1.0 SHIPPED, acceptance-test pass date, reference-hardware performance measurements, deferred tasks (DEFER-CB-*) for v2.
- Test coverage summary recorded in README or TESTS.md.
**Verification:** subsystem PRODUCTION_READY.
**Out of scope:** v2 follow-on work.

## Dependency Summary (Critical Path)

    T-CB-001 → T-CB-002 → T-CB-003 → T-CB-004 → T-CB-005 → T-CB-006
                                                              ↓
    T-CB-007 → T-CB-008 → T-CB-009 → T-CB-010 → T-CB-011 → T-CB-012 → T-CB-013
                                                                         ↓
    T-CB-014 → T-CB-015 → T-CB-016 → T-CB-017 → T-CB-018 → T-CB-019 → T-CB-020
                                                                         ↓
    T-CB-021 → T-CB-022 → T-CB-023 → T-CB-024 → T-CB-025 → T-CB-026 → T-CB-027
                                                                         ↓
    T-CB-028 → T-CB-029 → T-CB-030 → T-CB-031 → T-CB-032 → T-CB-033 → T-CB-034
                                                                         ↓
                            [T-CB-035..T-CB-041 (GUI)         in parallel with
                             T-CB-042..T-CB-048 (Pattern-Library upgrade)]
                                                                         ↓
                                  T-CB-049 → T-CB-050 → T-CB-051
                                  T-CB-052 (parallel with T-CB-049..T-CB-051)
                                  T-CB-053 (parallel with T-CB-049..T-CB-051)
                                  T-CB-054 (parallel)
                                                                         ↓
                                              T-CB-055 (acceptance gate)

Phases 6 (GUI) and 7 (Pattern-Library upgrade) parallelize after the CLI phase. Phase 8 (acceptance) gates production readiness; T-CB-055 is the final acceptance task.

## Tracking

- **T-CB-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` | `OPERATOR_BLOCKED` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.53.0):

- **T-CB-001** — Scaffold package structure and initialization — `OPEN`
- **T-CB-002** — Implement version and run-id computation logic — `OPEN`
- **T-CB-003** — Define core data models and dataclasses — `OPEN`
- **T-CB-004** — Implement freezer and lag-validation logic — `OPEN`
- **T-CB-005** — Wire skeleton CLI entry points and argument parsing — `OPEN`
- **T-CB-006** — Run Bootstrap phase verification gates (mypy, ruff, pytest) — `OPEN`
- **T-CB-007** — Scaffold schemas.py with DDL for backtest tables — `OPEN`
- **T-CB-008** — Implement trace_codec.py with zstd encode/decode round-trip — `OPEN`
- **T-CB-009** — Create migrations m6001 and m6002 — `OPEN`
- **T-CB-010** — Implement idempotent insert operations in operations.py — `OPEN`
- **T-CB-011** — Implement disk footprint estimation and pre-flight budget check — `OPEN`
- **T-CB-012** — Add caching and summary retrieval helpers for fast idempotent replay — `OPEN`
- **T-CB-013** — Integration test: persistence layer with idempotent re-run contract — `OPEN`
- **T-CB-014** — Implement freezer engine with source_publication_ts guards — `OPEN`
- **T-CB-015** — Implement polarity resolution with comparison_resolutions preference — `OPEN`
- **T-CB-016** — Implement lag enforcement and derive_prediction_ts — `OPEN`
- **T-CB-017** — Implement evaluate_class_at_frozen_time orchestration wrapper — `OPEN`
- **T-CB-018** — Implement main replay loop with resolution enumeration — `OPEN`
- **T-CB-019** — Wire replay loop to persistence and add trace encoding — `OPEN`
- **T-CB-020** — Run Replay phase verification gates (mypy, ruff, pytest) — `OPEN`
- **T-CB-021** — Implement core Brier score arithmetic in engines/scoring.py — `OPEN`
- **T-CB-022** — Implement reliability diagram binning via report_generator reuse — `OPEN`
- **T-CB-023** — Assemble aggregate summary JSON for backtest_runs.summary_json — `OPEN`
- **T-CB-024** — Implement compare engine with cell-level delta computation — `OPEN`
- **T-CB-025** — Implement compare ranking and sorting by absolute/percent delta — `OPEN`
- **T-CB-026** — Wire bin-count resolution and CLI flags for score aggregation — `OPEN`
- **T-CB-027** — Run Score-aggregation phase verification gates and integration tests — `OPEN`
- **T-CB-028** — Scaffold CLI module and run command skeleton — `OPEN`
- **T-CB-029** — Implement terminal and markdown output formatters — `OPEN`
- **T-CB-030** — Implement JSON output formatter and disclaimer field — `OPEN`
- **T-CB-031** — Wire run command output formatting and test default-runnable — `OPEN`
- **T-CB-032** — Implement list, show, compare, and prune CLI commands — `OPEN`
- **T-CB-033** — Integrate framing linter, build disclaimer constant, and audit CLI compliance — `OPEN`
- **T-CB-034** — Run CLI phase verification gates (mypy, ruff, pytest) — `OPEN`
- **T-CB-035** — Scaffold GUI module and routes foundation — `OPEN`
- **T-CB-036** — Implement list view route — `OPEN`
- **T-CB-037** — Implement detail view route — `OPEN`
- **T-CB-038** — Implement predictions table with pagination and filtering — `OPEN`
- **T-CB-039** — Integrate framing linter and disclaimer rendering — `OPEN`
- **T-CB-040** — Add comprehensive GUI route tests with seeded data — `OPEN`
- **T-CB-041** — GUI verification gates (mypy, ruff, pytest, no-circular) — `OPEN`
- **T-CB-042** — Implement polymarket_resolution_calibration._occurrences SQL query — `OPEN`
- **T-CB-043** — Add unit test for polymarket_resolution_calibration._occurrences upgrade — `OPEN`
- **T-CB-044** — Validate no circular dependency in pattern_library meta-class — `OPEN`
- **T-CB-045** — Verify polarity-correction semantics in meta-class output — `OPEN`
- **T-CB-046** — Integration test: meta-class occurrence count matches comparison_resolutions join — `OPEN`
- **T-CB-047** — Lint and type-check pattern_library upgrade — `OPEN`
- **T-CB-048** — Run pattern_library test suite; verify no regressions — `OPEN`
- **T-CB-049** — Implement P-CB-001..003 property tests (determinism, time honesty, polarity coherence) — `OPEN`
- **T-CB-050** — Implement P-CB-004..005 property tests (bin alignment, skip transparency) — `OPEN`
- **T-CB-051** — Implement P-CB-006..007 property tests (append-only persistence, framing linter) — `OPEN`
- **T-CB-052** — Implement performance gates (REQ-CB-PERF-001, REQ-CB-PERF-002) — `OPEN`
- **T-CB-053** — Implement end-to-end smoke test and golden-data calibration audit — `OPEN`
- **T-CB-054** — Implement no-side-channels audit and no-circular-dependency static check — `OPEN`
- **T-CB-055** — Run full test suite and update evolution log; verify mypy/ruff/pytest gates — `OPEN`

All tasks PROPOSED for v0.1.0. Lifecycle: SPECIFYING → IMPLEMENTING upon Phase 1 commencement. T-CB-053 (golden-data audit) and T-CB-055 (evolution-log update) gate PRODUCTION_READY.

## References

- Requirements: `CALIBRATION_BACKTEST.md` v0.1.0 — REQ-CB-RUN-001..005, REQ-CB-FREEZE-001..003, REQ-CB-REPLAY-001..004, REQ-CB-SCORE-001..005, REQ-CB-PERSIST-001..003, REQ-CB-CLI-001..004, REQ-CB-PL-001..002, REQ-CB-PERF-001..002.
- Design: `CALIBRATION_BACKTEST_DESIGN.md` v0.1.0 — §3.1 Module Layout, §3.2 Reuse, §3.3 Tables, §3.4 Run Identification, §3.5 Replay Loop, §3.6 Score Aggregation, §3.7 Compare Engine, §3.8 Configuration, §3.9 CLI, §3.10 Threat Model, §3.11 Trace Serialization, §3.12 Disclaimer, §3.13 Skip Reason Enumeration, §3.14 GUI Surface, §3.15 No Circular Dependency, §3.16 Meta-Class Query, §3.17 Properties; §4 Test Strategy; §6 Performance Notes; §7 Deferred.
- LOOM: `razorrooster.md` v0.53.0.
- Companion: `data_ingest`, `polymarket_connector`, `pattern_library`, `signal_scanner`, `report_generator`, `position_engine` v0.1.0+ specs.
