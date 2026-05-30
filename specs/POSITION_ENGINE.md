# POSITION_ENGINE — Requirements

**Subsystem:** `position_engine`
**Codename:** The Spur
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD (revised from FULL — see §3)
**Last updated:** 2026-05-14

---

## 1. Purpose

`position_engine` is the analysis-and-sizing layer. Given a surfaced comparison from `mispricing_detector`, its job is to produce a *sizing analysis* — a structured document that helps the operator think about whether and how much to act on the comparison if they choose to.

The subsystem produces analyses, not directives. It computes Kelly fractions, half-Kelly bounds, expected-value figures, and bankroll-survival diagnostics, and presents them with explicit framing that this is decision support and not a recommendation to trade. The operator decides whether to act, by how much, and when.

Downstream consumers:
- `report_generator` reads sizing analyses to populate the cycle report's "if you choose to act" section (intentionally framed as conditional).
- `monitor` reads the analyses to track which comparisons the operator has flagged as "watching" or "acting on" so it can surface invalidation triggers.
- The calibration backtest reads stored analyses retrospectively to evaluate how Kelly-sized hypothetical positions would have performed against actual outcomes — purely an analytical metric, not a P&L.

`position_engine` does not place orders, manage real capital, hold positions, or interact with Polymarket beyond reading. It is a paper-analysis subsystem.

## 2. Scope

### In scope (v1)

- For each surfaced `mispricing_detector` comparison: compute Kelly fraction, half-Kelly fraction, EV, bankroll-survival metrics, and adverse-scenario impact.
- A configurable bankroll model that the operator declares for analytical purposes (e.g., "the bankroll I'd hypothetically allocate to Polymarket activity is $X"). The subsystem does not enforce this in reality; it's an input to sizing math.
- Sizing analyses that surface explicit invalidation criteria (what observable events would change the analysis).
- A "watch list" mechanism: the operator can mark a comparison as one they're watching, which causes `monitor` to track it and surface follow-ups when conditions change.
- Persistence of every analysis so it can be referenced later (calibration backtest, retrospective review).
- Failure isolation: bad input from `mispricing_detector` produces a flagged-incomplete analysis, not a crash.
- Explicit framing in every output: "this is decision-support, not investment advice; the operator chooses whether and how to act; bankroll risk is the operator's responsibility."

### Out of scope (explicit)

- **Order placement.** Confirmed by OT-004; v1 is recommendation-only and `position_engine` does not interact with Polymarket trading APIs.
- **Real capital tracking.** No P&L tables, no realized-position accounting. The bankroll figure is a config input for sizing math, not a tracked value.
- **Wallet integration.** No Polygon wallet, no signing, no L2 API credentials. None of this code path exists in v1.
- **Position management semantics** (entry orders, stop-loss, take-profit). Not a v1 feature.
- **Multi-leg or hedged positions.** v1 considers each comparison independently.
- **Aggressive sizing heuristics.** v1 supports Kelly and half-Kelly only. More aggressive fractions (e.g. fractional Kelly with momentum) are deliberately out of scope; they encourage behavior the educational framing discourages.
- **Order book impact analysis.** v1 assumes the operator's hypothetical position size is a small fraction of market liquidity. If the position would meaningfully move the market, the analysis flags this but does not model the price impact.

## 3. Threat Context Reassessment

The LOOM originally records `threat_context: FULL` for `position_engine`, on the assumption that it would handle real financial decisions and potentially wallet integration. With the v1 scope locked to paper-analysis only:

- No credentials are stored.
- No transactions can be initiated.
- No real bankroll is tracked or held.

The threat context for v1 is therefore **STANDARD**, matching the rest of the system. If and when v2+ adds order placement or real capital handling, the threat context for those code paths returns to FULL and a separate spec amendment specifies the wallet handling, custody model, and operator-side authorization gates.

This reassessment is recorded in the LOOM evolution log alongside this spec.

## 4. Operating Assumptions

- **Cadence:** runs daily after `mispricing_detector` completes. Operator can run additional ad-hoc analyses.
- **Bankroll discipline:** the operator's declared bankroll is for analytical purposes; the subsystem assumes the operator understands this and does not put real capital at risk based on these analyses without their own deliberation.
- **Conservative-Kelly default:** the system surfaces half-Kelly as the default suggested fraction. Full Kelly is computed and shown but explicitly framed as "the theoretical maximum for log-bankroll growth, before accounting for model error, transaction costs, and slippage."
- **Per-comparison independence:** v1 analyzes each comparison in isolation. Multi-position sizing, correlation between comparisons, and portfolio-level Kelly are deferred.
- **No automated execution:** the engine never sends orders. The operator types orders into Polymarket UI themselves if they choose to act.

## 5. Conceptual Model

### 5.1 Bankroll Configuration

A *bankroll configuration* is a declared analytical figure plus risk parameters:

- `analytical_bankroll_usd`: the dollar figure the operator is using as a sizing baseline.
- `max_single_position_pct`: hard cap on what fraction of bankroll a single comparison's Kelly suggestion can occupy. Default: 5%.
- `kelly_fraction_default`: how aggressive the suggested fraction is. Default: 0.5 (half-Kelly).
- `min_edge_threshold`: minimum |delta| in probability units below which no analysis is computed (sub-noise). Default: 0.03.

These are operator-declared and persisted. The engine reads them; it does not enforce them against real capital.

### 5.2 Sizing Analysis

A *sizing analysis* is the per-comparison output:

- Reference to the source comparison (and through it, scan record and event class).
- Kelly fraction: `f* = (p × b - q) / b` where `p` is model probability, `q = 1 - p`, `b` is the payoff multiplier (1 for binary YES/NO at par).
- Suggested fraction: `kelly_fraction_default × f*`, clamped to `[0, max_single_position_pct]`.
- Suggested dollar size: `analytical_bankroll_usd × suggested_fraction`.
- Expected value (analytical metric, not a recommendation): `ev_per_dollar = p × (1/market_p - 1) - (1 - p)`.
- Bankroll-survival metrics: in adverse scenarios (3 consecutive losses, 5 consecutive losses), what would the bankroll be assuming each adverse-scenario position was sized at the suggested fraction.
- Invalidation criteria: which observable events (precursor variable shifts, market moves, Polymarket resolution mechanism notes) would change the analysis enough to revisit.
- Time-to-resolution context: when the underlying Polymarket market resolves (from `polymarket_markets.end_date`).
- Liquidity feasibility: at the suggested dollar size, what fraction of the market's recent volume does the position represent.
- Warning flags carried from upstream plus engine-specific (kelly_negative, kelly_above_max_cap, low_liquidity, long_time_to_resolution).
- Explicit framing block: standard text disclaiming that this is decision support, not investment advice.

### 5.3 Watch State

A *watch state* is operator-declared metadata on a specific comparison:

- `'watching'`: operator wants follow-ups on this comparison; `monitor` should track it.
- `'acted_on'`: operator has made a decision and (presumably) acted; `monitor` should track this for retrospective review.
- `'dismissed'`: operator has decided not to act; `monitor` should not surface this comparison again unless conditions change materially.

Watch state is purely metadata. Setting `'acted_on'` does not constitute Polymarket activity; it's the operator's declaration for the system's tracking.

## 6. Functional Requirements

Requirements use EARS-style phrasing. PE = `position_engine`.

### 6.1 Bankroll configuration

**REQ-PE-BR-001: Bankroll configuration storage**
The engine **shall** persist bankroll configuration in a `bankroll_config` table with: `analytical_bankroll_usd`, `max_single_position_pct`, `kelly_fraction_default`, `min_edge_threshold`, `effective_at`, `updated_by`. Each row is a snapshot; updates create new rows rather than overwriting.
*Verification:* schema migration; round-trip.

**REQ-PE-BR-002: Bankroll configuration CLI**
The operator **shall** be able to update bankroll configuration via `razor-rooster position-engine config --bankroll <usd> [--max-pct <p>] [--kelly-fraction <f>] [--min-edge <e>]`. Updates require confirmation.
*Verification:* CLI integration test.

**REQ-PE-BR-003: Disclaimer on configuration**
Every config update **shall** display a clear message: "This is an analytical bankroll figure for sizing math. The system does not track real capital; the operator is responsible for any real-world action."
*Verification:* CLI test confirms message displayed; non-interactive runs require explicit `--no-prompt --acknowledge-analytical` flags.

### 6.2 Analysis computation

**REQ-PE-CMP-001: Analysis cycle**
The engine **shall** provide `razor-rooster position-engine run` that, for every surfaced `mispricing_detector` comparison since the last run, computes a sizing analysis. Non-surfaced comparisons are not analyzed unless explicitly requested via `--include-suppressed`.
*Verification:* CLI integration test against synthetic surfaced comparisons.

**REQ-PE-CMP-002: Per-comparison scope**
The engine **shall** support `razor-rooster position-engine analyze <comparison_id>` for ad-hoc analysis of a single comparison.
*Verification:* CLI integration test.

**REQ-PE-CMP-003: Kelly computation**
The engine **shall** compute Kelly fraction `f* = max(0, (p × b - q) / b)` where `p = model_probability`, `q = 1 - p`, `b = (1 / market_probability) - 1` for a binary YES position. Negative Kelly is clamped to zero with `kelly_negative` flag. The unclamped value is also stored for transparency.
*Verification:* unit tests for positive Kelly, zero Kelly, negative Kelly, edge cases (p = 0, p = 1, market_p = 0, market_p = 1).

**REQ-PE-CMP-004: Suggested fraction**
The engine **shall** compute `suggested_fraction = clamp(kelly_fraction_default × f*, 0, max_single_position_pct)`. Clamps are flagged separately (`kelly_above_max_cap` if clamped down).
*Verification:* unit tests for clamp triggering and not triggering.

**REQ-PE-CMP-005: Bankroll-survival metrics**
The engine **shall** compute bankroll-survival in three scenarios at the suggested fraction:
- 1 consecutive adverse outcome.
- 3 consecutive adverse outcomes.
- 5 consecutive adverse outcomes.
Each scenario computes the resulting bankroll fraction. Scenarios assume the suggested fraction is reapplied each time.
*Verification:* unit tests with known suggested fractions produce expected scenario outputs.

**REQ-PE-CMP-006: Liquidity feasibility check**
The engine **shall** compute the suggested dollar size as a percentage of the market's `volume_24h`. If the percentage exceeds a configurable threshold (default: 5% of 24h volume), the analysis is flagged `low_liquidity` and the suggested fraction is reduced to keep the dollar size at the threshold. The unreduced suggestion is also stored for transparency.
*Verification:* unit test for liquidity-driven reduction.

**REQ-PE-CMP-007: Invalidation criteria extraction**
The engine **shall** generate invalidation criteria automatically from the underlying `signal_scanner` reasoning trace: each precursor variable that fired contributes a "if precursor X drops below threshold Y" criterion; the market price contributes "if market_p moves to Z" criteria where Z represents the price at which |delta| would fall below the surfacing threshold. Operator-curated invalidation criteria can be added per-analysis.
*Verification:* unit test confirms expected criteria generated for synthetic inputs.

**REQ-PE-CMP-008: Time-to-resolution context**
The analysis **shall** include the days remaining until the market's `end_date` and flag `long_time_to_resolution` when the window exceeds a configurable threshold (default: 365 days). Long-resolution markets have higher uncertainty and the analysis adds a corresponding caveat.
*Verification:* unit test confirms flag and caveat for long windows.

### 6.3 Framing and disclaimers

**REQ-PE-FRAME-001: Standard disclaimer block**
Every analysis **shall** include a standard disclaimer block: "This is decision-support analysis. Kelly figures are theoretical optima before accounting for model error, transaction costs, slippage, and the possibility that the model is wrong. Half-Kelly is the conservative default and should still be considered an upper bound. The system does not place orders; the operator decides whether and how to act, and is responsible for any real-world outcomes."
*Verification:* output inspection test confirms exact text present in every analysis.

**REQ-PE-FRAME-002: Explicit conditional language**
Analysis output **shall** use conditional language throughout: "if the operator chose to act," "the suggested fraction would be," etc. The renderer checks for and refuses to ship output containing imperative recommendations like "you should buy."
*Verification:* renderer linter rejects output containing imperative phrases.

**REQ-PE-FRAME-003: Confidence and uncertainty prominence**
Confidence indicators (low signature confidence, source stale, library stale, definition drift, low mapping confidence) **shall** be displayed at the top of the analysis output, before sizing math. Operators read warnings before numbers.
*Verification:* output ordering test.

### 6.4 Watch state

**REQ-PE-WATCH-001: Watch state CLI**
The operator **shall** be able to set watch state via:
- `razor-rooster position-engine watch <analysis_id> --note "..."`
- `razor-rooster position-engine acted-on <analysis_id> --note "..."`
- `razor-rooster position-engine dismiss <analysis_id> --reason "..."`
*Verification:* CLI integration tests for each.

**REQ-PE-WATCH-002: Watch state persistence**
Watch state **shall** persist in a `watch_states` table with: `analysis_id`, `state`, `notes`, `set_at`, `set_by`. State changes append rows; the latest row wins.
*Verification:* schema migration; round-trip test.

**REQ-PE-WATCH-003: Watch state queryability**
The operator **shall** be able to query watch states: `razor-rooster position-engine list-watched`, `list-acted-on`, `list-dismissed`.
*Verification:* CLI integration tests.

### 6.5 Persistence

**REQ-PE-PERSIST-001: Analysis tables**
The engine **shall** persist outputs to `analyses` (one row per analysis), `analysis_traces` (rendered analysis output as JSON), and `analysis_cycles` (one row per cycle execution).
*Verification:* schema migration; round-trip test.

**REQ-PE-PERSIST-002: Time-series retention**
Historical analyses **shall** be retained indefinitely. Each cycle produces new analyses; prior analyses are not overwritten.
*Verification:* repeated cycles accumulate rows.

**REQ-PE-PERSIST-003: Linkage to comparison and resolution**
An analysis **shall** reference its source `comparison_id`. When the underlying comparison resolves (via `comparison_resolutions`), retrospective metrics on the analysis (would-have-been P&L assuming suggested fraction was applied) are computable. v1 does not auto-compute these; they're available via SQL query.
*Verification:* DuckDB query produces hypothetical-outcome computation for representative resolved comparison.

### 6.6 Logging & observability

**REQ-PE-LOG-001: Structured cycle log**
Each analysis cycle **shall** emit a structured JSON log: cycle_id, analyses computed, surfaced (high-conviction, post-filter) count, suppressed counts by reason, duration, warnings.
*Verification:* log inspection.

**REQ-PE-LOG-002: Per-analysis log on Kelly-positive**
Every analysis with a positive Kelly **shall** be logged at INFO level with comparison_id, model_p, market_p, kelly_f, suggested_fraction. Kelly-zero or Kelly-negative analyses log at DEBUG.
*Verification:* log inspection after representative run.

## 7. Non-Functional Requirements

**NFR-PE-PERF-001:** A daily analysis cycle (v1 scale: ≤20 surfaced comparisons per cycle) **shall** complete within 1 minute on the operator's hardware.

**NFR-PE-AVAIL-001:** Engine failures **shall** degrade gracefully — `report_generator` and `monitor` consumers see absent or stale analyses rather than crashes.

**NFR-PE-DISK-001:** Engine tables **shall** stay under 100 MB out of the 100 GB global cap, given v1 scale and daily cadence over the first year.

**NFR-PE-DETERMINISM-001:** An analysis cycle against the same `mispricing_detector` snapshots and the same bankroll configuration **shall** produce identical analyses (excluding `cycle_id` and timestamps).

## 8. Open Questions (carry to design phase)

- **OQ-PE-001:** Whether to expose alternate Kelly fractions (e.g., quarter-Kelly, dynamic-Kelly with confidence weighting) as configuration. Default disposition: don't; half-Kelly is the conservative bound and adding more knobs invites the operator to dial up risk in subtle ways.
- **OQ-PE-002:** Liquidity-feasibility threshold default (5% of 24h volume) — validate against actual Polymarket market distributions.
- **OQ-PE-003:** Long-time-to-resolution threshold default (365 days). Empirical validation needed.
- **OQ-PE-004:** Whether to compute a "Kelly under model error" sensitivity analysis showing how the suggested fraction changes if model_p is varied by ±10%, ±20%. Useful but adds complexity. Default disposition: include in the analysis but only render in the verbose output mode.
- **OQ-PE-005:** Watch-state default expiration — should `'watching'` and `'acted_on'` states clear automatically when the underlying market resolves, or persist indefinitely? Decide in design.
- **OQ-PE-006:** Renderer linter for imperative language (REQ-PE-FRAME-002) — implement as a regex catalog of forbidden phrases or as a more principled check? Default: regex catalog with explicit phrase list.

## 9. Acceptance Criteria

The `position_engine` v1 is considered complete when:

- A daily analysis cycle runs end-to-end within NFR-PE-PERF-001.
- Bankroll configuration is persistable and update-trackable.
- Kelly and half-Kelly computations are correct across edge cases.
- Bankroll-survival, liquidity feasibility, time-to-resolution, and invalidation-criteria fields are populated for every analysis.
- Standard disclaimer block appears verbatim in every analysis.
- The renderer linter rejects analyses containing imperative language.
- Watch state mechanism works end-to-end.
- Failure isolation works.
- No order-placement code path exists in the codebase.

## 10. References

- LOOM v0.9.0 — `razorrooster.md`, subsystem registry entry for `position_engine`.
- `mispricing_detector` Requirements/Design/Tasks v0.1.0 — for surfaced comparisons.
- `polymarket_connector` Requirements/Design/Tasks v0.1.0 — for market metadata (volume, end_date, resolutions).
- `signal_scanner` Requirements/Design/Tasks v0.1.0 — for invalidation-criteria extraction from reasoning traces.
- Open thread OT-004 — confirmed: v1 is recommendation-only; this spec implements it.
- System prompt v0.2 — `razorrooster-prompt.md.txt` (educational framing; sizing is decision support, not directive).
