# POSITION_ENGINE — Implementation Tasks

**Subsystem:** `position_engine`
**Codename:** The Spur
**Spec version:** 0.1.0 (Tasks draft)
**Status:** PROPOSED
**Last updated:** 2026-05-14
**Companion specs:**
- Requirements: `POSITION_ENGINE.md` v0.1.0
- Design: `POSITION_ENGINE_DESIGN.md` v0.1.0

**Hard prerequisites:**
- `mispricing_detector` Phase 0–4 DONE.
- `polymarket_connector` Phase 0–4 DONE.
- `signal_scanner` Phase 0–4 DONE (for invalidation-criteria extraction from traces).

Task IDs prefixed `T-PE-NNN`.

---

## Phase 0 — Bootstrap

### T-PE-001 — Module init
**Depends on:** mispricing_detector T-MD-001.
**References:** design §3.1.
**Deliverables:**
- Module tree per design §3.1.
- `cli.py` click group.
- `config/position_engine.yaml` and `config/forbidden_phrases.yaml` populated.
- Test layout.
**Verification:** `--help` runs; pytest discovery clean.
**Out of scope:** logic.

## Phase 1 — Schemas

### T-PE-010 — Engine schemas + migration
**Depends on:** T-PE-001, data_ingest T-013.
**References:** design §3.3.
**Deliverables:**
- DDL for `bankroll_config`, `analysis_cycles`, `analyses`, `analysis_traces`, `watch_states`.
- m0001 migration.
**Verification:** schema applies; round-trip per table.
**Out of scope:** persistence helpers.

### T-PE-011 — Persistence helpers
**Depends on:** T-PE-010, data_ingest T-014.
**References:** REQ-PE-PERSIST-001..003, REQ-PE-BR-001.
**Deliverables:** `persistence/operations.py` with helpers for each table.
**Verification:** unit tests per helper.
**Out of scope:** business logic.

## Phase 2 — Bankroll Configuration

### T-PE-020 — Bankroll config CLI
**Depends on:** T-PE-011.
**References:** REQ-PE-BR-001..003, OQ-PE-001 resolution (validation bounds).
**Deliverables:**
- `razor-rooster position-engine config --bankroll <usd> [--max-pct <p>] [--kelly-fraction <f>] [--min-edge <e>]`.
- Validation: `kelly_fraction_default ∈ [0, 0.5]`; `max_single_position_pct ∈ [0, 0.25]`.
- Confirmation prompt with the analytical-bankroll disclaimer.
- `--no-prompt --acknowledge-analytical` for non-interactive use.
**Verification:**
- CLI integration test for normal flow.
- Validation tests for out-of-range values.
- Disclaimer-shown test.
**Out of scope:** integration with cycle.

## Phase 3 — Math Engines

### T-PE-030 — Kelly math
**Depends on:** T-PE-001.
**References:** REQ-PE-CMP-003, REQ-PE-CMP-004, design §3.4.
**Deliverables:**
- `engines/kelly.py` with `kelly_fraction(model_p, market_p)`, `apply_half_kelly(f, default_fraction)`, `clamp_to_max_cap(f, max_pct)`.
- Edge-case handling: market_p in {0, 1}, model_p in {0, 1}, NULL inputs.
**Verification:** edge-case unit tests; positive Kelly, zero Kelly, negative Kelly cases.
**Out of scope:** integration.

### T-PE-031 — Bankroll-survival
**Depends on:** T-PE-001.
**References:** REQ-PE-CMP-005.
**Deliverables:** `engines/bankroll.py` with `compute_survival(suggested_fraction, scenarios=(1, 3, 5))`.
**Verification:** unit tests with known inputs.
**Out of scope:** integration.

### T-PE-032 — Liquidity feasibility
**Depends on:** T-PE-001.
**References:** REQ-PE-CMP-006, OQ-PE-002 resolution.
**Deliverables:** `engines/liquidity.py` with `compute_pct_volume(suggested_dollars, volume_24h)`, `clamp_to_liquidity(suggested, threshold, volume_24h, bankroll)` returning the (possibly reduced) suggested fraction with a flag.
**Verification:** unit tests for clamp triggering and not triggering, edge cases (zero volume, NULL volume).
**Out of scope:** integration.

### T-PE-033 — Invalidation extraction
**Depends on:** T-PE-001.
**References:** REQ-PE-CMP-007.
**Deliverables:** `engines/invalidation.py` with `extract_criteria(scan_trace, comparison, config)` returning a list of structured criteria (precursor-shift criteria from scanner trace, market-move criteria from current market price, mapping-confidence-related criteria).
**Verification:** unit tests against synthetic scanner traces produce expected criteria.
**Out of scope:** rendering.

### T-PE-034 — Sensitivity analysis
**Depends on:** T-PE-030.
**References:** REQ-PE-CMP-007 (extracted to v1 via the design), OQ-PE-004 resolution.
**Deliverables:** `engines/sensitivity.py` with `compute_sensitivity(comparison, config, perturbations=(±0.10, ±0.20))` returning a JSON structure with model_p variants and the resulting suggested_fraction.
**Verification:** unit tests confirm expected variation.
**Out of scope:** rendering.

### T-PE-035 — Time-to-resolution
**Depends on:** T-PE-001.
**References:** REQ-PE-CMP-008, OQ-PE-003 resolution.
**Deliverables:** `engines/time_to_resolution.py` with `days_remaining(market)`, `is_long(days, threshold)`.
**Verification:** unit tests for short, long, no-end-date cases.
**Out of scope:** integration.

## Phase 4 — Framing

### T-PE-040 — Renderer
**Depends on:** T-PE-030, T-PE-031, T-PE-032, T-PE-033, T-PE-034, T-PE-035.
**References:** REQ-PE-FRAME-001, REQ-PE-FRAME-002, REQ-PE-FRAME-003, design §3.5.
**Deliverables:** `frame/renderer.py` with `render(analysis, verbose=False) -> str` filling the template from design §3.5.
**Verification:**
- Output inspection test: warnings appear before sizing math.
- Disclaimer-block exact-text test.
- Verbose mode includes sensitivity, normal mode does not.
- Conditional-language test: rendered output contains "if the operator chose to act" or equivalent.
**Out of scope:** linter (T-PE-041).

### T-PE-041 — Imperative-language linter
**Depends on:** T-PE-001 (for config), T-PE-040.
**References:** REQ-PE-FRAME-002, OQ-PE-006 resolution.
**Deliverables:**
- `frame/linter.py` reading `config/forbidden_phrases.yaml` and running case-insensitive substring match against rendered output.
- Raises `ImperativeLanguageDetected` with offending phrase highlighted.
- Catalog seeded with the OQ-PE-006 list.
**Verification:**
- Unit test per seed phrase: linter rejects.
- Unit test on standard renderer output: linter passes.
- Unit test on adversarial output deliberately containing imperative: linter rejects with clear error.
**Out of scope:** automatic phrase rephrasing.

## Phase 5 — Analyzer

### T-PE-050 — Analyzer
**Depends on:** T-PE-030..T-PE-035, T-PE-041, T-PE-011.
**References:** REQ-PE-CMP-001..008, design §3.4.
**Deliverables:** `engines/analyzer.py` `analyze_comparison(cycle_id, comparison, config) -> Analysis`.
- Sub-threshold filter (skip if |delta| < min_edge_threshold).
- Calls each math engine in dependency order.
- Builds Analysis dataclass.
**Verification:**
- Unit test against synthetic comparison: produces expected Analysis with all fields populated.
- Sub-threshold test: returns Analysis tagged sub_threshold without Kelly math.
- Negative-Kelly test: returns Analysis with kelly_negative=True, suggested_fraction=0.
**Out of scope:** cycle orchestration.

### T-PE-051 — Cycle runner
**Depends on:** T-PE-050.
**References:** REQ-PE-CMP-001, REQ-PE-LOG-001, design §3.4 run_cycle.
**Deliverables:** `engines/analyzer.py` extended with `run_cycle(include_suppressed=False)`.
- Reads new surfaced comparisons since last cycle from `mispricing_detector`.
- Per-comparison failure isolation.
- Renders + lints + persists.
- Structured cycle log.
**Verification:** integration test against synthetic comparisons; failure isolation verified.
**Out of scope:** watch state expiration (T-PE-061).

## Phase 6 — Watch State

### T-PE-060 — Watch state CLI
**Depends on:** T-PE-011, T-PE-050.
**References:** REQ-PE-WATCH-001..003.
**Deliverables:**
- `watch/state.py` with `set_state`, `latest_state`, `list_by_state`.
- CLI: `watch`, `acted-on`, `dismiss` subcommands per requirements.
**Verification:** CLI integration tests; latest-row-wins query semantics confirmed.
**Out of scope:** auto-expiration.

### T-PE-061 — Expiration pass
**Depends on:** T-PE-060, mispricing_detector T-MD-041.
**References:** OQ-PE-005 resolution, REQ-PE-WATCH-002.
**Deliverables:** `watch/expiration.py` with `run_expiration_pass()` that finds resolved comparisons with active watch states and transitions to `'expired'`.
- Idempotent.
- Wired into the cycle runner (T-PE-051).
**Verification:** integration test with synthetic resolutions confirms expirations fire, idempotent on re-run.
**Out of scope:** notifications.

## Phase 7 — CLI

### T-PE-070 — Analysis CLI
**Depends on:** T-PE-051, T-PE-060, T-PE-061.
**References:** design §3.7.
**Deliverables:**
- `razor-rooster position-engine run [--include-suppressed]`.
- `razor-rooster position-engine analyze <comparison_id>`.
- `razor-rooster position-engine show <analysis_id> [--verbose]`.
- `razor-rooster position-engine list [--watched | --acted-on | --dismissed | --expired]`.
**Verification:** CLI integration tests for each subcommand.
**Out of scope:** TUI.

## Phase 8 — Acceptance

### T-PE-080 — End-to-end integration test
**Depends on:** T-PE-070.
**References:** acceptance criteria in POSITION_ENGINE.md §9.
**Deliverables:**
- Integration test against synthetic upstream subsystems.
- All components exercised: Kelly math, bankroll survival, liquidity clamp, sub-threshold filter, watch state, expiration, renderer, linter.
- Adversarial test confirms linter blocks imperative output.
**Verification:** integration test passes in `make test`.
**Out of scope:** real network.

### T-PE-081 — First cycle on operator hardware
**Depends on:** T-PE-080, mispricing_detector T-MD-081.
**References:** NFR-PE-PERF-001, NFR-PE-DISK-001, OQ-PE-002, DEFER-PE-001..003.
**Deliverables:**
- Operator runs `razor-rooster position-engine run` after `mispricing_detector` populates surfaced comparisons.
- Records: cycle duration, analysis count, Kelly distribution, clamping rates (cap and liquidity).
- Validates liquidity threshold default.
- Updates DEFER-PE-001..003.
**Verification:** measurements recorded under §X-Measurements.
**Out of scope:** auto-tuning.

### T-PE-082 — Operator README
**Depends on:** T-PE-081.
**References:** design §5.
**Deliverables:**
- README updated with Position Engine section: bankroll-config setup, analytical-vs-real-capital framing, daily cycle, reviewing analyses, watch state workflow.
- `docs/position_engine.md` explaining the half-Kelly conservatism, the linter, and the forbidden-phrases catalog.
**Verification:** new operator can follow README.
**Out of scope:** developer docs.

## Dependency Summary (Critical Path)

    T-PE-001 → T-PE-010 → T-PE-011 → T-PE-020
                            ↓
    [T-PE-030..T-PE-035 in parallel]
                            ↓
                       T-PE-040 → T-PE-041
                            ↓
                       T-PE-050 → T-PE-051
                            ↓
                  [T-PE-060, T-PE-061]
                            ↓
                       T-PE-070 → T-PE-080 → T-PE-081 → T-PE-082

## Tracking

- **T-PE-NNN** — title — `OPEN` | `IN_PROGRESS` | `DONE` | `BLOCKED <reason>` — `<date>` — `<commit-sha or PR link>`

Status (LOOM v0.33.0):

- **T-PE-001** — Module init — `DONE` — 2026-05-15
- **T-PE-010** — Engine schemas + migration — `DONE` — 2026-05-15
- **T-PE-011** — Persistence helpers — `DONE` — 2026-05-15
- **T-PE-020** — Bankroll config CLI — `DONE` — 2026-05-15
- **T-PE-030** — Kelly math — `DONE` — 2026-05-15
- **T-PE-031** — Bankroll-survival — `DONE` — 2026-05-15
- **T-PE-032** — Liquidity feasibility — `DONE` — 2026-05-15
- **T-PE-033** — Invalidation extraction — `DONE` — 2026-05-15
- **T-PE-034** — Sensitivity analysis — `DONE` — 2026-05-15
- **T-PE-035** — Time-to-resolution — `DONE` — 2026-05-15
- **T-PE-040** — Renderer — `DONE` — 2026-05-15
- **T-PE-041** — Imperative-language linter — `DONE` — 2026-05-15
- **T-PE-050** — Analyzer — `DONE` — 2026-05-15
- **T-PE-051** — Cycle runner — `DONE` — 2026-05-15
- **T-PE-060** — Watch state CLI — `DONE` — 2026-05-15
- **T-PE-061** — Expiration pass — `DONE` — 2026-05-15
- **T-PE-070** — Analysis CLI — `DONE` — 2026-05-15
- **T-PE-080** — End-to-end integration test — `DONE` — 2026-05-15
- **T-PE-081** — First cycle on operator hardware — `OPERATOR_BLOCKED` — depends on mispricing_detector T-MD-081
- **T-PE-082** — Operator README — `DONE` — 2026-05-15

All Phases 0-8 fully complete. Lifecycle: PRODUCTION_READY.
T-PE-081 mirrors the OPERATOR_BLOCKED pattern from upstream
subsystems — the empirical Kelly distribution and clamping rates
land when the operator runs the first full cycle against real
mispricing comparisons on their EliteBook G8.

## References

- Requirements: `POSITION_ENGINE.md` v0.1.0
- Design: `POSITION_ENGINE_DESIGN.md` v0.1.0
- LOOM: `razorrooster.md`
