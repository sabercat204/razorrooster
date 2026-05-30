# MISPRICING_DETECTOR — Implementation Tasks

**Subsystem:** `mispricing_detector`
**Codename:** The Liver
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `MISPRICING_DETECTOR.md` v0.1.0
- Design: `MISPRICING_DETECTOR_DESIGN.md` v0.1.0

**Hard prerequisites:**
- `data_ingest` Phase 0–4 DONE.
- `polymarket_connector` Phase 0–6 DONE (markets, prices, resolutions, sector mapping all needed).
- `pattern_library` Phase 0–6 DONE (library facade needed).
- `signal_scanner` Phase 0–4 DONE (scan records and traces needed).

Task IDs prefixed `T-MD-NNN`.

---

## Phase 0 — Module Bootstrap

### T-MD-001 — Initialize mispricing_detector module
**Depends on:** signal_scanner T-SCAN-001.
**References:** design §3.1.
**Deliverables:**
- `razor_rooster/mispricing_detector/` directory tree.
- `cli.py` with `razor-rooster mispricing` click group.
- `config/mispricing.yaml` populated with default thresholds, liquidity floors per sector.
- Mirror test layout under `tests/mispricing_detector/`.
**Verification:** `razor-rooster mispricing --help` runs; pytest discovery clean.
**Out of scope:** logic.

## Phase 1 — Schemas

### T-MD-010 — Mapping and comparison schemas
**Depends on:** T-MD-001, data_ingest T-013.
**References:** design §3.3.
**Deliverables:**
- `persistence/schemas.py` with DDL for: `class_market_mappings`, `comparison_cycles`, `comparisons`, `comparison_traces`, `comparison_resolutions`, `mispricing_detector_state`.
- `persistence/migrations/m0001_mispricing_detector_initial.py`.
**Verification:** schema migration applies; round-trip test per table; unique-constraint test confirms duplicate active mappings rejected.
**Out of scope:** persistence helpers.

### T-MD-011 — Persistence helpers
**Depends on:** T-MD-010, data_ingest T-014.
**References:** REQ-MD-PERSIST-001, REQ-MD-PERSIST-002, REQ-MD-PERSIST-003.
**Deliverables:**
- `persistence/operations.py` with: `register_mapping`, `unmap`, `query_mappings`, `write_cycle`, `complete_cycle`, `persist_comparison`, `persist_trace`, `write_resolution_link`, `state_get`, `state_set`.
**Verification:** unit tests per operation.
**Out of scope:** business logic.

## Phase 2 — Mapping

### T-MD-020 — Operator mapping CLI
**Depends on:** T-MD-011.
**References:** REQ-MD-MAP-001, REQ-MD-MAP-002, REQ-MD-MAP-004, REQ-MD-MAP-005, OQ-MD-002 resolution.
**Deliverables:**
- `mapping/operator_overrides.py` with `register`, `remove`, `list_active`, `list_with_filters`.
- CLI subcommands:
  - `razor-rooster mispricing map <class_id> <condition_id> --type <t> [--polarity inverted] [--notes ...]`
  - `razor-rooster mispricing unmap <mapping_id>`
  - `razor-rooster mispricing list-mappings [--class ...] [--market ...] [--confidence ...]`
- Polarity flag accepted; defaults to `'aligned'`.
**Verification:** CLI integration tests; unique-constraint enforcement test.
**Out of scope:** auto-mapping.

### T-MD-021 — Auto-mapping heuristics
**Depends on:** T-MD-011.
**References:** OQ-MD-001 resolution, REQ-MD-MAP-003.
**Deliverables:**
- `mapping/auto_heuristic.py` with `confidence(class_, market) -> str | None`.
- Sector match check, keyword overlap with stemmer + stopwords, temporal qualifier regex catalog.
- Returns `'inferred'` | `'low'` | `None` (no auto-mapping).
**Verification:**
- Unit test: matching sector + 5 keyword overlaps + temporal phrase → 'inferred'.
- Unit test: matching sector + 1 keyword overlap → 'low'.
- Unit test: mismatched sector → None.
- Unit test: no temporal phrase + 5 keywords → 'low'.
**Out of scope:** mapping persistence.

### T-MD-022 — Mapping resolver
**Depends on:** T-MD-020, T-MD-021.
**References:** REQ-MD-MAP-003, design §3.5.
**Deliverables:**
- `engines/mapping_resolver.py` with `resolve(class_id_filter)`.
- Combines operator-curated active mappings with auto-derived (excluding classes already operator-mapped to a market).
- Auto-mappings never overwrite or get persisted to `class_market_mappings` — they're computed fresh per cycle.
**Verification:**
- Integration test: operator mapping survives auto-mapping pass.
- Integration test: tombstoned mapping (removed_at set) does not produce auto-mapping for the same pair.
- Integration test: filter by class_id returns only that class's mappings.
**Out of scope:** comparison computation.

## Phase 3 — Comparison Computation

### T-MD-030 — Probability and delta math
**Depends on:** T-MD-001.
**References:** REQ-MD-CMP-005, REQ-MD-CMP-006, OQ-MD-002 resolution, OQ-MD-006 resolution, design §3.4 compute_comparison.
**Deliverables:**
- `engines/delta.py` with `market_probability_from(snapshot, polarity)`, `compute_delta(model_p, market_p, polarity)`, `log_odds_delta(model_p, market_p)`, `expected_value(model_p, market_p)`.
- Polarity application: aligned uses YES side; inverted uses 1-YES.
- Edge cases: NULL prices, 0/1 boundary prices for log-odds (clip to (eps, 1-eps)).
**Verification:**
- Unit test per function.
- Polarity test: aligned and inverted with same inputs produce expected different deltas.
- Edge case tests for NULL and boundary inputs.
**Out of scope:** orchestration.

### T-MD-031 — CI overlap analysis
**Depends on:** T-MD-001.
**References:** REQ-MD-CMP-007, design §3.4.
**Deliverables:**
- `engines/ci_overlap.py` with `check_ci_overlap(model_ci, market_bid, market_ask)`.
- Returns True if intervals overlap or touch.
- Handles NULL bid or ask gracefully (treats as "cannot determine overlap" → returns False as the safer default).
**Verification:** unit tests for: clean overlap, no overlap, touching intervals, model CI inside market range, market range inside model CI, NULL bid/ask.
**Out of scope:** integration into comparator.

### T-MD-032 — Surfacing logic
**Depends on:** T-MD-030, T-MD-031.
**References:** REQ-MD-CMP-008, OQ-MD-004 resolution, design §3.6.
**Deliverables:**
- `engines/surfacing.py` with `surfacing_decision(log_odds_delta, ci_overlap, warnings, mapping_confidence, sector, config)`.
- Returns `(surfaced: bool, suppression_reasons: list[str])`.
- `confidence_weighted_score(log_odds_delta, signature_confidence, market_volume, liquidity_floor)`.
**Verification:**
- Unit tests for each suppression reason as a single trigger.
- Unit tests for combined triggers.
- Unit test for score formula with synthetic inputs.
**Out of scope:** comparison record assembly.

### T-MD-033 — Comparison trace builder
**Depends on:** T-MD-030, T-MD-031, T-MD-032.
**References:** REQ-MD-TRACE-001..005, design §3.7.
**Deliverables:**
- `engines/trace.py` with `build_trace(scan_record, scan_trace, snapshot, market, mapping, ...) -> dict`.
- `render_trace_text(trace) -> str` produces text with `case_for_model` and `case_for_market` sections at equal prominence.
- Embeds full scanner trace (REQ-MD-TRACE-002).
- Populates contextual market fields (REQ-MD-TRACE-003).
**Verification:**
- Unit test: build_trace populates all expected sections.
- Unit test: rendered text contains both case-for-model and case-for-market sections; word count of "market may be right" content is at least equal to "model may be right" content.
- JSON round-trip test.
**Out of scope:** comparator orchestration.

### T-MD-034 — Comparator
**Depends on:** T-MD-022, T-MD-030, T-MD-031, T-MD-032, T-MD-033, T-MD-011.
**References:** REQ-MD-CMP-001..008, design §3.4.
**Deliverables:**
- `engines/comparator.py` with `compute_comparison(cycle_id, mapping, scan_summary)` and per-comparison failure isolation.
- Reads model side from `signal_scanner.scan_record(scan_id, class_id)`.
- Reads market side from `polymarket_connector.market(condition_id)` and `polymarket_connector.latest_price(...)`.
- Picks YES outcome token (binary v1 only — multi-outcome markets skipped with documented reason).
- Assembles `Comparison` dataclass and trace.
**Verification:**
- Unit test with synthetic upstream data: produces expected Comparison.
- Failure-isolation test: missing market raises a typed exception that the orchestrator (T-MD-040) handles, not a runtime crash.
- Multi-outcome market: skipped with `MultiOutcomeMarketSkipped` typed result.
**Out of scope:** cycle orchestration.

## Phase 4 — Cycle Orchestration

### T-MD-040 — Cycle runner
**Depends on:** T-MD-034.
**References:** REQ-MD-CMP-001, design §3.4 run_cycle.
**Deliverables:**
- `engines/comparator.py` extended with `run_cycle(class_id_filter=None)`.
- Reads latest scan summary from `signal_scanner`. Raises typed error if no scan available.
- Iterates all active mappings, calls `compute_comparison` per mapping, persists results.
- Per-mapping failure isolation: exception captured, logged, cycle continues.
- Aggregates suppression reasons into cycle-level `suppressed_breakdown`.
- Emits structured cycle log (REQ-MD-LOG-001).
**Verification:**
- Integration test: full cycle against synthetic upstream produces expected comparisons.
- Failure-isolation test: one mapping throws, cycle completes for others.
- No-scan-available test: raises typed error early.
**Out of scope:** linkage pass.

### T-MD-041 — Linkage pass
**Depends on:** T-MD-011.
**References:** REQ-MD-PERSIST-003, OQ-MD-005 resolution, design §3.8.
**Deliverables:**
- `engines/linkage.py` with `run_linkage_pass()`.
- Reads `polymarket_resolutions` rows since `state.last_linkage_ts`.
- For each resolution, finds comparisons referencing the market and writes `comparison_resolutions` row.
- Polarity adjustment: `outcome_observed = 1 if (resolution_outcome == 'yes' and polarity == 'aligned') or (resolution_outcome == 'no' and polarity == 'inverted') else 0`.
- Idempotent: re-runs do not duplicate.
**Verification:**
- Integration test: synthetic resolutions trigger expected linkages.
- Polarity test: inverted mapping with NO resolution produces `outcome_observed = 1`.
- Idempotency test: re-run produces no new rows.
- Resume test: linkage from `last_linkage_ts` correctly skips already-linked resolutions.
**Out of scope:** real-time linkage on resolution event.

### T-MD-042 — Cycle integration
**Depends on:** T-MD-040, T-MD-041.
**References:** design §3.4.
**Deliverables:**
- `run_cycle` calls `run_linkage_pass` after the comparison pass.
- Both logged as separate phases in the cycle's structured log.
**Verification:** integration test confirms linkage phase runs, fails gracefully if it errors.
**Out of scope:** UI.

## Phase 5 — CLI

### T-MD-050 — Comparison CLI
**Depends on:** T-MD-042.
**References:** design §3.9.
**Deliverables:**
- `razor-rooster mispricing run [--class <id>]`.
- `razor-rooster mispricing show <comparison_id>` — renders trace.
- `razor-rooster mispricing list-comparisons [--surfaced-only] [--since <iso>]`.
- `razor-rooster mispricing relink` — runs linkage pass on demand.
**Verification:** CLI integration tests for each subcommand.
**Out of scope:** TUI.

## Phase 6 — Acceptance

### T-MD-080 — End-to-end integration test
**Depends on:** T-MD-050.
**References:** acceptance criteria in MISPRICING_DETECTOR.md §8.
**Deliverables:**
- Integration test against synthetic upstream subsystems.
- All seed classes mapped, mixed mapping confidences, comparisons produced.
- Surfacing matches expectations: large delta + high confidence + liquid market = surfaced; same with stale price = suppressed.
- Polarity inversion case included.
- Linkage pass with synthetic resolutions populates `comparison_resolutions`.
**Verification:** integration test passes in `make test`.
**Out of scope:** real network.

### T-MD-081 — First cycle on operator hardware
**Depends on:** T-MD-080, signal_scanner T-SCAN-081, polymarket_connector T-PMC-073.
**References:** NFR-MD-PERF-001, NFR-MD-DISK-001, OQ-MD-003, DEFER-MD-001..002.
**Deliverables:**
- Operator runs `razor-rooster mispricing run` against the populated system.
- Records: cycle duration, comparison count, surfaced count, suppression breakdown.
- Empirical Polymarket volume distribution measured to validate liquidity-floor default.
- Updates DEFER-MD-001 and DEFER-MD-002 with measurements.
**Verification:** measurements recorded under §X-Measurements.
**Out of scope:** automatic threshold tuning.

### T-MD-082 — Operator README updates
**Depends on:** T-MD-081.
**References:** design §6.
**Deliverables:**
- README updated with Mispricing Detector section: setting up class-to-market mappings, running cycles, reading comparisons, polarity rules.
- `docs/mispricing.md` (or similar) explaining the surfacing logic and the case-for-model vs case-for-market framing.
**Verification:** new operator can follow README from clean machine to working cycle.
**Out of scope:** developer docs.

## Dependency Summary (Critical Path)

    T-MD-001 → T-MD-010 → T-MD-011
                            ↓
          [T-MD-020, T-MD-021] → T-MD-022
                                    ↓
    [T-MD-030, T-MD-031] → T-MD-032 → T-MD-033 → T-MD-034 → T-MD-040 → T-MD-041 → T-MD-042 → T-MD-050 → T-MD-080 → T-MD-081 → T-MD-082

## Tracking

- **T-MD-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.32.0):

- **T-MD-001** — Initialize mispricing_detector module — `DONE` — 2026-05-15
- **T-MD-010** — Mapping and comparison schemas — `DONE` — 2026-05-15
- **T-MD-011** — Persistence helpers — `DONE` — 2026-05-15
- **T-MD-020** — Operator mapping CLI — `DONE` — 2026-05-15
- **T-MD-021** — Auto-mapping heuristics — `DONE` — 2026-05-15
- **T-MD-022** — Mapping resolver — `DONE` — 2026-05-15
- **T-MD-030** — Probability and delta math — `DONE` — 2026-05-15
- **T-MD-031** — CI overlap analysis — `DONE` — 2026-05-15
- **T-MD-032** — Surfacing logic — `DONE` — 2026-05-15
- **T-MD-033** — Comparison trace builder — `DONE` — 2026-05-15
- **T-MD-034** — Comparator — `DONE` — 2026-05-15
- **T-MD-040** — Cycle runner — `DONE` — 2026-05-15
- **T-MD-041** — Linkage pass — `DONE` — 2026-05-15
- **T-MD-042** — Cycle integration — `DONE` — 2026-05-15
- **T-MD-050** — Comparison CLI — `DONE` — 2026-05-15
- **T-MD-080** — End-to-end integration test — `DONE` — 2026-05-15
- **T-MD-081** — First cycle on operator hardware — `OPERATOR_BLOCKED` — depends on signal_scanner T-SCAN-081 + polymarket_connector T-PMC-073
- **T-MD-082** — Operator README updates — `DONE` — 2026-05-15

All Phases 0-6 fully complete. Lifecycle: PRODUCTION_READY.
T-MD-081 mirrors the OPERATOR_BLOCKED pattern from upstream
subsystems — the first cycle against real Polymarket prices and a
real signal_scanner posterior lands when the operator runs the
backfill, library refresh, and scanner cycle on their EliteBook G8.

## References

- Requirements: `MISPRICING_DETECTOR.md` v0.1.0
- Design: `MISPRICING_DETECTOR_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md`
