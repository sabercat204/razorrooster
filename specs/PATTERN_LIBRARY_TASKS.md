# PATTERN_LIBRARY — Implementation Tasks

**Subsystem:** `pattern_library`
**Codename:** The Bone Pile
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `PATTERN_LIBRARY.md` v0.1.0
- Design: `PATTERN_LIBRARY_DESIGN.md` v0.1.0

**Hard prerequisites:**
- `data_ingest` Phase 0–4 tasks (T-001 through T-040) must be DONE — the library reuses DuckDBStore, staging-merge, migrations, and structured logging.
- For seed classes that depend on Polymarket resolutions (`polymarket_resolution_calibration`), `polymarket_connector` T-PMC-042 must be DONE.
- For meaningful refresh output, `data_ingest` T-072 (backfill) must be done — without populated source data, precursor signatures and analogue spaces will be empty.

Task IDs prefixed `T-PL-NNN`.

---

## Phase 0 — Module Bootstrap

### T-PL-001 — Initialize pattern_library module
**Depends on:** data_ingest T-002.
**References:** design §3.1.
**Deliverables:**
- `razor_rooster/pattern_library/` directory tree per design §3.1.
- Empty `__init__.py` in each subdirectory.
- `cli.py` with a `click` group `razor-rooster pattern-library` showing subcommands.
- Mirror test layout under `tests/pattern_library/`.
- `version.py` with `LIBRARY_VERSION = 1`.
**Verification:** `razor-rooster pattern-library --help` runs; pytest discovers and runs zero tests.
**Out of scope:** any logic.

## Phase 1 — Models and Schemas

### T-PL-010 — Core dataclasses
**Depends on:** T-PL-001.
**References:** design §3.3.
**Deliverables:**
- `models/event_class.py` with `EventClass`, `Sector` enum, `BaselineStrategy` enum, `Normalization` enum, `ThresholdMethod` enum.
- `models/outcomes.py` with `OutcomeRecord`.
- `models/base_rate.py` with `BaseRateResult`.
- `models/signature.py` with `PrecursorVariable`, `PrecursorSignature`, `SignatureResult`.
- `models/analogue.py` with `AnalogueFeature`, `AnalogueFeatureSpace`, `AnalogueResults`, `AnalogueMatch`.
- `models/calibration.py` with `CalibrationOutput`, `ReliabilityBin`.
- All dataclasses are frozen and use type annotations strictly.
**Verification:** unit tests confirm validation, default values, and `__post_init__` behavior for each.
**Out of scope:** persistence, computation engines.

### T-PL-011 — Pattern library schemas and migration
**Depends on:** T-PL-010, data_ingest T-013.
**References:** design §3.4.
**Deliverables:**
- `persistence/schemas.py` with DDL strings for all eight `pl_*` tables (event_classes, outcomes, base_rates, precursor_signatures, analogue_features, calibration, library_versions, refresh_log).
- `persistence/migrations/m0001_pattern_library_initial.py` applying all DDL.
- Registers with the data_ingest migrations framework.
**Verification:** schema migration on fresh DB applies all tables; round-trip tests insert and query a synthetic row per table; indexes verified via `EXPLAIN`.
**Out of scope:** reading/writing through these tables (T-PL-012 wraps that).

### T-PL-012 — Persistence helpers
**Depends on:** T-PL-011, data_ingest T-014 (staging-merge), data_ingest T-015 (provenance).
**References:** REQ-PL-PROV-001, REQ-PL-PROV-002, design §3.4.
**Deliverables:**
- `persistence/operations.py` with helpers: `upsert_event_class()`, `upsert_outcomes()`, `upsert_base_rate()`, `upsert_signature()`, `upsert_analogue_features()`, `upsert_calibration()`, `record_library_version_bump()`, `record_refresh()`.
- All upserts use staging-merge.
- Each helper sets the relevant `library_version` and `definition_version` columns on insert.
**Verification:** unit test per helper; round-trip query works for each.
**Out of scope:** computation logic — these are storage primitives.

## Phase 2 — Library Versioning

### T-PL-020 — Library version detection
**Depends on:** T-PL-012.
**References:** REQ-PL-VER-001, REQ-PL-VER-002, REQ-PL-VER-003, design §3.6.
**Deliverables:**
- `version.py` extended with `current_version()`, `bump_for_reason(reason, affected_class_ids)`.
- Auto-bump logic invoked at refresh start: detects class registry changes and triggers a bump.
- A pre-commit hook script (or CI check) verifies that any change under `engines/`, `models/`, `transforms.py` requires a manual `LIBRARY_VERSION` bump.
**Verification:**
- Unit test: registry change detected, bump triggered.
- Unit test: unchanged registry, no bump.
- Unit test: forced bump records expected reason.
- The pre-commit/CI check fires on a synthetic engines/ change without version bump.
**Out of scope:** handling deletions of classes (we keep historic class entries; they're flagged removed but not pruned).

## Phase 3 — Class Registry

### T-PL-030 — Class registration mechanism
**Depends on:** T-PL-010, T-PL-012.
**References:** REQ-PL-CLASS-001, REQ-PL-CLASS-002, REQ-PL-CLASS-003, REQ-PL-CLASS-004, design §3.3.
**Deliverables:**
- `registry.py` with `register(cls: EventClass)`, `get_all()`, `get(class_id)`.
- Auto-discovery: on first access, iterates modules under `pattern_library/classes/` and imports them, expecting each to expose a module-level `CLASS = EventClass(...)`.
- Validation runs on registration: queries are callable, sectors are valid enum values, definition_version is a positive integer, refractory_months is positive.
- Invalid classes raise `ClassValidationError` with details.
- Persistent registration: registry sync writes to `pl_event_classes`.
**Verification:**
- Unit test: register synthetic valid class, confirm appears in `get_all()`.
- Unit test: register synthetic invalid class, confirm rejection with informative error.
- Unit test: registry sync writes expected rows.
**Out of scope:** evaluating classes (that's per-engine).

### T-PL-031 — Validate CLI subcommand
**Depends on:** T-PL-030.
**References:** REQ-PL-CLASS-004.
**Deliverables:**
- `razor-rooster pattern-library validate <class_id>` runs full validation and reports issues without persisting.
- `razor-rooster pattern-library list [--sector <s>]` lists registered classes.
- `razor-rooster pattern-library show <class_id>` prints the class's metadata, current outputs, and calibration summary.
**Verification:** CLI integration test runs each subcommand against a populated registry.
**Out of scope:** refresh, eval (later phases).

## Phase 4 — Computation Engines

### T-PL-040 — Transforms module
**Depends on:** T-PL-001.
**References:** OQ-PL-004 resolution, design §3.1.
**Deliverables:**
- `transforms.py` with `zscore(series)`, `percentile_rank(series, window)`, `lag(series, n)`, `rolling_mean(series, window)`.
- Each transform handles edge cases (empty series, all-NaN, insufficient window).
- All transforms are pure functions returning a new `pd.Series`.
**Verification:** unit tests with known inputs and outputs; edge case tests (empty, all-NaN, single-point window).
**Out of scope:** transforms tied to specific event classes — those live in class definitions.

### T-PL-041 — Base rate engine
**Depends on:** T-PL-012, T-PL-030.
**References:** REQ-PL-BR-001..005, design §3.5.
**Deliverables:**
- `engines/base_rates.py` with `compute_base_rate(conn, cls, window=None) -> BaseRateResult`.
- Implements Jeffreys-prior (or per-class override) credible interval computation.
- Sets `low_sample_warning` for n < 5.
- Sets `source_stale_warning` based on `data_ingest` freshness view of underlying sources.
**Verification:**
- Unit test: synthetic class with known occurrence count produces expected rate and CI.
- Unit test: low-sample case (n=2) produces wide CI and warning flag.
- Unit test: large-sample case (n=200) produces tight CI.
- Unit test: per-class prior override is respected and logged.
**Out of scope:** persistence (T-PL-046 ties everything together).

### T-PL-042 — Threshold-discovery primitives
**Depends on:** T-PL-040.
**References:** OQ-PL-002 resolution, design §3.5.
**Deliverables:**
- `engines/thresholds.py` with `youden_j(scores, labels)`, `f1_threshold(scores, labels)`, `quantile_95(baseline_scores)`, `manual(value)`.
- Each returns `(threshold, hit_rate, false_positive_rate)` at the chosen point.
**Verification:** unit tests against synthetic distributions with known optimal thresholds for each method.
**Out of scope:** signature integration (T-PL-043).

### T-PL-043 — Signature engine
**Depends on:** T-PL-042.
**References:** REQ-PL-SIG-001..005, design §3.5.
**Deliverables:**
- `engines/signatures.py` with `compute_signature(conn, cls) -> list[SignatureResult]`.
- Implements baseline sampling per `cls.baseline_strategy` with refractory-zone exclusion (OQ-PL-003 resolution).
- Per-precursor: extract pre-event windows, compute distributions, pick threshold via T-PL-042.
- Confidence score combining sample size, Cohen's d (effect size), bootstrap-based threshold uncertainty.
**Verification:**
- Unit test: synthetic class with strong signal recovers high hit rate and high confidence.
- Unit test: synthetic noise input produces low hit rate and low-confidence flag.
- Unit test: refractory zone correctly excludes pre-event windows from baseline.
**Out of scope:** multi-variable combination (T-PL-044).

### T-PL-044 — Multi-variable signature combination
**Depends on:** T-PL-043.
**References:** REQ-PL-SIG-004, design §3.5, DEFER-PL-003.
**Deliverables:**
- `engines/signatures.py` extended with `combine_variables(signatures, current_values) -> CombinedScore`.
- Implements geometric-mean-with-co-occurrence correction per design §3.5.
- Co-occurrence lookup table built during signature computation.
**Verification:**
- Unit test: two precursors that co-occur historically produce a calibrated joint signal close to historical co-occurrence rate.
- Unit test: two precursors that never co-occurred fall back to geometric mean.
**Out of scope:** alternative combination methods.

### T-PL-045 — Analogue engine
**Depends on:** T-PL-040, T-PL-012.
**References:** REQ-PL-AN-001..005, design §3.5.
**Deliverables:**
- `engines/analogues.py` with:
  - `populate_feature_space(conn, cls) -> AnalogueFeatureSpace` — computes feature vectors for events + baselines, normalizes, persists.
  - `find_analogues(conn, class_id, current_features, k) -> AnalogueResults` — loads space, normalizes current features using saved population stats, computes weighted-Euclidean distance, returns top-k.
- Per-class distance metric override supported.
**Verification:**
- Unit test: synthetic two-cluster population, query near each cluster returns the right cluster's neighbors.
- Unit test: per-class custom metric (Mahalanobis) produces ranking different from default Euclidean.
- Performance test: 10,000-point space with 10-feature vectors returns top-10 in under 2 seconds (NFR-PL-PERF-003).
**Out of scope:** ML-based similarity (deferred to v2).

### T-PL-046 — Calibration engine
**Depends on:** T-PL-041, T-PL-043, T-PL-044.
**References:** REQ-PL-SEED-003, OQ-PL-005 resolution, design §3.5.
**Deliverables:**
- `engines/calibration.py` with `compute_calibration(conn, cls) -> CalibrationOutput` implementing leave-one-out evaluation per design §3.5.
- Brier score, reliability diagram (10 bins, configurable), full prediction trace.
- Trace written to `data/library/calibration/<class_id>.json`.
- Skipped (returns insufficient-data result) for classes with <10 occurrences.
**Verification:**
- Unit test: well-calibrated synthetic predictions produce low Brier score.
- Unit test: poorly-calibrated synthetic predictions produce high Brier score.
- Unit test: insufficient-data case skips gracefully.
- File-output test confirms JSON written with expected schema.
**Out of scope:** continuous-magnitude calibration (binary v1 only).

## Phase 5 — Refresh Orchestration

### T-PL-050 — Refresh runner
**Depends on:** T-PL-020, T-PL-030, T-PL-041, T-PL-043, T-PL-044, T-PL-045, T-PL-046.
**References:** REQ-PL-REFRESH-001, REQ-PL-REFRESH-002, REQ-PL-REFRESH-003, design §3.7.
**Deliverables:**
- `engines/refresh.py` with `run_refresh(class_id=None, force=False) -> RefreshReport`.
- File-based lock at `data/library/.refresh.lock`.
- Bounded concurrency (default `max_workers=2`).
- Per-class isolation: failure on one class does not stop others.
- Calls all engines in dependency order (outcomes → base rate → signatures → analogues → calibration).
- Writes structured log per REQ-PL-LOG-001.
**Verification:**
- Integration test: refresh against synthetic data populates all tables for all classes.
- Failure-isolation test: one class throws, others complete.
- Lock test: concurrent refresh attempts fail with clear error.
**Out of scope:** automatic scheduling.

### T-PL-051 — Refresh and eval CLI
**Depends on:** T-PL-050.
**References:** design §3.8.
**Deliverables:**
- `razor-rooster pattern-library refresh [--class <id>] [--force]`.
- `razor-rooster pattern-library eval <class_id> [--window-start ...] [--window-end ...]` — ad-hoc evaluation without persistence.
- Both invoke geo gate from `polymarket_connector` only when class includes Polymarket-derived features (e.g. the calibration meta-class).
**Verification:** CLI integration tests for each subcommand.
**Out of scope:** GUI; non-interactive flag for `force`.

## Phase 6 — Public API

### T-PL-060 — Library facade
**Depends on:** T-PL-050.
**References:** design §5.4.
**Deliverables:**
- `razor_rooster.pattern_library.library` module exposing the read-only public API:
  - `current_version() -> int`
  - `base_rate(class_id, window=None) -> BaseRateResult`
  - `signature(class_id) -> list[SignatureResult]`
  - `find_analogues(class_id, current_features, k=10) -> AnalogueResults`
  - `calibration(class_id) -> CalibrationOutput | None`
  - `list_classes(sector=None) -> list[EventClassSummary]`
- Each function reads from persisted tables (no on-the-fly computation).
- Returns versioned outputs so consumers can detect mismatches.
**Verification:** unit test confirms all functions return expected dataclasses with `library_version` field populated.
**Out of scope:** write operations; downstream-friendly caching.

## Phase 7 — Seed Library

The eight seed classes from design §2 OQ-PL-008. Each task creates one class module + a documentation file under `specs/seed_event_classes/<class_id>.md`. Tasks can be done in parallel by a single developer.

### T-PL-070 — Seed: pheic_declaration_12mo
**Depends on:** T-PL-060.
**References:** design §2 OQ-PL-008, REQ-PL-SEED-001..003.
**Deliverables:**
- `classes/pheic_declaration_12mo.py` defining the EventClass.
- `specs/seed_event_classes/pheic_declaration_12mo.md` with rationale, sources, predicate, precursors, analogue features, known limitations.
- Refresh against synthetic test fixtures populates outputs.
**Verification:**
- Validate-CLI passes.
- Refresh-CLI populates tables.
- Documentation file exists and is non-trivial.
**Out of scope:** production-quality predicate tuning — that happens after T-PL-080 measurements.

### T-PL-071 — Seed: gdelt_conflict_intensification
**Depends on:** T-PL-060.
**References:** same as T-PL-070.
**Deliverables:** same pattern as T-PL-070 for `gdelt_conflict_intensification`.
**Verification:** same.
**Out of scope:** same.

### T-PL-072 — Seed: final_rule_within_12mo
**Depends on:** T-PL-060.
**References:** same as T-PL-070.
**Deliverables:** same pattern for `final_rule_within_12mo`. Predicate uses `data_ingest`'s `document_docket` schema joining proposed and final rules on `docket_id`.
**Verification:** same.
**Out of scope:** same.

### T-PL-073 — Seed: opec_unscheduled_cut
**Depends on:** T-PL-060.
**References:** same as T-PL-070.
**Deliverables:** same pattern. Precursors include FRED oil price series, EIA stock levels.
**Verification:** same.
**Out of scope:** same.

### T-PL-074 — Seed: enso_neutral_to_elnino
**Depends on:** T-PL-060.
**References:** same as T-PL-070.
**Deliverables:** same pattern. NOAA ENSO indices used for both occurrence detection and precursor computation.
**Verification:** same.
**Out of scope:** same.

### T-PL-075 — Seed: eia_grid_reliability_event
**Depends on:** T-PL-060.
**References:** same as T-PL-070.
**Deliverables:** same pattern. EIA grid data from `data_ingest`.
**Verification:** same.
**Out of scope:** same.

### T-PL-076 — Seed: multi_signal_geopolitical_alert
**Depends on:** T-PL-060, T-PL-071, T-PL-072.
**References:** same as T-PL-070.
**Deliverables:** same pattern. Combines ACLED + GDELT + Federal Register signals to exercise multi-precursor combination logic (REQ-PL-SIG-004).
**Verification:** same.
**Out of scope:** same.

### T-PL-077 — Seed: polymarket_resolution_calibration
**Depends on:** T-PL-060, polymarket_connector T-PMC-042.
**References:** same as T-PL-070, plus OT-006.
**Deliverables:**
- `classes/polymarket_resolution_calibration.py` defining the meta-class.
- Predicate joins prior pattern_library predictions (when downstream subsystems begin logging them) with `polymarket_resolutions`.
- Documentation explicitly notes this class will produce empty outputs until downstream subsystems are populating prediction logs — it's the scaffolding for OT-006, not a full calibration backtest yet.
**Verification:** validate passes; refresh against test fixtures with synthetic predictions and resolutions populates expected calibration.
**Out of scope:** the prediction logging itself (lives in `mispricing_detector` and `report_generator` later).

## Phase 8 — Acceptance and Operational Readiness

### T-PL-080 — End-to-end integration test
**Depends on:** all T-PL-070..T-PL-077.
**References:** acceptance criteria in PATTERN_LIBRARY.md §8.
**Deliverables:**
- Integration test running full refresh against a comprehensive test fixture (synthetic `data_ingest` + `polymarket_*` data covering all seed classes).
- Verifies all eight seed classes evaluate, populate tables, and (where applicable) produce calibration files.
- Failure-isolation scenario: one class's predicate intentionally throws; others succeed.
- Library version mismatch scenario: simulate consumer holding an older version, confirm public API tags outputs correctly.
**Verification:** integration test passes as part of `make test`.
**Out of scope:** real-network testing.

### T-PL-081 — First refresh on operator hardware
**Depends on:** T-PL-080, data_ingest T-072.
**References:** NFR-PL-PERF-001, NFR-PL-DISK-001, DEFER-PL-001..004.
**Deliverables:**
- Operator runs `razor-rooster pattern-library refresh` against the populated `data_ingest` corpus.
- Records: total duration, per-class duration, base rate values, sample sizes, calibration metrics, total disk usage of `pl_*` tables.
- Updates DEFER-PL-001..004 with measured numbers.
- Findings inform v1.1 priorities (e.g. classes that consistently low-confidence may need redefinition; classes with >2-second analogue lookups may need feature-space reduction).
**Verification:** measurements recorded in this document under §X-Measurements (added by operator).
**Out of scope:** retuning seed classes based on measurements — that's a follow-on work item.

### T-PL-082 — Operator README updates
**Depends on:** T-PL-081.
**References:** design §5.
**Deliverables:**
- `README.md` updated with a Pattern Library section: refresh workflow, adding a class, modifying a class, reading library outputs.
- `docs/pattern_library.md` (or similar) explaining the eight seed classes and the documentation conventions for future classes.
**Verification:** a new operator could follow the README from a clean machine to a working refresh without code-reading.
**Out of scope:** developer architecture docs.

## Dependency Summary (Critical Path)

    T-PL-001 → T-PL-010 → T-PL-011 → T-PL-012
                                       ↓
    T-PL-020 → T-PL-030 → T-PL-031
                            ↓
    T-PL-040 → T-PL-041 → T-PL-042 → T-PL-043 → T-PL-044 → T-PL-045 → T-PL-046
                                                                          ↓
                                                          T-PL-050 → T-PL-051 → T-PL-060
                                                                                  ↓
                                              [T-PL-070..T-PL-077 in parallel]
                                                                                  ↓
                                                              T-PL-080 → T-PL-081 → T-PL-082

Phase 7 (seed classes) parallelizes after Phase 6. Phase 8 is the gate.

## Tracking

- **T-PL-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.30.0):

- **T-PL-001** — Initialize pattern_library module — `DONE` — 2026-05-15
- **T-PL-010** — Core dataclasses — `DONE` — 2026-05-15
- **T-PL-011** — Pattern library schemas and migration — `DONE` — 2026-05-15
- **T-PL-012** — Persistence helpers — `DONE` — 2026-05-15
- **T-PL-020** — Library version detection — `DONE` — 2026-05-15
- **T-PL-030** — Class registration mechanism — `DONE` — 2026-05-15
- **T-PL-031** — Validate / list / show CLI subcommands — `DONE` — 2026-05-15
- **T-PL-040** — Transforms module — `DONE` — 2026-05-15
- **T-PL-041** — Base rate engine — `DONE` — 2026-05-15
- **T-PL-042** — Threshold-discovery primitives — `DONE` — 2026-05-15
- **T-PL-043** — Signature engine — `DONE` — 2026-05-15
- **T-PL-044** — Multi-variable signature combination — `DONE` — 2026-05-15
- **T-PL-045** — Analogue engine — `DONE` — 2026-05-15
- **T-PL-046** — Calibration engine — `DONE` — 2026-05-15
- **T-PL-050** — Refresh runner — `DONE` — 2026-05-15
- **T-PL-051** — Refresh and eval CLI — `DONE` — 2026-05-15
- **T-PL-060** — Library facade — `DONE` — 2026-05-15
- **T-PL-070** — Seed: pheic_declaration_12mo — `DONE` — 2026-05-15
- **T-PL-071** — Seed: gdelt_conflict_intensification — `DONE` — 2026-05-15
- **T-PL-072** — Seed: final_rule_within_12mo — `DONE` — 2026-05-15
- **T-PL-073** — Seed: opec_unscheduled_cut — `DONE` — 2026-05-15
- **T-PL-074** — Seed: enso_neutral_to_elnino — `DONE` — 2026-05-15
- **T-PL-075** — Seed: eia_grid_reliability_event — `DONE` — 2026-05-15
- **T-PL-076** — Seed: multi_signal_geopolitical_alert — `DONE` — 2026-05-15
- **T-PL-077** — Seed: polymarket_resolution_calibration — `DONE` — 2026-05-15
- **T-PL-080** — End-to-end integration test — `DONE` — 2026-05-15
- **T-PL-081** — First refresh on operator hardware — `OPERATOR_BLOCKED` — depends on data_ingest T-072 backfill on operator hardware
- **T-PL-082** — Operator README updates — `DONE` — 2026-05-15

Phases 0, 1, 2, 3, 4, 5, 6, 7, 8 fully complete. Lifecycle: PRODUCTION_READY.
The single OPERATOR_BLOCKED task (T-PL-081) parallels the same blocker
in data_ingest (T-072 / T-073) and polymarket_connector (T-PMC-072 /
T-PMC-073) — first run against real upstream data lands when the
operator runs the backfill on their EliteBook G8 hardware.

## References

- Requirements: `PATTERN_LIBRARY.md` v0.1.0
- Design: `PATTERN_LIBRARY_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md` v0.7.0
- `data_ingest` specs (Requirements/Design/Tasks v0.1.0).
- `polymarket_connector` specs (Requirements/Design/Tasks v0.1.0).
