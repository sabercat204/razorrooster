# SIGNAL_SCANNER — Implementation Tasks

**Subsystem:** `signal_scanner`
**Codename:** The Nose
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `SIGNAL_SCANNER.md` v0.1.0
- Design: `SIGNAL_SCANNER_DESIGN.md` v0.1.0

**Hard prerequisites:**
- `data_ingest` Phase 0–4 tasks (T-001 through T-040) DONE.
- `pattern_library` Phase 0–6 tasks (T-PL-001 through T-PL-060) DONE — the public `library` facade must be available.

Task IDs prefixed `T-SCAN-NNN`.

---

## Phase 0 — Module Bootstrap

### T-SCAN-001 — Initialize signal_scanner module
**Depends on:** data_ingest T-002, pattern_library T-PL-001.
**References:** design §3.1.
**Deliverables:**
- `razor_rooster/signal_scanner/` directory tree per design §3.1.
- `cli.py` with `razor-rooster scan` click group.
- Mirror test layout under `tests/signal_scanner/`.
- `config/scanner.yaml` populated per design §3.7.
**Verification:** `razor-rooster scan --help` runs; pytest discovery clean.
**Out of scope:** any logic.

## Phase 1 — Schemas

### T-SCAN-010 — Scan tables and migration
**Depends on:** T-SCAN-001, data_ingest T-013.
**References:** design §3.3.
**Deliverables:**
- `persistence/schemas.py` with DDL for `scan_summaries`, `scan_records`, `scan_traces`.
- `persistence/migrations/m0001_signal_scanner_initial.py`.
- Registers with shared migrations framework.
**Verification:** schema migration applies; round-trip test on each table.
**Out of scope:** persistence helpers (T-SCAN-011).

### T-SCAN-011 — Persistence helpers
**Depends on:** T-SCAN-010, data_ingest T-014.
**References:** REQ-SCAN-PERSIST-001, REQ-SCAN-PERSIST-003.
**Deliverables:**
- `persistence/operations.py` with: `write_summary(scan_id, ...)`, `complete_summary(scan_id, ...)`, `persist_record(record)`, `persist_trace(trace)`, `query_recent_candidates(since)`, `prune_before(date)`.
- All operations idempotent at the (scan_id, class_id) primary key.
- Pruning requires explicit confirmation parameter.
**Verification:** unit tests per operation; idempotency confirmed via repeated insert.
**Out of scope:** scan logic.

## Phase 2 — Computation Engines

### T-SCAN-020 — Posterior computation
**Depends on:** T-SCAN-001.
**References:** OQ-SCAN-001 resolution, OQ-SCAN-002 resolution, REQ-SCAN-PROB-001, REQ-SCAN-PROB-002, design §3.5.
**Deliverables:**
- `engines/posterior.py` with `posterior_with_ci(base_rate, signatures, current_values, n_samples=1000) -> (point, (lower, upper))`.
- Co-occurrence correction reusing `pattern_library`'s cached lookup.
- Likelihood-ratio computation for fired vs. not-fired precursors.
- Sampling from beta posteriors for both prior and per-variable rates.
**Verification:**
- Unit test: synthetic prior + likelihood, verify posterior matches analytical Bayes update.
- Unit test: high-uncertainty inputs produce wider CI than low-uncertainty.
- Unit test: co-occurrence correction reduces joint signal when historical co-occurrence is high.
- Determinism test: with fixed seed, repeated calls produce identical output.
**Out of scope:** trace generation (T-SCAN-021).

### T-SCAN-021 — Reasoning trace builder and renderer
**Depends on:** T-SCAN-020.
**References:** REQ-SCAN-TRACE-001..003, design §3.6.
**Deliverables:**
- `engines/trace.py` with `build_trace(class, base_rate, signatures, current_values, posterior_result, warnings) -> dict`.
- `render_trace_text(trace) -> str` produces human-readable output.
- Trace dict format exactly matches design §3.6 example.
**Verification:**
- Unit test: build_trace populates all expected fields.
- Unit test: rendered output contains key data points.
- JSON-roundtrip test: build_trace → json.dumps → json.loads → equivalent dict.
**Out of scope:** Polymarket comparison (handled in `mispricing_detector`).

### T-SCAN-022 — Candidate identification
**Depends on:** T-SCAN-001.
**References:** REQ-SCAN-CAND-001..004, OQ-SCAN-003 resolution, design §3.7.
**Deliverables:**
- `engines/candidates.py` with `identify_candidate(sector, log_odds_shift, signature_confidence, source_stale, config) -> tuple[bool, direction]`.
- Returns `(False, None)` for low-confidence or (when configured) stale-source cases.
- Direction tag: `'elevated'` if shift > 0, `'depressed'` if shift < 0.
**Verification:**
- Unit test per acceptance scenario: large positive shift → candidate elevated; large negative → candidate depressed; small shift → not candidate; large shift but low confidence → not candidate; large shift but stale source → not candidate (default config).
- Per-sector threshold test confirms different sectors use different thresholds.
**Out of scope:** persistence — that's caller's concern.

## Phase 3 — Scan Orchestration

### T-SCAN-030 — Class evaluator
**Depends on:** T-SCAN-020, T-SCAN-021, T-SCAN-022, T-SCAN-011.
**References:** REQ-SCAN-EXEC-003, REQ-SCAN-PROV-001..003, design §3.4 evaluate_class.
**Deliverables:**
- `engines/scanner.py` `evaluate_class(scan_id, cls, library_version, strict) -> tuple[ScanRecord, Trace]`.
- Calls library facade for base_rate and signatures.
- Detects definition_drift; flags or aborts per `strict`.
- Detects missing/stale data; produces base-rate-equal record with no_update_applied=true.
- Catches exceptions; returns error record (no Trace dict mutation).
**Verification:**
- Unit test: synthetic class evaluates to expected record + trace.
- Unit test: forced exception produces error record without crashing.
- Unit test: missing data produces no_update record; trace explains why.
- Unit test: definition_drift sets warning flag.
- Unit test: strict + drift raises StrictDriftAbort.
**Out of scope:** orchestration (T-SCAN-031).

### T-SCAN-031 — Scan orchestrator
**Depends on:** T-SCAN-030.
**References:** REQ-SCAN-EXEC-001..004, design §3.4 run_scan.
**Deliverables:**
- `engines/scanner.py` `run_scan(class_id=None, strict=False) -> ScanReport`.
- ThreadPoolExecutor with `max_workers` from config.
- Library version pinned at scan start; mid-scan library-version change triggers abort.
- Per-class success/failure aggregation into summary.
- Structured JSON log emitted per REQ-SCAN-LOG-001.
**Verification:**
- Integration test: scan against synthetic library + ingest fixtures, all classes evaluated.
- Failure-isolation test: one class throws, others succeed, summary reflects partial.
- Mid-scan library bump test: triggers documented abort error.
**Out of scope:** CLI (T-SCAN-040).

## Phase 4 — CLI

### T-SCAN-040 — Scan CLI
**Depends on:** T-SCAN-031, T-SCAN-011.
**References:** design §3.8.
**Deliverables:**
- `razor-rooster scan run [--class <id>] [--strict]`.
- `razor-rooster scan show <scan_id>` — prints summary + per-class records.
- `razor-rooster scan show-trace <scan_id> <class_id>` — renders trace via T-SCAN-021.
- `razor-rooster scan list-candidates [--since <iso>]`.
- `razor-rooster scan prune --before <iso> --confirm`.
**Verification:** CLI integration tests for each subcommand against populated DB.
**Out of scope:** TUI / colored output.

## Phase 5 — Acceptance

### T-SCAN-080 — End-to-end integration test
**Depends on:** T-SCAN-040.
**References:** acceptance criteria in SIGNAL_SCANNER.md §8.
**Deliverables:**
- Integration test: synthetic library + ingest fixtures covering all eight seed classes, full scan executes, expected records produced, candidates identified per the threshold config.
- Failure isolation, library drift, source stale, idempotent re-scan all covered.
**Verification:** integration test passes in `make test`.
**Out of scope:** real-network testing.

### T-SCAN-081 — First scan on operator hardware
**Depends on:** T-SCAN-080, pattern_library T-PL-081.
**References:** NFR-SCAN-PERF-001, NFR-SCAN-DISK-001, OQ-SCAN-003, DEFER-SCAN-001.
**Deliverables:**
- Operator runs `razor-rooster scan run` against the populated and refreshed system.
- Records: total duration, per-class duration, candidate count by sector, posterior distributions, warning flag rates.
- Updates DEFER-SCAN-001 with measured numbers.
- If candidate rate per sector is anomalous (~0% or ~100%), flag for threshold revision.
**Verification:** measurements recorded under §X-Measurements (added by operator).
**Out of scope:** automatic threshold tuning.

### T-SCAN-082 — Operator README updates
**Depends on:** T-SCAN-081.
**References:** design §5.
**Deliverables:**
- README updated with Signal Scanner section: daily-cadence setup, investigating candidates, trace interpretation.
- `docs/scanner.md` (or similar) explaining the candidate threshold logic and configuration knobs.
**Verification:** new operator can follow README from clean machine to working daily scan.
**Out of scope:** developer architecture docs.

## Dependency Summary (Critical Path)

    T-SCAN-001 → T-SCAN-010 → T-SCAN-011 → T-SCAN-020 → T-SCAN-021 → T-SCAN-022 → T-SCAN-030 → T-SCAN-031 → T-SCAN-040 → T-SCAN-080 → T-SCAN-081 → T-SCAN-082

Mostly linear; not a lot of fan-out.

## Tracking

- **T-SCAN-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.31.0):

- **T-SCAN-001** — Initialize signal_scanner module — `DONE` — 2026-05-15
- **T-SCAN-010** — Scan tables and migration — `DONE` — 2026-05-15
- **T-SCAN-011** — Persistence helpers — `DONE` — 2026-05-15
- **T-SCAN-020** — Posterior computation — `DONE` — 2026-05-15
- **T-SCAN-021** — Reasoning trace builder and renderer — `DONE` — 2026-05-15
- **T-SCAN-022** — Candidate identification — `DONE` — 2026-05-15
- **T-SCAN-030** — Class evaluator — `DONE` — 2026-05-15
- **T-SCAN-031** — Scan orchestrator — `DONE` — 2026-05-15
- **T-SCAN-040** — Scan CLI — `DONE` — 2026-05-15
- **T-SCAN-080** — End-to-end integration test — `DONE` — 2026-05-15
- **T-SCAN-081** — First scan on operator hardware — `OPERATOR_BLOCKED` — depends on pattern_library T-PL-081 + data_ingest T-072 backfill on operator hardware
- **T-SCAN-082** — Operator README updates — `DONE` — 2026-05-15

All Phases 0-5 fully complete. Lifecycle: PRODUCTION_READY.
The single OPERATOR_BLOCKED task (T-SCAN-081) parallels the same
blocker pattern in data_ingest (T-072 / T-073), polymarket_connector
(T-PMC-072 / T-PMC-073), and pattern_library (T-PL-081) — first scan
against real upstream data lands when the operator runs the backfill
and library refresh on their EliteBook G8 hardware.

## References

- Requirements: `SIGNAL_SCANNER.md` v0.1.0
- Design: `SIGNAL_SCANNER_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md`
- `pattern_library` and `data_ingest` specs.
