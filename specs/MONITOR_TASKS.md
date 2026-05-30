# MONITOR — Implementation Tasks

**Subsystem:** `monitor`
**Codename:** The Comb
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `MONITOR.md` v0.1.0
- Design: `MONITOR_DESIGN.md` v0.1.0

**Hard prerequisites:**
- All upstream subsystems' implementation phases through their cycle-running tasks: `signal_scanner` T-SCAN-031, `polymarket_connector` T-PMC-061, `mispricing_detector` T-MD-042, `position_engine` T-PE-061.

Task IDs prefixed `T-MON-NNN`.

---

## Phase 0 — Bootstrap

### T-MON-001 — Module init
**Depends on:** position_engine T-PE-001.
**References:** design §3.1.
**Deliverables:** module tree, CLI click group, `config/monitor.yaml`, test layout.
**Verification:** `--help` runs; pytest discovery clean.
**Out of scope:** logic.

## Phase 1 — Schemas

### T-MON-010 — Monitor schemas + migration
**Depends on:** T-MON-001, data_ingest T-013.
**References:** design §3.3.
**Deliverables:** DDL for `monitor_cycles`, `follow_ups`, `follow_up_notes`. m0001 migration.
**Verification:** schema applies; round-trip per table.
**Out of scope:** persistence helpers.

### T-MON-011 — Persistence helpers
**Depends on:** T-MON-010, data_ingest T-014.
**References:** REQ-MON-PERSIST-001..003.
**Deliverables:** `persistence/operations.py` with: `write_cycle`, `complete_cycle`, `persist_follow_up`, `persist_follow_up_error`, `add_note`, `query_alerts`, `query_trajectory`.
**Verification:** unit tests per helper.
**Out of scope:** business logic.

## Phase 2 — Detection Engines

### T-MON-020 — Change detector
**Depends on:** T-MON-001.
**References:** REQ-MON-DETECT-001..004, OQ-MON-001 resolution, design §3.5.
**Deliverables:** `engines/change_detector.py` with `compute_model_shift`, `compute_market_shift`, `snapshot_precursors`, `classify_band`. Per-sector threshold reading from config.
**Verification:** unit tests per band; per-sector threshold-application test.
**Out of scope:** orchestration.

### T-MON-021 — Invalidation evaluator
**Depends on:** T-MON-001.
**References:** REQ-MON-DETECT-005, design §3.6.
**Deliverables:** `engines/invalidation_evaluator.py` with `evaluate_invalidations(analysis, current_scan, current_price)`. Handles three criterion types from `position_engine` plus `cannot_evaluate` fallback.
**Verification:** unit tests per criterion type; fallback test with synthetic unknown-type criterion.
**Out of scope:** orchestration.

### T-MON-022 — Alert ranker
**Depends on:** T-MON-001.
**References:** REQ-MON-ALERT-001, REQ-MON-ALERT-002, design §3.7.
**Deliverables:** `engines/alert_ranker.py` with `compute_alert_tiers(...)` returning (primary, all_applicable).
**Verification:** unit tests for each combination; ordering test confirms tier priority.
**Out of scope:** integration.

### T-MON-023 — Reasoning text builder
**Depends on:** T-MON-020, T-MON-021.
**References:** REQ-MON-REVIEW-002, design §3.8.
**Deliverables:** `engines/reasoning.py` with `build(...)` template-driven text generator.
**Verification:** unit test produces expected text from synthetic inputs; deterministic test.
**Out of scope:** integration.

## Phase 3 — Cycle Orchestration

### T-MON-030 — Per-analysis evaluator
**Depends on:** T-MON-020..T-MON-023, T-MON-011.
**References:** REQ-MON-EXEC-002, REQ-MON-EXEC-003, REQ-MON-DETECT-006, design §3.4 evaluate_analysis.
**Deliverables:** `engines/comb.py` `evaluate_analysis(cycle_id, analysis) -> FollowUp`.
- Resolution check first; short-circuits to resolution_follow_up.
- Otherwise gathers all current snapshots, runs detectors, builds follow-up.
- Triggers `position_engine.expire_watch` on resolution detection.
**Verification:**
- Unit test: synthetic non-resolved analysis produces expected FollowUp.
- Unit test: synthetic resolved analysis produces resolution-tagged FollowUp.
- Integration test: resolution detection fires expiration call.
**Out of scope:** orchestrator.

### T-MON-031 — Cycle runner
**Depends on:** T-MON-030.
**References:** REQ-MON-EXEC-001, REQ-MON-LOG-001, design §3.4 run_cycle.
**Deliverables:** `engines/comb.py` `run_cycle()` over all watched and acted-on analyses with per-analysis isolation.
**Verification:**
- Integration test: cycle against synthetic state with multiple watched analyses produces expected follow-ups.
- Failure-isolation test: one analysis throws, others complete.
**Out of scope:** CLI.

## Phase 4 — CLI

### T-MON-040 — Monitor CLI
**Depends on:** T-MON-031.
**References:** design §3.9.
**Deliverables:**
- `razor-rooster monitor run`.
- `razor-rooster monitor evaluate <analysis_id>`.
- `razor-rooster monitor show <follow_up_id>` — prints reasoning text + key fields.
- `razor-rooster monitor list-alerts [--tier <t>] [--since <iso>]` — ordered by tier.
- `razor-rooster monitor trajectory <analysis_id>` — chronological view across all cycles.
- `razor-rooster monitor note <follow_up_id> "..."`.
**Verification:** CLI integration tests for each subcommand.
**Out of scope:** TUI.

## Phase 5 — Acceptance

### T-MON-080 — End-to-end integration test
**Depends on:** T-MON-040.
**References:** acceptance criteria in MONITOR.md §8.
**Deliverables:**
- Integration test against synthetic upstream subsystems, all watched-analysis states represented.
- Multiple cycles produce trajectory data.
- Resolution-on-cycle scenario: analysis transitions through `'watching'` → resolution detected → `'expired'`.
- Failure isolation, alert ordering all covered.
**Verification:** integration test passes in `make test`.
**Out of scope:** real network.

### T-MON-081 — First cycle on operator hardware
**Depends on:** T-MON-080, position_engine T-PE-081.
**References:** NFR-MON-PERF-001, NFR-MON-DISK-001, OQ-MON-001, DEFER-MON-001.
**Deliverables:**
- Operator runs `razor-rooster monitor run` against the populated system.
- Records: cycle duration, follow-up count, alerts by tier.
- Magnitude distribution measured to validate OQ-MON-001.
- Updates DEFER-MON-001.
**Verification:** measurements recorded under §X-Measurements.
**Out of scope:** auto-tuning.

### T-MON-082 — Operator README
**Depends on:** T-MON-081.
**References:** design §5.
**Deliverables:**
- README updated with Monitor section: daily cadence, reading alerts, trajectory views, note-taking workflow.
**Verification:** new operator can follow README.
**Out of scope:** developer docs.

## Dependency Summary (Critical Path)

    T-MON-001 → T-MON-010 → T-MON-011
                              ↓
    [T-MON-020, T-MON-021, T-MON-022] → T-MON-023
                                            ↓
                                       T-MON-030 → T-MON-031 → T-MON-040 → T-MON-080 → T-MON-081 → T-MON-082

## Tracking

- **T-MON-001** — Module init — `DONE` — 2026-05-15 — `monitor cli + module bootstrap`
- **T-MON-010** — Monitor schemas + migration — `DONE` — 2026-05-15 — `m6001_monitor_initial`
- **T-MON-011** — Persistence helpers — `DONE` — 2026-05-15 — `monitor.persistence.operations`
- **T-MON-020** — Change detector — `DONE` — 2026-05-15 — `engines.change_detector`
- **T-MON-021** — Invalidation evaluator — `DONE` — 2026-05-15 — `engines.invalidation_evaluator`
- **T-MON-022** — Alert ranker — `DONE` — 2026-05-15 — `engines.alert_ranker`
- **T-MON-023** — Reasoning text builder — `DONE` — 2026-05-15 — `engines.reasoning`
- **T-MON-030** — Per-analysis evaluator — `DONE` — 2026-05-15 — `engines.comb.evaluate_analysis`
- **T-MON-031** — Cycle runner — `DONE` — 2026-05-15 — `engines.comb.run_cycle`
- **T-MON-040** — Monitor CLI — `DONE` — 2026-05-15 — `monitor.cli` (run, evaluate, show, list-alerts, trajectory, note, version)
- **T-MON-080** — End-to-end integration test — `DONE` — 2026-05-15 — `tests/monitor/test_end_to_end_cycle.py`
- **T-MON-081** — First cycle on operator hardware — `OPERATOR_BLOCKED` — pending operator first run
- **T-MON-082** — Operator README — `DONE` — 2026-05-15 — `README.md` Monitor section + `docs/monitor.md`

## References

- Requirements: `MONITOR.md` v0.1.0
- Design: `MONITOR_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md`
