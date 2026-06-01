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

### T-CB-022 — Implement reliability diagram binning natively (mirroring report_generator's equal-width convention)
**Depends on:** T-CB-021.
**References:** REQ-CB-SCORE-004, design §3.6, D6.

> **Scout amendment (2026-05-31):** report_generator does NOT expose a public `compute_bins(pairs, bin_count)` function. Its only public symbol is `assemble(conn, *, since_ts, until_ts, ...)` which is DuckDB-driven and returns a renderer content-dict — not a `ReliabilityDiagram`. The reusable arithmetic (`_equal_width_bins`, `_compute_bin_summaries`) is private. Furthermore, the previous text "equal-probability bins" was factually wrong: report_generator uses **equal-width bins**. calibration_backtest must implement binning natively, mirroring the equal-width convention bit-for-bit, and lock parity via tests against `report_generator._equal_width_bins`.

**Deliverables:**
- `engines/scoring.py::compute_bins(pairs: Sequence[tuple[float, int]], *, bin_count: int) -> ReliabilityDiagram` implemented natively in calibration_backtest. Do NOT import from `report_generator` (no compatible public API exists; reusable helpers are underscore-private).
- **Equal-width binning convention** (mirror report_generator exactly): width = `1.0 / bin_count`; bin edges rounded to 4 decimals via `round(i * width, 4)`; bins are half-open `[lo, hi)` except the **last bin** which is closed at 1.0 — a prediction `p == 1.0` lands in the last bin (`is_top_bin` inclusive rule).
- Empty-bin policy: emit exactly `bin_count` `ReliabilityBin` entries; bins with `count == 0` use `mean_predicted_p=None` and `empirical_rate=None` (compatible with calibration_backtest's `ReliabilityBin` model — no `sparse` field).
- `compute_reliability_diagrams_per_sector(conn, run_id, bin_count_global, bin_count_per_sector) -> dict[str, ReliabilityDiagram]` queries `backtest_predictions` filtered by `status='scored' AND brier_contribution IS NOT NULL`, groups by `sector`, and calls `compute_bins` per sector with `bin_count_per_sector.get(sector, bin_count_global)`.
- Return dict mapping sector → serializable `ReliabilityDiagram` with bin edges, counts, mean predicted/empirical rate per bin.
- Validate `bin_count >= 2` before constructing `ReliabilityDiagram` (the model raises `BacktestConfigError` for `<2`; the loader clamps to `[2,50]` silently — calibration_backtest must guard explicitly).
**Verification:**
- **Parity test (regression-critical):** assert calibration_backtest's bin edges equal `report_generator.engines.section_assemblers.reliability._equal_width_bins(bin_count)` for `bin_count in {2, 5, 10, 20}`. This is a private import for test purposes only — production code does NOT import it.
- **Top-bin boundary test:** pin bin assignment for boundary values `0.0`, `0.1`, `0.5`, `0.9999`, and `1.0` at `bin_count=10` (asserts `1.0` lands in the last bin, not raises an off-by-one error).
- **4-decimal rounding test:** assert bin edges for `bin_count=3` produce `[0.0, 0.3333, 0.6667, 1.0]` (rounded), not float-noise values like `0.30000000000000004`.
- **Empty-bin test:** with `pairs = [(0.05, 0), (0.95, 1)]` and `bin_count=10`, assert exactly 10 `ReliabilityBin` entries, with bins 0 and 9 populated and bins 1-8 carrying `count=0`, `mean_predicted_p=None`, `empirical_rate=None`.
- Integration test: run backtest on synthetic corpus, then assert `len(diagram.bins) == bin_count` for every sector with scored predictions.
**Out of scope:** summary assembly (T-CB-023).

### T-CB-023 — Assemble aggregate summary JSON for backtest_runs.summary_json
**Depends on:** T-CB-021, T-CB-022.
**References:** REQ-CB-SCORE-001, REQ-CB-SCORE-002, REQ-CB-SCORE-003, REQ-CB-SCORE-004, design §3.6.

> **Scout amendment (2026-05-31):** specify the canonical aggregation function so T-CB-024's compare engine produces zero deltas on self-compare.

**Deliverables:**
- `aggregate_run_summary(conn, run_id, bin_count_global, bin_count_per_sector) -> ScoreSummary` orchestrating overall, per-sector, per-class Brier and per-sector reliability diagrams.
- **Canonical Brier aggregation:** `per_sector_brier` and `per_class_brier` use `AVG(brier_contribution)` over rows with `status='scored' AND brier_contribution IS NOT NULL`. T-CB-024's compare engine MUST mirror this exact aggregation so a self-compare (`run_a_id == run_b_id`) produces `delta_absolute = 0.0` and `delta_percent = 0.0` for every cell.
- `models.py::ScoreSummary` extended with: `overall_brier`, `per_sector_brier`, `per_class_brier`, `zero_resolutions_sectors`, `zero_resolutions_classes`, `reliability_diagrams`, `fallback_polarity_rate`, `fallback_polarity_count`.
- `ScoreSummary.to_json()` using `json.dumps(sort_keys=True)` for determinism.
- `persistence/operations.py::complete_run(run_id, summary: ScoreSummary)` converts summary to JSON and persists to `backtest_runs.summary_json` (the existing complete_run from T-CB-019 already accepts a summary_json parameter; this task ensures the ScoreSummary -> JSON conversion is wired in).
**Verification:**
- Round-trip JSON serialization/deserialization preserves all fields.
- `zero_resolutions_*` populated correctly when a sector or class has zero scored predictions.
- Determinism: identical `ScoreSummary` inputs produce identical `to_json()` output (locked via `sort_keys=True`).
**Out of scope:** compare engine (T-CB-024).

### T-CB-024 — Implement compare engine with cell-level delta computation
**Depends on:** T-CB-023.
**References:** REQ-CB-SCORE-005, design §3.7, D3.

> **Scout amendment (2026-05-31):** `CompareCell` and the `PresentIn` enum are NOT yet defined in `models.py`. The miscalibration threshold currently lives only in `report_generator`'s config; calibration_backtest must add a local key to avoid runtime coupling to a sibling subsystem.

**Deliverables:**
- **Add to `models.py`** (frozen=True, slots=True):
  - `PresentIn(StrEnum)` with values `BOTH = 'both'`, `A_ONLY = 'a_only'`, `B_ONLY = 'b_only'`.
  - `CompareCell` dataclass with fields: `sector: str`, `class_id: str`, `brier_a: float | None`, `brier_b: float | None`, `delta_absolute: float | None`, `delta_percent: float | None`, `crossed_miscalibration_threshold: bool | None`, `present_in: PresentIn`, `trace_diff_summary: str | None = None` (None for v1; future trace-diff work would populate it).
- **Add to `config/backtest.yaml`:** `compare.brier_miscalibration_threshold: 0.25` (mirrors report_generator's name and default but keeps subsystem isolation — do NOT import `report_generator.config.loader` at runtime). Surface it via the existing `BacktestConfig` loader.
- `engines/compare.py::compare_runs(conn, run_a_id: str, run_b_id: str, *, threshold: float | None = None) -> list[CompareCell]` (rank_by removed from this task — T-CB-025 owns ranking; this returns unsorted cells).
- **Single SQL CTE pattern** (one round-trip):
  ```sql
  WITH a AS (
    SELECT sector, class_id, AVG(brier_contribution) AS brier_a
    FROM backtest_predictions
    WHERE run_id = :run_a_id AND status = 'scored' AND brier_contribution IS NOT NULL
    GROUP BY sector, class_id
  ),
  b AS (... same for run_b_id ...)
  SELECT
    COALESCE(a.sector, b.sector) AS sector,
    COALESCE(a.class_id, b.class_id) AS class_id,
    a.brier_a, b.brier_b,
    CASE WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL THEN b.brier_b - a.brier_a END AS delta_absolute,
    CASE WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL AND a.brier_a > 0
         THEN 100.0 * (b.brier_b - a.brier_a) / a.brier_a END AS delta_percent,
    CASE WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL THEN 'both'
         WHEN a.brier_a IS NOT NULL THEN 'a_only'
         ELSE 'b_only' END AS present_in
  FROM a FULL OUTER JOIN b USING (sector, class_id)
  ORDER BY sector, class_id
  ```
- **Aggregation parity:** `AVG(brier_contribution) FILTER status='scored' AND brier_contribution IS NOT NULL` MUST match T-CB-023's `per_sector_brier` aggregation exactly (so self-compare deltas are zero).
- **Threshold flag (Python layer):** `crossed_miscalibration_threshold = abs(delta_absolute) >= threshold` ONLY when `present_in == 'both'`; otherwise `None`. Threshold defaults to `compare.brier_miscalibration_threshold` from `backtest.yaml` (0.25); CLI/API may override.
**Verification:**
- **Self-compare test (correctness-critical):** `compare_runs(conn, run_id, run_id)` returns cells with `delta_absolute == 0.0` and `delta_percent == 0.0` for all cells where `brier_a > 0`; `delta_percent == None` where `brier_a == 0`.
- Asymmetric cells: cells in run_a but not run_b carry `present_in='a_only'`, `brier_b=None`, `delta_absolute=None`, `delta_percent=None`, `crossed_miscalibration_threshold=None`.
- Threshold flag fires correctly on `present_in='both'` cells with `abs(delta) >= 0.25`; never fires on asymmetric cells.
- `delta_percent == None` when `brier_a == 0` (division-by-zero guard).
**Out of scope:** ranking (T-CB-025).

### T-CB-025 — Implement compare ranking and sorting by absolute/percent delta
**Depends on:** T-CB-024.
**References:** REQ-CB-SCORE-005, design §3.7, D3.

> **Scout amendment (2026-05-31):** clarify None handling in sort keys.

**Deliverables:**
- `engines/compare.py::rank_compare_cells(cells: list[CompareCell], *, rank_by: Literal['absolute','percent']) -> list[CompareCell]`.
- `rank_by='absolute'`: stable-sort by `abs(delta_absolute)` descending; asymmetric cells (`present_in != 'both'`, where `delta_absolute is None`) sort to the **bottom** in stable order (sort key tuple `(present_in != BOTH, -abs(delta_absolute or 0.0))` or equivalent).
- `rank_by='percent'`: stable-sort by `abs(delta_percent)` descending; cells with `delta_percent is None` (asymmetric OR `brier_a == 0`) sort to the bottom.
- Tie-stability preserved across both modes (Python's `sorted` is stable; rely on it).
- CLI flag `--compare-rank-by {absolute|percent}` (default `absolute`) routes to this function (CLI integration in Phase 5).
**Verification:**
- Integration test: 4 cells (2 both-present with different deltas, 1 a_only, 1 b_only); `rank_by='absolute'` orders by `|delta_absolute|` desc, asymmetric cells last; `rank_by='percent'` does the same for `|delta_percent|`.
- Tie-stability: two cells with identical `|delta_absolute|` retain their input order.
- None-handling: cells with `delta_absolute=None` or `delta_percent=None` never raise `TypeError` from comparison.
**Out of scope:** CLI rendering (Phase 5).

### T-CB-026 — Wire bin-count resolution + CLI flags + canonical run_id hash
**Depends on:** T-CB-021, T-CB-022.
**References:** REQ-CB-SCORE-004, REQ-CB-RUN-001, REQ-CB-RUN-003, design §3.6, D6.

> **Scout amendment (2026-05-31):** use the existing `reliability_bin_count_for_sector` helper rather than dict-traversal; pass an absolute Path to `load_config` (DEFAULT_CONFIG_PATH is relative and CWD-dependent — silent all-defaults bug). Also folds in Phase 3 review advisories E and F (replace placeholder UUID and `system_revision="unversioned"` in `replay.py` with the canonical hash and resolver).

**Deliverables:**
- `engines/scoring.py::resolve_bin_counts(params: RunParameters, *, config_path: Path | None = None) -> tuple[int, dict[str, int]]`.
- **Resolution order** (highest priority first):
  1. CLI `--bin-count N` and `--bin-count-per-sector S=N` (carried on `RunParameters`).
  2. `report.yaml` per-sector via `cfg.thresholds.reliability_bin_count_for_sector(sector)` — this helper already implements the per-sector → global → default(10) fallback chain in one call. Do NOT traverse `cfg.thresholds.reliability_bin_count_per_sector` directly.
  3. `report.yaml` global via `cfg.thresholds.reliability_bin_count` (covered by the helper above).
  4. Module default `DEFAULT_BIN_COUNT = 10`.
- **Path resolution:** accept `config_path: Path | None`; if provided, pass an absolute `Path` to `load_config(path=config_path)`. If omitted, pass an absolute Path resolved against the workspace root (NOT relying on `DEFAULT_CONFIG_PATH` which is relative — it would silently return all-defaults when CLI runs from a different CWD).
- **Logging:** at startup, log the resolved config path and a warning if the YAML file is missing (the loader does NOT raise — calibration_backtest must distinguish "loaded" from "defaulted").
- **Validation:** assert `bin_count >= 2` before constructing any `ReliabilityDiagram` (the loader clamps to `[2, 50]` silently; the `ReliabilityDiagram` model raises `BacktestConfigError` for `<2`). For per-sector overrides, validate each.
- `bin_count_global` and `bin_count_per_sector_json` stored on `backtest_runs` row for auditability; **excluded from `run_id` hash** (per design §3.4 — the hash captures replay-determinism inputs only).
- CLI `run` command accepts `--bin-count N` and `--bin-count-per-sector SECTOR=N` (repeatable). [CLI surfacing lands in Phase 5; T-CB-026 wires the parameter plumbing.]
- **Canonical run_id wiring (clears Phase 3 review advisory E):** `engines/replay.py::run_backtest` calls `compute_run_id(params, library_version, system_revision)` instead of `uuid.uuid4().hex` for the `run_id`. The placeholder UUID is removed. `library_version` resolved from `pattern_library.current_version()`; `system_revision` resolved from `version.resolve_system_revision()`.
- **system_revision wiring (clears Phase 3 review advisory F):** the hard-coded `system_revision="unversioned"` strings in `replay.py` (currently at the IN_PROGRESS and COMPLETE persistence call sites) are replaced with `version.resolve_system_revision()`. The result is captured once at the top of `run_backtest` so it is consistent across the run row's lifecycle.
**Verification:**
- Unit test: resolution order — CLI override beats per-sector beats global beats default.
- Unit test: `--bin-count-per-sector A=5,B=10` parsed into a dict; missing sector falls through to global.
- Unit test: `load_config(path=missing_path)` returns ReportConfig defaults; calibration_backtest logs a warning and uses module default 10.
- Unit test: `bin_count=1` raises `BacktestConfigError` BEFORE constructing the diagram.
- Unit test: `compute_run_id` produces identical hash for two `RunParameters` differing only in `bin_count` (bin counts are excluded from the hash).
- **Determinism integration test:** running the same `RunParameters` twice produces the same `run_id` (canonical hash, not UUID).
**Out of scope:** rendering (Phase 5).

### T-CB-027 — Run Score-aggregation phase verification gates and integration tests
**Depends on:** T-CB-021, T-CB-022, T-CB-023, T-CB-024, T-CB-025, T-CB-026.
**References:** REQ-CB-SCORE-001, REQ-CB-SCORE-002, REQ-CB-SCORE-003, REQ-CB-SCORE-004, REQ-CB-SCORE-005, design §3.6, §3.7, D3, D6.

> **Scout amendment (2026-05-31):** explicit determinism assertion on bin edges (4-decimal rounding) so float-noise differences do not break determinism re-run gates.

**Deliverables:**
- `mypy --strict` clean on `engines/scoring.py`, `engines/compare.py`, and `models.py` additions.
- `ruff check` and `ruff format` clean on new code.
- Pytest: `tests/test_scoring.py`, `tests/test_compare.py` green.
- Integration test executing a full backtest end-to-end, calling `aggregate_run_summary`, and verifying JSON serialization matches schema.
- **Bit-equality integration test:** `compute_bins(pairs, bin_count=10)` bin edges equal `report_generator.engines.section_assemblers.reliability._equal_width_bins(10)` exactly (4-decimal rounding parity).
- **Determinism re-run gate:** running the same `RunParameters` twice produces (a) identical `run_id` (canonical hash, not UUID — clears Phase 3 advisory E), (b) identical `summary_json` byte-for-byte, (c) identical bin edges across both runs.
- No regressions in upstream subsystems; no circular dependency introduced.
**Verification:** all gates green.
**Out of scope:** CLI surfaces (Phase 5).

## Phase 5 — CLI

### T-CB-028 — Scaffold CLI module, register subgroup, and add mypy strict override
**Depends on:** Phase 1 (T-CB-001..T-CB-006), Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027).
**References:** REQ-CB-CLI-001, design §3.9.

> **Scout amendment (2026-05-31):** the existing CLI conventions (`razor-rooster <kebab>` group with `_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"` and `_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"`) are well-established in `signal_scanner/cli.py` and `monitor/cli.py`. click is already a hard dep (`pyproject.toml` line 17). Mirror the existing template verbatim. **Critical:** the subgroup definition AND the registration in `src/razor_rooster/cli.py` must land in the SAME commit — registering before the subgroup exists raises ImportError at module load and bricks every other CLI. **calibration_backtest is also missing from the mypy strict overrides** (peer subsystems all have it); add it here so T-CB-034's gate matches the rest of the repo.

**Deliverables:**
- Replace the placeholder `src/razor_rooster/calibration_backtest/cli.py` with `@click.group(name="calibration-backtest")` mirroring `signal_scanner/cli.py`:
  - `_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"`, `_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"`.
  - `_resolve_db_path(option_value: str | None) -> Path` honoring CLI flag → env var → default.
  - `_open_store(db_path: Path) -> tuple[duckdb.DuckDBPyConnection, DuckDBStore]` running ALL upstream migrations in dependency order (data_ingest → polymarket_connector → pattern_library → signal_scanner → mispricing_detector → position_engine → calibration_backtest), matching `monitor/cli.py` lines 84-90 verbatim.
- `@run` subcommand with flags: `--since`, `--until`, `--lag-days`, `--class-id` (repeatable), `--sector` (repeatable), `--venue` (repeatable), `--bin-count`, `--bin-count-per-sector` (repeatable, parsed as `KEY=N`), `--allow-recent`, `--format` (NOT `--output`; mirror `monitor/cli.py` lines 224-235 with `click.Choice(["terminal", "markdown", "html", "json"], case_sensitive=False)` defaulting to `"terminal"`), `--db PATH`.
- Wire `from razor_rooster.calibration_backtest.engines.replay import run_backtest` (NOT `api.run_backtest` — `api.py` is an empty placeholder; the real entry point is `engines.replay.run_backtest`). Required keyword-only args: `conn`, `store`. Optional `persistence_conn` MUST be passed equal to `conn` to actually persist rows. Returns `ReplayResult` (dataclass with `.run`, `.predictions`, `.traces`); render `result.run` for BacktestRun-shaped output.
- `models.py::RunParameters` already exists from Phase 3 with `since_ts`, `until_ts`, `lag_days`, `class_ids: list[str]`, `sectors: list[str]`, `venues: list[str]`, `allow_recent: bool`. Confirm `bin_count: int | None` and `bin_count_per_sector: Mapping[str, int]` overrides from T-CB-026 are wired through to RunParameters construction in cli.py.
- **Register the subgroup in `src/razor_rooster/cli.py`** in the same commit: add `from razor_rooster.calibration_backtest.cli import calibration_backtest` to the imports block (around line 22), and `main.add_command(calibration_backtest)` to the registrations block (around line 48).
- **Add mypy strict override to `pyproject.toml`** mirroring peer subsystems (around lines 85-119): `[[tool.mypy.overrides]] module = "razor_rooster.calibration_backtest.*" strict = true`.
- Use exit codes 0=ok, 1=not-found/usage error, 2=hard failure via `click.exceptions.Exit(code=N)` with `click.echo(..., err=True)` for stderr (match `signal_scanner/cli.py` lines 132-136, 173-176).
**Verification:**
- `razor-rooster --help` exits 0 (smoke test in `tests/test_cli_entrypoint.py` via CliRunner).
- `razor-rooster calibration-backtest --help` exits 0 and lists subcommands.
- `razor-rooster calibration-backtest run --help` shows all flags with correct defaults.
- `mypy --strict src/razor_rooster/calibration_backtest tests/calibration_backtest` is a HARD gate per the new override (matches CI's `mypy --strict src/razor_rooster/calibration_backtest tests/calibration_backtest` step).
**Out of scope:** rendering (T-CB-029, T-CB-030).

### T-CB-029 — Implement terminal, markdown, and HTML output formatters (with native SVG)
**Depends on:** T-CB-028.
**References:** REQ-CB-CLI-002, design §3.9, §3.12.

> **Scout amendment (2026-05-31):** **report_generator emits NO SVG anywhere** — its only chart helper is `render_chart()` returning an 11×21 ASCII grid wrapped in `<pre>`. The design's "imports report_generator.engines.section_assemblers.reliability to produce bit-equal SVG diagrams" is unimplementable as written. calibration_backtest must render SVG natively. Bit-equality (REQ-CB-SCORE-004 / P-CB-4) applies to the bin-tuple inputs (already parity-locked to `report_generator._equal_width_bins` per T-CB-022), NOT to rendered SVG bytes. **Also:** the framing linter's exception is `ImperativeLanguageDetected` (RuntimeError subclass), not `FramingError`, and its signature is `check_text(text, *, catalog=None, extra_phrases=())` — no `config_path: str` parameter.

**Deliverables:**
- `razor_rooster/calibration_backtest/renderers.py`:
  - `render_terminal(run: BacktestRun) -> str` formatting: run header (run_id, since_ts, until_ts, lag_days, library_version, system_revision, status), disclaimer block from `calibration_backtest.frame.DISCLAIMER`, prediction counts, overall_brier, per-sector and per-class Brier tables, fallback_polarity rate (with note when >5%).
  - `render_markdown(run: BacktestRun) -> str` mirroring structure with Markdown tables; reliability diagrams as indented code blocks (no SVG in markdown — markdown does not render inline SVG reliably across viewers).
  - `render_html(run: BacktestRun) -> str` generating minimal HTML with embedded CSS using plain string concatenation (mirror `report_generator/renderer/html.py`'s no-template-engine idiom). Embeds inline SVG produced natively by `render_reliability_svg`.
- `razor_rooster/calibration_backtest/renderers/reliability_svg.py::render_reliability_svg(diagram: ReliabilityDiagram, *, width: int = 320, height: int = 320, padding: int = 32) -> str`:
  - Returns a complete `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 W H'>...</svg>` string with explicit viewBox so embedding is size-stable.
  - Axes (two `<line>` at the inner padding rectangle), perfect-calibration y=x diagonal reference (one `<line>` from `(pad, H-pad)` to `(W-pad, pad)`).
  - Per non-empty bin: one `<rect>` x-positioned at `pad + bin_lo * (W - 2*pad)` with width `(bin_hi - bin_lo) * (W - 2*pad)`; height proportional to `empirical_rate`. Empty / sparse bins (`count == 0`) get `class="sparse"` styling (e.g., `stroke-dasharray`).
  - Per non-empty bin: one `<circle cx=... cy=... r=3>` at `(mean_predicted_p, empirical_rate)` so the operator can see model-vs-empirical at a glance.
  - Inline `<style>` inside the SVG so embedded SVG renders correctly when cut/pasted standalone.
- **Operator-supplied strings** (sector names, axis labels) embedded in `<svg><text>` MUST be HTML-escaped via the same `_html()` escape used in `report_generator/renderer/html.py` to avoid breaking the document. Numeric bin/probability values are safe by construction.
- All three renderers frame text in conditional language ("would", "could", "might", "if the operator chose"). The linter is denylist-only — it does NOT enforce conditional-voice presence; it rejects imperative phrases via case-insensitive substring match. Tests should assert imperative-phrase rejection only, NOT positive presence of "would/could/might".
- Each renderer calls `frame.check_cli_framing(text)` (defined in T-CB-033) which wraps `position_engine.frame.linter.check_text(text, catalog=_LINTER_CATALOG)`. The catalog is built ONCE at module import using an absolute Path (NOT relying on `DEFAULT_CATALOG_PATH` which is CWD-relative — silent under-coverage risk). Catches `ImperativeLanguageDetected` (NOT `FramingError`).
**Verification:**
- Unit tests for each renderer with synthetic `BacktestRun`.
- SVG: assert valid `<svg ...>...</svg>` markup; bin-tuple equality (NOT byte-equality) preserved across runs.
- Linter integration: feed text containing a known forbidden phrase from `config/forbidden_phrases.yaml` and assert `ImperativeLanguageDetected` is raised; assert `.phrase` attribute matches.
- HTML escape: a sector name containing `<script>` is escaped to `&lt;script&gt;` in the rendered output.
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

> **Scout amendment (2026-05-31):** the design's `api.run_backtest(params)` does not exist (`api.py` is an empty placeholder). Wire to `engines.replay.run_backtest(params, *, conn, store, persistence_conn=conn)` returning `ReplayResult`. Render `result.run`. No earliest-resolution helper exists for the bare-run default; CLI must issue raw SQL and guard against NULL.

**Deliverables:**
- `cli.py` run command dispatches to the appropriate renderer based on `--format` (NOT `--output` — see T-CB-028 amendment).
- Run command:
  1. Opens DuckDB conn + DuckDBStore via `_open_store(_resolve_db_path(db_option))`.
  2. Calls `result = run_backtest(params, conn=conn, store=store, persistence_conn=conn)` — both `conn` and `store` are required keyword-only.
  3. Renders `result.run` via the dispatched renderer.
  4. Prints to stdout for terminal/markdown/html; writes JSON to stdout.
- **Bare-run defaults** (when none of `--since`, `--until`, `--class-id`, `--sector`, `--venue` are provided):
  - `since_ts`: issue `SELECT MIN(resolution_ts) FROM polymarket_resolutions` directly in cli.py. If NULL (empty table), raise `BacktestConfigError` with operator hint pointing to `razor-rooster ingest` to populate `polymarket_resolutions`.
  - `until_ts`: `now() - timedelta(days=30)` (the recent-window cutoff per REQ-CB-RUN-002).
  - `lag_days`: `7` (per `config/backtest.yaml::default_lag_days`).
  - `class_ids`: from `pattern_library.list_classes()`.
  - `sectors`: `[]` (all sectors).
  - `venues`: `['polymarket']`.
- **Smoke-check before invoking `run_backtest`:** issue a count over `iter_mapped_resolutions(conn, since_ts, until_ts, venues, class_ids)` to fail fast on mis-wired DB paths (zero mapped resolutions → exit code 1 with a clear error message).
- Integration test: `razor-rooster calibration-backtest run --db /tmp/test.duckdb` against a populated test corpus exits 0; stdout contains expected sections; output passes the framing linter.
- Unit tests for each `--format` value against canned fixtures.
**Verification:** integration test confirms bare-command works on a seeded DB; output rendered; JSON valid with disclaimer; bare-run with empty `polymarket_resolutions` raises `BacktestConfigError` with helpful operator hint (NOT a cryptic NULL error).
**Out of scope:** other subcommands (T-CB-032).

### T-CB-032 — Implement list, show, compare, and prune CLI commands
**Depends on:** T-CB-028, T-CB-029.
**References:** design §3.9.

> **Scout amendment (2026-05-31):** `RunNotFoundError` does not exist in `errors.py` (must be added). No `prune_runs` / `delete_run` helper exists in `persistence.operations`. **Schemas.py explicitly OMITS FK constraints** (DuckDB FK support is limited per the schema comment) — cascade delete WILL NOT work via FKs. Prune must issue 3 ordered DELETEs in a transaction. The `compare_runs` signature additionally accepts a keyword-only `threshold: float | None = None` which can be exposed as `--threshold FLOAT`.

**Deliverables:**
- `list` command parses `--since ISO`, `--limit N` (default 50); calls `persistence.operations.list_runs(conn, since=..., limit=...)` (returns `tuple[BacktestRun, ...]` ordered `started_at DESC`); renders table with run_id (12-char), started_at, library_version, truncated system_revision, lag_days, prediction counts, overall_brier, fallback_polarity_rate, status; passes through framing linter.
- `show` command accepts positional `RUN_ID` and optional `--format FORMAT`. Calls `persistence.operations.fetch_run(conn, run_id)` (returns `BacktestRun | None`). If None, raises `RunNotFoundError(run_id)` and exits with code 1.
- **Add `RunNotFoundError(CalibrationBacktestError)` to `calibration_backtest/errors.py`** with `__init__(self, run_id: str)` storing `self.run_id` and a deterministic message. Re-export from `calibration_backtest/__init__.py.__all__`.
- `compare` command accepts positional `RUN_A`, `RUN_B`, optional `--compare-rank-by {absolute|percent}` (default `absolute`), optional `--top N`, optional `--threshold FLOAT` (passes through to `compare_runs(..., threshold=...)`). Calls `engines.compare.compare_runs(conn, run_a_id, run_b_id, threshold=...)` then `engines.compare.rank_compare_cells(cells, rank_by=...)`. Renders ranked table with sector, class_id, brier_a, brier_b, delta_absolute, delta_percent, crossed_miscalibration_threshold, present_in; passes through framing linter.
- `prune` command requires `--before ISO` and `--confirm`. Without `--confirm`, exits 1 with a usage warning (mirror `signal_scanner/cli.py` lines 308-350 verbatim).
- **Add `prune_run(conn, run_id)` to `persistence/operations.py`** that opens a transaction (`with conn:` — DuckDB context manager auto-commits or rolls back on exception) and issues, in order: `DELETE FROM backtest_traces WHERE run_id = ?`, `DELETE FROM backtest_predictions WHERE run_id = ?`, `DELETE FROM backtest_runs WHERE run_id = ?`. Returns row-counts dict for CLI summary output.
- `prune` CLI calls `prune_run` for each row matching the `--before` filter, accumulates row counts, prints summary.
- All non-JSON outputs pass through the linter; rejection raises `ImperativeLanguageDetected` (NOT `FramingError`) and exits with code 2.
**Verification:**
- Unit tests for each command with synthetic fixtures.
- Integration test against seeded database.
- `prune` refuses without `--confirm` (exit code 1 + warning).
- Mid-transaction failure injection: assert no orphan trace/prediction rows remain after a failed `prune_run`.
**Out of scope:** GUI (Phase 6).

### T-CB-033 — Build frame module + linter wrapper + CLI compliance audit
**Depends on:** T-CB-029, T-CB-030, T-CB-032.
**References:** REQ-CB-CLI-002, REQ-CB-CLI-003, design §3.10, §3.12.

> **Scout amendment (2026-05-31):** the linter signature is `check_text(text, *, catalog=None, extra_phrases=())` raising `ImperativeLanguageDetected` (RuntimeError subclass), NOT `check_text(text, config_path)` raising `FramingError`. **Critical risk:** `DEFAULT_CATALOG_PATH` is CWD-relative; if cwd != repo root and no explicit catalog is passed, `LinterCatalog.from_yaml` silently falls back to a 10-phrase default list instead of the full ~45-phrase YAML — silent under-coverage. Build the catalog ONCE at module import using an absolute Path.

**Deliverables:**
- `razor_rooster/calibration_backtest/frame.py` with:
  - Module constant `DISCLAIMER: str` (exact text from design §3.12).
  - Module constant `FOOTER_NOTE: str` (footer text from design §3.12).
  - Module-level `_LINTER_CATALOG: LinterCatalog = LinterCatalog.from_yaml(Path(__file__).resolve().parents[3] / "config" / "forbidden_phrases.yaml")` built once at import time. Path resolves against `<repo>/config/forbidden_phrases.yaml` regardless of CWD.
  - **Startup assertion:** `assert len(_LINTER_CATALOG.phrases) > 10, "calibration_backtest framing catalog under-loaded; check repo layout"` — fails loudly if the YAML is missing instead of silently under-linting.
  - Helper `check_cli_framing(text: str) -> None` wrapping `position_engine.frame.linter.check_text(text, catalog=_LINTER_CATALOG)`. Lets `ImperativeLanguageDetected` propagate (do NOT wrap in a calibration_backtest-local exception — the existing exception's `.phrase` and `.snippet` attributes are useful for downstream error reporting; renaming would lose information).
- All renderers include `DISCLAIMER` block at top of terminal/markdown/html outputs.
- Terminal/markdown append `FOOTER_NOTE` at end; HTML places both in semantic sections with CSS classes (`<section class="disclaimer">`, `<section class="footer-note">`).
- `tests/test_cli_framing.py::test_all_renders_pass_linter`:
  - Runs all five CLI commands (run, list, show, compare, prune) against a seeded database via `CliRunner`.
  - Captures outputs from each `--format` value.
  - Asserts each passes `check_cli_framing()` without raising.
  - Confirms `DISCLAIMER` substring is present in terminal/markdown/html outputs.
  - Confirms JSON output includes a `"disclaimer": ...` field with the canonical text.
- `tests/test_cli_framing.py::test_cwd_independence`:
  - Calls `os.chdir(tmp_path)` before importing renderers.
  - Asserts `len(frame._LINTER_CATALOG.phrases) > 10` (full YAML loaded, NOT the 10-phrase fallback).
**Verification:**
- Linter integration test passes.
- All command outputs carry expected disclaimer + footer.
- No forbidden phrases in fixtures.
- CWD-independence test passes (catches the silent under-coverage regression).
**Out of scope:** GUI framing (Phase 6).

### T-CB-034 — Run CLI phase verification gates (mypy, ruff, pytest, --help smoke)
**Depends on:** T-CB-028, T-CB-029, T-CB-030, T-CB-031, T-CB-032, T-CB-033.
**References:** REQ-CB-CLI-001, REQ-CB-CLI-002, REQ-CB-CLI-003.

> **Scout amendment (2026-05-31):** explicit gate matching CI's hard-gate exactly so a Phase-3-style local/CI mismatch cannot recur.

**Deliverables:**
- `mypy --strict src/razor_rooster/calibration_backtest tests/calibration_backtest` clean (matches CI's hard gate verbatim — runs strict against BOTH src and tests).
- `mypy --strict src/razor_rooster/calibration_backtest/cli.py src/razor_rooster/calibration_backtest/renderers.py src/razor_rooster/calibration_backtest/frame.py` clean (focused subset).
- `ruff check src tests` clean across the repo; imports sorted.
- `ruff format --check src tests` clean.
- Pytest selectors `-k 'test_cli or test_render or test_framing'` green.
- `tests/test_cli_framing.py` green.
- `tests/test_cli_entrypoint.py::test_razor_rooster_help_exits_zero` green (smoke-checks the top-level `razor-rooster --help` does not crash from registering calibration-backtest).
- `razor-rooster calibration-backtest --help` and `razor-rooster calibration-backtest run --help` execute without errors (run via CliRunner in pytest, not bash, so coverage tracking works).
- Full pytest suite green (no regressions in upstream subsystems from CLI changes).
**Verification:** all gates green; no type errors; no lint violations; pytest pass rate 100%.
**Out of scope:** GUI (Phase 6).

## Phase 6 — GUI

> The GUI phase implements two read-only routes for listing and viewing calibration backtest runs. All renders pass through the framing linter (REQ-CB-CLI-002), include standard disclaimer blocks (REQ-CB-CLI-003), and reuse operator auth from `report_generator`. Tasks address REQ-CB-CLI-004 (GUI surface).

### T-CB-035 — Scaffold GUI router and route module
**Depends on:** Phase 2 (T-CB-007..T-CB-013), Phase 3 (T-CB-014..T-CB-020), Phase 4 (T-CB-021..T-CB-027), Phase 5 (T-CB-028..T-CB-034 — provides `frame.py`, `renderers/reliability_svg.py`, `RunNotFoundError`).
**References:** REQ-CB-CLI-004, design §3.14, §3.15.

> **Scout amendment (2026-05-31):** the GUI is **FastAPI** (not Flask). It uses **APIRouter** registered via `app.include_router` in `_register_routes` (gui/app.py:131-149). There is **no** `report_generator.gui.auth` module — `report_generator` has no GUI submodule at all. Operator auth is **network-level only** (loopback binding enforced at `gui/cli.py:107-118`). The framing linter is **globally wired** via `LinterMiddleware` (gui/app.py:45-101) — every text/html response auto-passes through it. Templates use **Jinja2** via `fastapi.templating.Jinja2Templates`, with `_base.html` as the inheritance root. Tests use `fastapi.testclient.TestClient` with function-scoped fixtures.

**Deliverables:**
- Add new module `src/razor_rooster/gui/routes/calibration_backtest.py` exporting `router = APIRouter()` with two GET handlers:
  - `@router.get("/calibration-backtest", response_class=HTMLResponse)` — list view (T-CB-036)
  - `@router.get("/calibration-backtest/{run_id}", response_class=HTMLResponse)` — detail view (T-CB-037)
- Each handler signature: `async def name(request: Request) -> Response`, opens DuckDB via `with open_store(request.app.state.db_path) as conn:`, returns `render_template(request, "calibration_backtest/<page>.html", context)` (the typed wrapper from `gui/_render.py`, NOT raw `TemplateResponse` — needed for mypy --strict).
- Register router via **local import** inside `gui/app.py::_register_routes` (mirroring lines 131-142 — local imports preserve the no-circular-import guarantee). Add two lines: `from razor_rooster.gui.routes.calibration_backtest import router as cb_router` and `app.include_router(cb_router)`.
- Add a nav link to `gui/templates/_base.html` (lines 12-19, alongside Dashboard/Reports/Digest/Compare/Watch/Calibration) pointing to `/calibration-backtest`.
- Create `gui/templates/calibration_backtest/` with `list.html` and `detail.html` extending `_base.html`. Set `{% block title %}` and `{% block content %}`.
- **Drop the `report_generator.gui.auth` import entirely** — the module does not exist. Drop the auth decorator. Security inherits from loopback-only binding (gui/cli.py:107-118) and `LinterMiddleware`.
- **Drop the framing-linter wrapping per route** — `LinterMiddleware` (gui/app.py:45-101) already wraps every text/html response with `check_text`. Re-adding it would double-run.
- **Read-only invariant:** only GET routes; never POST/PUT/DELETE/PATCH (test_no_state_mutation_routes_registered enforces this).
- **No external assets:** no `http(s)://` URLs in templates, no external `<script>`/`<link>` tags, inline SVG only (test_no_external_assets_in_any_page enforces this).
**Verification:** module imports cleanly; `app.include_router(cb_router)` registers under `_register_routes`; `TestClient(create_app(db_path=...)).get("/calibration-backtest")` returns 200 (smoke); template files extend `_base.html`.
**Out of scope:** route handlers' bodies (T-CB-036, T-CB-037).

### T-CB-036 — Implement list view (`/calibration-backtest` route)
**Depends on:** T-CB-035.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.12.

> **Scout amendment (2026-05-31):** drop the per-route linter call — `LinterMiddleware` already covers it globally. Use existing `list_runs` from Phase 4.

**Deliverables:**
- `GET /calibration-backtest` handler calling `persistence.operations.list_runs(conn, limit=..., offset=...)` (already paginated; sorted `started_at DESC`).
- Query params: `?limit=N` (default 50, max 200) and `?offset=M` for pagination.
- Columns rendered in `list.html`: run_id (12-char prefix linked to `/calibration-backtest/{run_id}`), started_at, library_version, system_revision (16-char prefix), lag_days, predictions_total, predictions_scored, overall_brier (4-decimal), fallback_polarity_rate (% formatted; show "—" when no scored predictions).
- Status badge (`in_progress` | `complete` | `failed`) styled via inline CSS (no external stylesheet — must satisfy test_no_external_assets_in_any_page).
- `list.html` extends `_base.html`; `{% block content %}` includes:
  - Page heading
  - Disclaimer block (`{{ disclaimer }}` — passed in via context, sourced from `calibration_backtest.frame.DISCLAIMER`)
  - Table with the columns above
  - Pagination prev/next links honoring `?limit` + `?offset`
- All chrome strings (headings, labels, badge text) are static or come from `frame.DISCLAIMER` / `frame.FOOTER_NOTE`. `LinterMiddleware` will catch any imperative phrasing on response.
**Verification:** route returns 200; table rows match seeded `backtest_runs` ordered `started_at DESC`; pagination respects `?limit` and `?offset`; status badge styling correct; `test_no_external_assets_in_any_page` and `test_no_state_mutation_routes_registered` pass for the new route; LinterMiddleware never rejects.
**Out of scope:** detail view (T-CB-037).

### T-CB-037 — Implement detail view (`/calibration-backtest/{run_id}` route)
**Depends on:** T-CB-035, T-CB-036.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.6, §3.12, OQ-CB-002.

> **Scout amendment (2026-05-31):** **`report_generator.engines.section_assemblers.reliability(..., bin_count=...)` does not produce SVG and never has** — `report_generator` only emits an 11×21 ASCII chart wrapped in `<pre>`. Reuse `calibration_backtest.renderers.reliability_svg.render_reliability_svg(diagram, sector_label=...)` from Phase 5. Lift the diagram-hydration helpers (`_reliability_diagrams_from_run`, `_hydrate_diagram`) out of `report_generator/renderer/html.py` (lines 302-361) into a shared module so the HTML renderer and GUI consume the same hydration path.

**Prerequisite refactor (lift-and-share):**
- Create `src/razor_rooster/calibration_backtest/renderers/_diagram_hydrate.py` exporting:
  - `reliability_diagrams_from_run(run: BacktestRun) -> dict[str, ReliabilityDiagram]` (rehydrates per-sector diagrams from `run.summary_json["reliability_diagrams"]`)
  - `hydrate_diagram(payload: Mapping[str, Any]) -> ReliabilityDiagram`
- Move the corresponding logic out of `report_generator/renderer/html.py` (lines 302-361) and import from this shared module so `html.py` and the new GUI view share one path. Schema changes to `ReliabilityBin` then propagate atomically.
- Add a contract test that round-trips a `BacktestRun.summary_json` through the shared hydrator and asserts both `html.py` and the GUI consume identical `ReliabilityDiagram` instances.

**Deliverables:**
- `GET /calibration-backtest/{run_id}` handler:
  1. `run = persistence.operations.fetch_run(conn, run_id)`. If `None`, raise `HTTPException(404)` with a clear message.
  2. Extract per-sector and per-class Brier from `run.summary_json`. Resolve `bin_count_global` and `bin_count_per_sector` from the run row.
  3. Hydrate per-sector diagrams via `reliability_diagrams_from_run(run)`.
  4. Pre-render SVGs in the route handler: `reliability_svgs = {sector: render_reliability_svg(diagram, sector_label=sector) for sector, diagram in diagrams.items()}`. Catch `BacktestConfigError` to surface degenerate/invalid diagrams as a 500 with a meaningful message.
  5. Pass `reliability_svgs` (str-valued dict), per-sector/per-class Brier dicts, run metadata, and disclaimer into context.
- `detail.html` template:
  - Header section: run parameters, library_version, system_revision (full), status, prediction counts (total / scored / skipped grouped by skip_reason).
  - Per-sector Brier table.
  - Per-class Brier table.
  - Per-sector reliability diagrams: `{% for sector, svg in reliability_svgs.items() %}<section class="diagram"><h3>Sector: {{ sector }}</h3><div class="diagram-wrapper">{{ svg | safe }}</div></section>{% endfor %}`. The `| safe` filter is correct because `render_reliability_svg` returns a complete `<svg>...</svg>` element with HTML-escaped operator strings (sector labels go through `html.escape` inside the helper).
  - Fallback-polarity-rate banner: when rate >5%, render highlight note in a `<section class="warning">` block. Use static, conditional-voice language ("Operators may want to investigate..." not "Investigate this..."). LinterMiddleware enforces.
  - Disclaimer block at top.
- **DO NOT pass user-controlled width/height/padding to `render_reliability_svg`.** Use defaults (320, 320, 32).
**Verification:**
- 200 for seeded run; 404 for unknown run_id.
- Banner appears at >5% fallback rate.
- Reliability SVGs are valid `<svg>...</svg>` markup with `viewBox`; `len(diagram.bins) == bin_count`.
- Bin-tuple inputs match `report_generator._equal_width_bins(bin_count)` (parity inherited from T-CB-022 / Phase 5).
- Contract test: `reliability_diagrams_from_run(run)` produces identical `ReliabilityDiagram` instances when called from `html.py` and from the GUI view.
**Out of scope:** predictions table (T-CB-038).

### T-CB-038 — Implement predictions table with pagination and filtering
**Depends on:** T-CB-035, T-CB-037.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.7, §3.14.

> **Scout amendment (2026-05-31):** `persistence.operations` only has `fetch_predictions(conn, run_id) -> tuple[BacktestPrediction, ...]` — no filters, no pagination, no count. Add `list_predictions` and `count_predictions` to `operations.py` BEFORE the route handler. Drop the AJAX-loaded trace_diff_summary column entirely for v1 (deferred). LinterMiddleware covers chrome strings globally.

**Prerequisite (persistence helpers):**
- Add `list_predictions(conn, run_id, *, status: PredictionStatus | None = None, skip_reason: SkipReason | None = None, limit: int = 20, offset: int = 0) -> tuple[BacktestPrediction, ...]` to `calibration_backtest/persistence/operations.py`. Mirror `list_runs`'s pattern: validate `limit >= 0`, `offset >= 0` (raise `BacktestPersistenceError`), build SQL with optional WHERE clauses appended for `status` and `skip_reason`, **`ORDER BY prediction_id ASC`** (matching `fetch_predictions` so pagination is correct across pages), `LIMIT ? OFFSET ?`. Reuse `_PREDICTION_SELECT_COLUMNS` and `_row_to_prediction`. Wrap `duckdb.Error` via `_wrap_db_error("list_predictions failed", exc)`.
- Add `count_predictions(conn, run_id, *, status=None, skip_reason=None) -> int` for "Page N of M" rendering.
- **Filter binding:** pass enum values via `str(status)` and `str(skip_reason)` (matching how `_prediction_params` stores them) — passing the enum object directly produces zero rows silently.
- Append both names to `__all__`.
- Tests: `tests/calibration_backtest/test_list_predictions.py` covering filter combinations, limit/offset boundaries, ordering stability, validation errors.

**Deliverables (route + template):**
- Detail-view predictions section in `detail.html` rendering `list_predictions(...)` results in a paged table.
- Columns: prediction_id (truncated 12-char), class_id, condition_id, venue, sector, prediction_ts, resolution_ts, model_p (4-decimal), observed, polarity, polarity_source, status, skip_reason (only when status=skipped, else "—").
- Filter tabs/dropdown: 'All', 'Scored', 'Skipped', then `Skip reason: {reason}` per unique reason discovered via `count_predictions(..., status=skipped, skip_reason=...)` calls.
- Query params: `?status=&skip_reason=&page=N&limit=20` (default limit 20, max 200). `page` is 1-indexed; convert to `offset = (page - 1) * limit` before calling `list_predictions`.
- Pagination footer with "Page N of M" + prev/next links honoring active filters.
- **DEFER**: AJAX `trace_diff_summary` column. Add a `# DEFER-CB-006` comment in the route module and document in design §7.
- Data cells render numeric/timestamp values verbatim; LinterMiddleware will catch any imperative phrasing in chrome.
**Verification:**
- Filter tabs return correct subsets (assert via `count_predictions` cross-check).
- Pagination boundary test: seed `>= 2 * limit` rows; assert page 1 + page 2 union equals full set with no duplicates or gaps.
- `list_predictions` ordering stable (matches `fetch_predictions` ASC ordering).
- LinterMiddleware never rejects.
**Out of scope:** trace decompression and `trace_diff_summary` (deferred to v2).

### T-CB-039 — Verify framing linter coverage and disclaimer rendering
**Depends on:** T-CB-035.
**References:** REQ-CB-CLI-002, REQ-CB-CLI-004, design §3.9, §3.12.

> **Scout amendment (2026-05-31):** the framing linter is **already wired globally** — `LinterMiddleware` in `gui/app.py:45-101` wraps every text/html response and runs `check_text` on the decoded body. Re-adding per-route linter calls would double-run. Rewrite the task to **verify** existing global coverage rather than wire new calls.

**Deliverables:**
- Pass `disclaimer = DISCLAIMER` and `footer_note = FOOTER_NOTE` (sourced from `calibration_backtest.frame`) into the Jinja context for both list and detail views. Templates emit them as static blocks.
- Confirm via integration test that `LinterMiddleware` rejects an injected forbidden phrase: temporarily monkey-patch the route to embed a known imperative phrase from `config/forbidden_phrases.yaml`; assert the response is 500 (or whatever `LinterMiddleware` raises) with `ImperativeLanguageDetected` in the error chain.
- `tests/gui/test_calibration_backtest_framing.py`:
  - `test_disclaimer_in_list_response`: GET `/calibration-backtest`; assert DISCLAIMER substring in response.text.
  - `test_disclaimer_in_detail_response`: GET `/calibration-backtest/{run_id}`; assert DISCLAIMER substring.
  - `test_footer_note_in_both_responses`: assert FOOTER_NOTE substring at end of both responses.
  - `test_lintermiddleware_catches_forbidden_phrase`: inject a known forbidden phrase via a test-only route; assert middleware rejects.
  - `test_no_per_route_linter_call_in_calibration_backtest_module`: AST-grep `gui/routes/calibration_backtest.py` and assert no `check_text` import or call (relies on middleware).
- **DO NOT** add a `gui/frame.py` re-export module; consumers should import from `calibration_backtest.frame` directly.
- **DO NOT** wrap individual template strings with `check_text` calls — `LinterMiddleware` covers them.
**Verification:** all four tests pass; AST check confirms no double-run; disclaimer renders at page top on both routes.
**Out of scope:** route logic (T-CB-036..T-CB-038).

### T-CB-040 — Add comprehensive GUI route tests with seeded data
**Depends on:** T-CB-035, T-CB-036, T-CB-037, T-CB-038, T-CB-039.
**References:** REQ-CB-CLI-004, design §3.14, §4.2.

> **Scout amendment (2026-05-31):** mirror `tests/gui/conftest.py` + `tests/gui/test_routes.py` patterns verbatim — `fastapi.testclient.TestClient` wrapping `create_app(db_path=...)`, function-scoped fixtures. Drop the auth test (no auth layer exists; security is loopback-only). Seed `>= 2 * limit` predictions per run so pagination boundaries are exercised.

**Deliverables:**
- `tests/gui/conftest.py` extension: add a `populated_backtest_db` function-scoped fixture that:
  - Creates a fresh DuckDB at `tmp_path / "test.duckdb"`.
  - Runs all upstream migrations (data_ingest, polymarket, pattern_library, signal_scanner, mispricing, position_engine, calibration_backtest) in dependency order.
  - Inserts ~3 `BacktestRun` rows (mixed `BacktestStatus`: complete, in_progress, failed) via `persist_score_summary` / `insert_run`.
  - Inserts `>= 2 * 20 = 40` `BacktestPrediction` rows per run, mixing `status=scored` and `status=skipped` with both common `skip_reason` values (e.g., `mapping_mismatch`, `outside_window`).
  - Inserts 1-2 `BacktestTrace` rows via `insert_trace` for the detail-view trace section.
- `tests/gui/test_calibration_backtest_routes.py`:
  - `test_list_route_200`: GET `/calibration-backtest` returns 200; HTML contains all 3 seeded run_ids; ordered `started_at DESC`.
  - `test_list_run_id_link_format`: each row's run_id is a link to `/calibration-backtest/<full_run_id>` (NOT the truncated 12-char prefix in the href).
  - `test_list_pagination`: GET `/calibration-backtest?limit=2`; assert exactly 2 rows; GET `?limit=2&offset=2`; assert remaining row(s).
  - `test_detail_route_200`: GET `/calibration-backtest/<run_id>` returns 200; HTML contains run metadata, per-sector Brier table, per-class Brier table, ≥1 inline `<svg>` element.
  - `test_detail_missing_run_404`: GET `/calibration-backtest/nonexistent_run_id` returns 404.
  - `test_detail_fallback_banner`: seed a run with >5% `fallback_polarity_rate`; assert `<section class="warning">` block present.
  - `test_predictions_pagination`: GET `/calibration-backtest/<run_id>?page=1&limit=20`; assert 20 rows; `?page=2&limit=20`; assert remaining 20 rows; assert no overlap between page sets.
  - `test_predictions_filter_status`: GET `?status=skipped`; assert only skipped rows.
  - `test_predictions_filter_skip_reason`: GET `?skip_reason=mapping_mismatch`; assert only matching rows.
  - `test_no_external_assets`: re-run `tests/gui/test_routes.py::test_no_external_assets_in_any_page` against the new routes (asserts no `http(s)://` URLs, no external `<script>`/`<link>` tags).
  - `test_no_state_mutation`: assert all calibration_backtest routes are GET-only.
- **DROP the auth test** — no auth layer exists; security is loopback-only and inherited from `gui/cli.py`.
**Verification:** all GUI tests green; pagination boundary test confirms page 1 + page 2 union = full set with no duplicates; filter tests confirm correct subsets; LinterMiddleware never rejects.
**Out of scope:** CLI tests (Phase 5).

### T-CB-041 — GUI verification gates (mypy, ruff, pytest, no-circular)
**Depends on:** T-CB-035, T-CB-036, T-CB-037, T-CB-038, T-CB-039, T-CB-040.
**References:** REQ-CB-CLI-004, REQ-CB-CLI-002, design §3.14, §3.15.

> **Scout amendment (2026-05-31):** the new module lives at `src/razor_rooster/gui/routes/calibration_backtest.py` (FastAPI router), NOT `razor_rooster/calibration_backtest/gui/`. Adjust gate paths. Add explicit no-top-level-import check (router must be imported locally inside `_register_routes` to preserve no-circular guarantee). Match CI's hard-gate verbatim.

**Deliverables:**
- **CI hard-gate match:** `mypy --strict src/razor_rooster/calibration_backtest tests/calibration_backtest tests/gui` clean (matches CI's hard-gate target verbatim — runs strict against src + the new tests dir).
- `mypy --strict src/razor_rooster/gui/routes/calibration_backtest.py` clean (focused subset).
- `ruff check src tests` clean.
- `ruff format --check src tests` clean.
- `pytest tests/gui` green (including the new `test_calibration_backtest_routes.py` and `test_calibration_backtest_framing.py`).
- Full pytest suite green (no regressions in upstream subsystems from the GUI route addition).
- **Static no-circular-import check:** `grep -E "^from razor_rooster.calibration_backtest|^import razor_rooster.calibration_backtest" src/razor_rooster/gui/app.py` returns ZERO matches at module top-level. The import must be inside `_register_routes` (local import).
- **Static no-cross-pollution check:** `grep -rn "from razor_rooster.calibration_backtest\|from razor_rooster.gui" src/razor_rooster/report_generator/` returns ZERO matches (`report_generator` does not import either downstream subsystem).
- **DEFER-CB-005 + DEFER-CB-006 documented** in `CALIBRATION_BACKTEST_DESIGN.md` §7: JS interactivity (collapsible bin tooltips, sortable tables); trace_diff_summary AJAX endpoint; dark-mode CSS for inline SVG.
**Verification:** all gates green; no top-level circular import; no `report_generator` -> `calibration_backtest`/`gui` imports.
**Out of scope:** non-GUI surfaces.

## Phase 7 — Pattern-Library upgrade

### T-CB-042 — Implement polymarket_resolution_calibration._occurrences SQL query
**Depends on:** Phase 1 (T-CB-001..T-CB-006).
**References:** REQ-CB-PL-001, REQ-CB-FREEZE-003, design §3.16, OQ-CB-002, OQ-CB-005.

> **Scout amendment (2026-05-31, Phase 3):** `comparison_resolutions` does NOT have a `class_id` column — it must be derived by joining through the `comparisons` table on `comparison_id`. The query is therefore a **three-table** join, not two. Until `mispricing_detector` linkage matures, some predictions may not flow into `comparison_resolutions`; treat coverage as expected partial, not a defect.
>
> **Scout amendment (2026-06-01, Phase 7):** four additional drift items the prior amendment missed:
> 1. **Column rename:** the real `polymarket_resolutions` column is `winning_outcome_label`, NOT `pr.outcome` (design §3.16 was wrong; verified against `polymarket_connector/persistence/schemas.py`).
> 2. **Protocol contract is `(conn) -> DataFrame`:** the `OccurrenceQuery` protocol at `pattern_library/models/event_class.py:69` is a single-arg callable; `refresh.py:314` and `base_rates.py:85` both call `cls.occurrence_query(conn)` with no extra args. **DO NOT add `:since_ts/:until_ts/:class_id` bind parameters** — that would break the protocol and the seven other seed classes plus T-CB-047's `mypy --strict` check. Time-window filtering happens downstream via `refresh._count_in_window`. The `class_id` filter is also redundant — `_occurrences` is bound to its own class.
> 3. **File path: no `meta/` subpackage.** The file lives at `pattern_library/classes/polymarket_resolution_calibration.py` (flat). The registry's `pkgutil.iter_modules` at `pattern_library/registry.py:245` does NOT recurse into subpackages — adding a `meta/` subdir would silently NOT register the meta-class.
> 4. **Polarity double-correction trap:** `comparison_resolutions.outcome_observed` is **already polarity-adjusted at write time** (per `mispricing_detector/models.py:148-149`). The meta-class must read raw `pr.winning_outcome_label` and apply `cr.polarity_at_comparison` itself. Reading `cr.outcome_observed` plus `cr.polarity_at_comparison` would apply polarity twice — silent calibration corruption with no error raised.

**Deliverables:**
- `pattern_library/classes/polymarket_resolution_calibration.py::_occurrences(_conn)` replaces the empty-frame stub with a real DuckDB query. Signature stays `(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame` — single-arg per `OccurrenceQuery` protocol.
- **Three-table join (no bind parameters):**
  ```sql
  SELECT
    c.condition_id,
    c.class_id,
    cr.polarity_at_comparison,
    pr.winning_outcome_label,
    pr.resolution_ts AS occurrence_ts,
    pr.invalidated
  FROM comparison_resolutions cr
  JOIN comparisons c USING (comparison_id)
  JOIN polymarket_resolutions pr USING (condition_id)
  WHERE pr.invalidated = FALSE
    AND pr.superseded_at IS NULL
  ```
- Returns ALL occurrences across history; `refresh._count_in_window` applies time filtering downstream.
- Mirrors the `eia_grid_reliability_event.py:39-56` idiom (`conn.execute(...).fetchall()` then construct DataFrame manually) for consistency with the other seven seed classes.
- Module docstring documents the linkage-coverage caveat (some comparisons may lack `comparison_resolutions` rows until linkage pass matures — partial coverage is expected, not a defect).
- **`definition_version` bump (REQ-CB-FREEZE-003):** in the `EventClass(...)` literal at `polymarket_resolution_calibration.py:43-60`, bump `definition_version` from `1` to `2`. The semantic change (empty stub → real query) MUST propagate through `compute_run_id` so cached `backtest_runs` rows from the stub era do not get silently reused. `replay.py::_resolve_class_definition_versions` (lines 1382-1414) reads `pl_event_classes.definition_version` to seed `compute_run_id`'s `class_definition_versions` input, but ONLY if the operator both bumps the integer AND a `registry.sync_to_store` runs.
- Polarity correction: do NOT read `cr.outcome_observed` (already corrected). Use raw `pr.winning_outcome_label` ('yes'/'no') and apply `cr.polarity_at_comparison` ('direct'/'inverted') in pattern_library code (not SQL) per the matrix in T-CB-045.
**Verification:**
- Seeded fixture produces non-empty DataFrame; column set matches spec; `class_id` correctly derived from `comparisons` table.
- Regression test: `compute_run_id` returns a different SHA-256 hex digest before vs after the `definition_version` bump (holds all other params constant).
- mypy --strict on the upgraded file is clean (signature must remain `(conn) -> DataFrame`).
**Out of scope:** test fixture (T-CB-043).

### T-CB-043 — Add unit test for polymarket_resolution_calibration._occurrences upgrade
**Depends on:** T-CB-042.
**References:** REQ-CB-PL-001, design §4.2.

> **Scout amendment (2026-06-01, Phase 7):** test path is `tests/pattern_library/` (NOT `pattern_library/tests/` — the in-package directory does not exist; `pytest pattern_library/tests/` would no-op). Existing `populated_store` fixture at `tests/pattern_library/test_end_to_end_refresh.py:53-84` and the seed-class fixture at `tests/pattern_library/test_seed_classes.py:62-74` do NOT run mispricing_detector migrations, so `comparisons` and `comparison_resolutions` tables don't exist in those fixtures. After the upgrade, `_occurrences` would raise `Catalog Error` cascading into 5+ pattern_library/signal_scanner tests. **Decision: extend the fixtures with `run_pending_mispricing_detector_migrations` rather than adding defensive `try/except CatalogException` to `_occurrences`** — the fixture extension is more realistic and doesn't mask production schema bugs.

**Deliverables:**
- New test module at `tests/pattern_library/test_polymarket_resolution_calibration.py` (NOT `pattern_library/tests/...`).
- Seed `comparisons` row with `comparison_id`, `cycle_id`, `mapping_id`, `class_id`, `condition_id` (via `mispricing_detector.persistence.operations.persist_comparison`).
- Seed `comparison_resolutions` row matching by `comparison_id` with `polarity_at_comparison='direct'`, `resolution_outcome='yes'` (via `write_resolution_link`).
- Seed matching `polymarket_resolutions` row with same `condition_id`, `invalidated=FALSE`, `winning_outcome_label='yes'`, `superseded_at IS NULL`.
- Invoke `polymarket_resolution_calibration._occurrences(conn)` against the fixture (single-arg signature).
- Assert returned DataFrame contains exactly one row with the expected `class_id`, `condition_id`, `polarity_at_comparison`, `winning_outcome_label`, `occurrence_ts`.
- Assert no conditional empty-frame fallback path exists in production code (AST grep for `pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})` returns zero matches in `polymarket_resolution_calibration.py`).
- Add test for `invalidated=TRUE` row confirming filtering.
- **Fixture extension:** update `tests/pattern_library/test_end_to_end_refresh.py::populated_store` and `tests/pattern_library/test_seed_classes.py` shared fixture to ALSO run `run_pending_mispricing_migrations` (in addition to data_ingest, polymarket, pattern_library migrations). Confirms `comparisons` and `comparison_resolutions` tables exist for refresh to succeed.
**Verification:** test green; existing pattern_library tests still pass after fixture extension; AST grep confirms empty-frame fallback removed from production code.
**Out of scope:** circular-dependency check (T-CB-044).

### T-CB-044 — Validate no circular dependency in pattern_library meta-class
**Depends on:** T-CB-042.
**References:** REQ-CB-PL-002, design §3.15, §3.2.

> **Scout amendment (2026-06-01, Phase 7):** use the canonical 7-package list (per REQ-CB-PL-002 reconciliation): `pattern_library, signal_scanner, mispricing_detector, polymarket_connector, data_ingest, report_generator, position_engine`. The same list is referenced by T-CB-054.

**Deliverables:**
- Static import check across all 7 canonical packages: `grep -rE '^(from razor_rooster\.calibration_backtest|import razor_rooster\.calibration_backtest)' src/razor_rooster/{pattern_library,signal_scanner,mispricing_detector,polymarket_connector,data_ingest,report_generator,position_engine}/` returns zero matches.
- AST-level check on each package's `__init__.py` and submodule files: no `import calibration_backtest` or `from razor_rooster.calibration_backtest` statements anywhere.
- `_occurrences` does not invoke any `calibration_backtest.*` symbols (AST scan of `polymarket_resolution_calibration.py`).
- DuckDB connection (`conn`) is the sole external dependency passed to `_occurrences` (signature is `(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame`).
- Inline comments at the top of `polymarket_resolution_calibration.py` document REQ-CB-PL-002 compliance and the §3.2 reuse pattern.
- New test in `tests/pattern_library/test_no_back_edge.py` programmatically asserts no back-edge across all 7 packages.
**Verification:** static + AST checks pass; no back-edge introduced; pytest test green.
**Out of scope:** other meta-classes.

### T-CB-045 — Verify polarity-correction semantics in meta-class output
**Depends on:** T-CB-043.
**References:** REQ-CB-PL-001, REQ-CB-REPLAY-003, design §3.16, OQ-CB-005.

> **Scout amendment (2026-06-01, Phase 7):** explicit anti-pattern — **never read `cr.outcome_observed`** in the meta-class. That column is already polarity-adjusted at write time (per `mispricing_detector/models.py:148-149`), so reading it AND applying `cr.polarity_at_comparison` yields silent double-correction. Always re-derive observed from raw `pr.winning_outcome_label` + `cr.polarity_at_comparison`.

**Deliverables:**
- 4-cell parametrized test covering `polarity_at_comparison in {'direct','inverted'}` × `winning_outcome_label in {'yes','no'}`:
  - `polarity='direct'` + `'yes'` → `observed=1.0`
  - `polarity='direct'` + `'no'` → `observed=0.0`
  - `polarity='inverted'` + `'yes'` → `observed=0.0`
  - `polarity='inverted'` + `'no'` → `observed=1.0`
- The test asserts the helper that consumes `_occurrences` rows (or the meta-class's own polarity-correction step) computes `observed` correctly from the four input cells.
- `(model_p, observed)` pairs are computable from the returned row per §3.16.
- Compares against hand-computed reference data.
- **Anti-pattern guard:** AST grep on `pattern_library/classes/polymarket_resolution_calibration.py` confirms NO read of `cr.outcome_observed` — meta-class must re-derive from raw `winning_outcome_label`. (The column is allowed in fixtures and elsewhere; only the meta-class itself is forbidden from reading it.)
**Verification:** all four polarity × outcome combinations match expected `observed` values; AST grep returns zero `outcome_observed` references in the upgraded production file.
**Out of scope:** integration testing (T-CB-046).

### T-CB-046 — Integration test: meta-class occurrence count matches comparison_resolutions join
**Depends on:** T-CB-043.
**References:** REQ-CB-PL-001, design §4.2.

> **Scout amendment (2026-06-01, Phase 7):** disambiguate canonical join. There are two readers in the codebase: (a) `calibration_backtest/engines/polarity.py:62-72` uses **inner join** on `comparison_id` filtered by `pr.resolution_ts`; (b) `report_generator/engines/section_assemblers/calibration.py:81-89` uses **LEFT JOIN** on `comparisons` filtered by `r.linked_at`. **The canonical reader is (a) — `polarity.py`'s inner-join semantics.** The meta-class mirrors the polarity.py join exactly (post-Phase 7 amendment: three-table inner join, no time filter in SQL — refresh applies it downstream). The row-count assertion is also tightened: replace `floor(N/2) ± 1` with seeded data structured for an exact midpoint split.

**Deliverables:**
- Seed `comparisons` rows with N entries (use even N, e.g., N=10).
- Seed `comparison_resolutions` rows linking each comparison; spread `resolution_ts` evenly across a known range with an exact midpoint timestamp `t_mid`.
- Seed matching `polymarket_resolutions` rows with `invalidated=FALSE`.
- Invoke `_occurrences(conn)` (single-arg) and assert row count == N.
- Verify time filtering happens DOWNSTREAM (refresh applies `_count_in_window`); the meta-class itself returns ALL N rows.
- Apply `_count_in_window(occurrences_df, t_start, t_mid)` independently and assert exactly N/2 rows match (with even N and midpoint timestamp, the count is deterministic — no `±1` slop).
- Mix in 1 `invalidated=TRUE` row; assert `_occurrences(conn)` excludes it (returns N rows even with N+1 seeded total).
- **Partial-coverage test:** seed an extra `polymarket_resolutions` row WITHOUT a matching `comparison_resolutions` row; assert `_occurrences(conn)` excludes it (the inner join filters the unlinked resolution). Document this as the partial-coverage caveat from the T-CB-042 amendment.
- **Canonical-join cross-check:** assert that `_occurrences(conn)`'s row set equals (within ordering) the same query issued by `polarity.resolve`'s underlying SQL pattern (3-table inner join). Catches drift if either side mutates the join semantics later.
**Verification:** all assertions pass; row count is exact (no `±1` slop); partial-coverage caveat exercised.
**Out of scope:** lint gates (T-CB-047).

### T-CB-047 — Lint and type-check pattern_library upgrade
**Depends on:** T-CB-042, T-CB-043, T-CB-044, T-CB-045, T-CB-046.
**References:** REQ-CB-PL-001.

> **Scout amendment (2026-06-01, Phase 7):** test path is `tests/pattern_library/`, NOT `pattern_library/tests/`. Match CI's hard-gate verbatim.

**Deliverables:**
- `mypy --strict src/razor_rooster/pattern_library/classes/polymarket_resolution_calibration.py` clean.
- `ruff check src tests` clean (repo-wide).
- `ruff format --check src tests` clean.
- No new import statements introduce circular dependencies (T-CB-044 AST guard runs before lint).
- Function signature conforms to the `EventClass.occurrence_query` protocol: `(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame`. Adding parameters fails the type contract.
- `pytest tests/pattern_library/` green; no regressions in other classes.
- `pytest tests/calibration_backtest/` green (replay's `_resolve_class_definition_versions` must still find the bumped `definition_version=2` after the upgrade).
**Verification:** all gates green.
**Out of scope:** acceptance regression suite (T-CB-048).

### T-CB-048 — Run pattern_library test suite; verify no regressions
**Depends on:** T-CB-047.
**References:** REQ-CB-PL-001, REQ-CB-PL-002, design §4.2.

> **Scout amendment (2026-06-01, Phase 7):** test path is `tests/pattern_library/`. Coverage gate previously had no numeric threshold ("any non-zero coverage on the new lines"); tighten to >= 90% line coverage on the new `_occurrences` SQL block. Reconcile REQ-CB-PL-002 package list with DESIGN.md §4.2 — the canonical list is **7 packages**: `pattern_library, signal_scanner, mispricing_detector, polymarket_connector, data_ingest, report_generator, position_engine`.

**Deliverables:**
- Execute `pytest tests/pattern_library/` (unit + integration).
- All existing pattern_library tests continue to pass (no broken imports, no schema mismatches after the fixture extension).
- Static import-graph test (§4.2 "No circular dependency") passes for all **7 canonical packages** (NOT the 5-package list in REQ-CB-PL-002 — that's now reconciled).
- Meta-class is callable within the `pattern_library` refresh loop without errors.
- **Coverage gate:** `_occurrences` new SQL block must have >= 90% line coverage in `tests/pattern_library/` runs.
- CHANGELOG entry records REQ-CB-PL-001 and REQ-CB-PL-002 gate completion, citing both the Phase 3 and Phase 7 scout amendments.
- **Determinism re-run gate:** running the calibration_backtest test suite (`tests/calibration_backtest/`) before vs after the `definition_version` bump produces different `run_id` values for any `RunParameters` referencing `polymarket_resolution_calibration`. This proves REQ-CB-FREEZE-003 propagation.
**Verification:** suite green; coverage >= 90% on the new SQL; determinism re-run gate confirms hash difference.
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
