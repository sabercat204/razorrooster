# MISPRICING_DETECTOR — Requirements

**Subsystem:** `mispricing_detector`
**Codename:** The Liver
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14

---

## 1. Purpose

`mispricing_detector` is the model-vs-market comparison layer. Its job, on each cycle:

1. For every active `signal_scanner` candidate situation, identify Polymarket markets that correspond to the same event class.
2. Read the live Polymarket price for those markets via `polymarket_connector` and convert to market-implied probability.
3. Compare the model probability (from `signal_scanner`) to the market-implied probability and compute the delta.
4. Produce a *comparison* output — a structured record describing the disagreement, with full reasoning trace, that the operator can read and reason about.
5. Log every comparison to a persistent record so the calibration backtest (OT-006) can later evaluate whether the model's stated probabilities matched observed outcome frequencies.

The phrase "mispricing detector" — the legacy name from the LOOM — should be read in the educational-framing sense: the subsystem detects *disagreements between model and market*, not "the market is wrong here." Treating the market as default-correct is one of the system's stated principles. Comparisons surface evidence; the operator decides what to do with it.

Downstream consumers:
- `position_engine` reads comparisons and produces sizing analyses for the operator.
- `report_generator` reads comparisons to populate the cycle report's "top analyses" section.
- `monitor` reads comparisons for active analyses to detect when market or model has moved enough to warrant re-evaluation.
- The calibration backtest reads logged comparisons against eventual Polymarket resolutions.

`mispricing_detector` does not place orders, recommend position sizes (that's `position_engine`'s job), or itself decide whether the model or the market is correct. It surfaces the data and the reasoning.

## 2. Scope

### In scope (v1)

- A comparison cycle that, for each `signal_scanner` candidate, finds Polymarket markets in the same event class and computes the model-vs-market delta.
- Mapping logic: which Polymarket markets correspond to which `pattern_library` event classes. Combines `polymarket_sector_mapping` (from `polymarket_connector`) with explicit operator-curated class-to-market mappings for tighter coupling where it exists.
- Comparison output structure: a per-comparison record with model probability, market-implied probability, delta, confidence interval overlap analysis, reasoning trace, and warnings.
- A "default-to-market" disposition: when model and market disagree, the comparison record explicitly flags possible reasons the model could be missing information, not just possible reasons the market could be wrong.
- Persistence of every comparison for calibration backtest input.
- Configurable delta thresholds for surfacing — small disagreements are persisted but only large ones bubble up to the operator's attention.
- Source-stale and library-stale propagation through to comparison outputs.
- Failure isolation: a bad mapping or a missing market doesn't kill the cycle.

### Out of scope (explicit)

- **Position sizing or recommendations.** `position_engine` does that.
- **Order placement.** Out of v1 scope (OT-004).
- **Live continuous monitoring of comparisons.** v1 is batch, daily-cadence.
- **Adjusting the model probability based on the market.** The model is what `signal_scanner` produced. The comparison is independent. Auto-adjusting model output based on market price would defeat the calibration check.
- **Multi-outcome Polymarket markets.** v1 is binary-only (per `polymarket_connector` design OQ-PMC-004). Multi-outcome comparisons are a v1.1 concern.
- **Cross-platform comparison (Kalshi, PredictIt, etc.).** Polymarket-only in v1.

## 3. Operating Assumptions

- **Cadence:** runs daily after `signal_scanner` completes. Operator can run additional ad-hoc comparisons.
- **Data dependencies:** requires fresh `signal_scanner` scan output AND fresh `polymarket_connector` market metadata + price snapshots. If either is stale beyond a configurable threshold, comparison outputs are flagged.
- **Mapping discipline:** each comparison must explicitly reference how the mapping (event class ↔ market) was established. Vague or heuristic mappings produce comparisons with lower confidence flags than operator-curated mappings.
- **Market-default disposition:** in any case where the model and market disagree, the trace must describe possible reasons *both* could be wrong. The convention is to consider market-correctness as the default explanation for a delta.

## 4. Conceptual Model

### 4.1 Class-to-Market Mapping

A *class-to-market mapping* connects a `pattern_library` event class to one or more Polymarket markets. Mappings have:

- A type: `direct` (1:1 — the market resolves to the same event the class describes), `proxy` (the market is a proxy for the class — same domain, different specific question), or `aggregate` (multiple markets together approximate the class).
- A confidence level: `exact` (operator-asserted, semantically precise), `inferred` (derived from sector heuristics in `polymarket_sector_mapping`), or `low` (only sector-level coincidence).
- A `mapped_at` timestamp and `mapped_by` ('operator' or 'auto').

Comparisons require a class-to-market mapping. v1 uses operator-curated mappings as the primary source; auto-derived mappings (sector-level matches) only generate comparisons flagged `low_mapping_confidence`.

### 4.2 Comparison

A *comparison* is the output of comparing one event class's model probability to one Polymarket market's price:

- The class and market identifiers.
- Mapping metadata (type, confidence).
- Model probability with credible interval (from `signal_scanner`).
- Market-implied probability with bid/ask spread information (from `polymarket_connector` price snapshot).
- Delta (model − market, in probability units).
- Log-odds delta (a more analytically useful scale).
- CI overlap: do the model's credible interval and the market's bid-ask range overlap? If yes, "no material disagreement" is a defensible read.
- Reasoning trace: full decomposition of how the model probability was derived, plus possible explanations for the delta.
- Warning flags carried from `signal_scanner` (low confidence, source stale, library stale, definition drift) plus mispricing-specific flags (stale market price, wide bid-ask spread, low market volume).
- A surfacing flag: `surfaced` if the |delta| exceeds the configurable threshold AND CI overlap is absent AND no critical warnings are set.

### 4.3 Reasoning Trace

A reasoning trace explains the comparison. It includes:

- The full `signal_scanner` reasoning trace embedded.
- The market price and the market's recent activity (volume, last trade, spread).
- Possible reasons for the delta in three categories:
  - "Model has information the market may not have priced" — derived from precursor variables that fired hard.
  - "Market may have information the model doesn't see" — derived from market-side context (recent trade volume spikes, news-driven moves) when surfaceable.
  - "Both could be reasonable; the question itself may be ambiguous" — derived from mapping confidence and CI overlap.

Traces are intentionally not prescriptive. They lay out the considerations the operator should weigh.

### 4.4 Calibration Log

The calibration log is the persistent record of every comparison ever produced. When the corresponding Polymarket market resolves, a follow-up record links the comparison to the resolution outcome, enabling the calibration backtest (OT-006).

## 5. Functional Requirements

Requirements use EARS-style phrasing. MD = `mispricing_detector`.

### 5.1 Class-to-market mapping

**REQ-MD-MAP-001: Mapping registry**
The detector **shall** maintain a `class_market_mappings` table with: `mapping_id`, `class_id`, `condition_id`, `mapping_type`, `mapping_confidence`, `mapped_by`, `mapped_at`, `notes`.
*Verification:* schema migration; round-trip test.

**REQ-MD-MAP-002: Operator mapping CLI**
The operator **shall** be able to register a mapping via `razor-rooster mispricing map <class_id> <condition_id> --type <type> [--notes ...]`. Operator-set mappings have `mapping_confidence = 'exact'` and `mapped_by = 'operator'`.
*Verification:* CLI integration test.

**REQ-MD-MAP-003: Auto-mapping derivation**
On each comparison cycle, before producing comparisons, the detector **shall** derive auto-mappings for `class_id` ↔ `condition_id` pairs where the Polymarket market's `razor_sector` (from `polymarket_sector_mapping`) matches the class's `domain_sector`. Auto-derived mappings have `mapping_confidence = 'inferred'` or `'low'` based on additional heuristics (keyword match in market question vs. class title) and never overwrite operator mappings.
*Verification:* unit test: operator mapping survives an auto-mapping pass; auto-mapping correctly tags confidence.

**REQ-MD-MAP-004: Mapping queryability**
The detector **shall** provide `razor-rooster mispricing list-mappings [--class ...] [--market ...] [--confidence ...]`.
*Verification:* CLI integration test.

**REQ-MD-MAP-005: Mapping override and removal**
The operator **shall** be able to remove a mapping via `razor-rooster mispricing unmap <mapping_id>`. Removed mappings are soft-deleted (`removed_at` timestamp) for audit; they do not produce new comparisons.
*Verification:* unit test: unmapped pair does not produce a comparison.

### 5.2 Comparison computation

**REQ-MD-CMP-001: Comparison cycle**
The detector **shall** provide `razor-rooster mispricing run` that, for every (active class candidate from `signal_scanner`, mapped market) pair, computes a comparison record.
*Verification:* CLI integration test against synthetic candidates and mappings.

**REQ-MD-CMP-002: Per-class scope**
The detector **shall** support `razor-rooster mispricing run --class <class_id>` for a single-class run.
*Verification:* CLI integration test.

**REQ-MD-CMP-003: Model-probability source**
A comparison **shall** use the most recent `signal_scanner` scan record for the class, regardless of whether that record was a candidate. The "is_candidate" status of the source record is reflected in the comparison record but does not gate computation.
*Verification:* unit test confirms non-candidate scan records still produce comparisons.

**REQ-MD-CMP-004: Market-probability source**
A comparison **shall** use the most recent `polymarket_price_snapshots` row for the mapped market and outcome token. If the price is older than a configurable freshness threshold (default: 12 hours), the comparison **shall** be flagged `stale_market_price` and surfacing **shall** be suppressed regardless of delta size.
*Verification:* unit test simulates stale price; comparison flagged and not surfaced.

**REQ-MD-CMP-005: Probability derivation from market price**
The market-implied probability for a binary YES outcome **shall** be the mid-price between best bid and best ask. When best bid or best ask is missing, the comparison **shall** use the last_trade_price and flag `degenerate_orderbook`. When all of these are NULL, the comparison **shall** record `market_probability = NULL` and flag `no_market_price` and surfacing **shall** be suppressed.
*Verification:* unit tests for normal, missing-side, and fully-missing cases.

**REQ-MD-CMP-006: Delta computation**
A comparison **shall** include: `delta = model_probability - market_probability`, `log_odds_delta = log_odds(model) - log_odds(market)`. Both are stored.
*Verification:* unit test confirms math correctness.

**REQ-MD-CMP-007: CI-overlap analysis**
A comparison **shall** compute whether the model's credible interval and the market's spread (bid-to-ask range) overlap. If yes, the comparison record sets `ci_overlap = TRUE` and the trace describes this as "model and market are not in material disagreement at current uncertainty levels."
*Verification:* unit test for overlapping and non-overlapping cases.

**REQ-MD-CMP-008: Surfacing logic**
A comparison **shall** be flagged `surfaced = TRUE` when ALL of the following hold:
- `|log_odds_delta|` exceeds the configurable per-sector threshold (default: 0.5).
- `ci_overlap = FALSE`.
- No critical warnings set (`stale_market_price`, `no_market_price`, `low_mapping_confidence`, `low_signature_confidence`, `library_stale_warning`).
- Mapping confidence is `'exact'` or `'inferred'` (not `'low'`).

If `surfaced = FALSE`, the comparison is still persisted; it just doesn't bubble up to operator attention.
*Verification:* unit tests for the each suppression case.

### 5.3 Reasoning traces

**REQ-MD-TRACE-001: Trace contents**
Every comparison **shall** include a reasoning trace covering the four trace categories from §4.3.
*Verification:* schema test; format test.

**REQ-MD-TRACE-002: Embedded scanner trace**
The `signal_scanner` reasoning trace for the underlying scan record **shall** be embedded in the comparison's trace verbatim. This preserves the chain of reasoning from raw data → model probability → comparison.
*Verification:* unit test confirms scanner trace fields visible in comparison trace.

**REQ-MD-TRACE-003: Market-context fields**
The trace **shall** include, when available: market 24h volume, recent trade count, bid-ask spread in basis points, time since last trade. These contextual fields help the operator evaluate whether the market price is well-supported.
*Verification:* unit test confirms fields populated when source data has them; gracefully NULL when not.

**REQ-MD-TRACE-004: Renderability**
The trace **shall** be renderable to human-readable text via a documented function. Output format compatible with `report_generator`.
*Verification:* unit test renders representative trace.

**REQ-MD-TRACE-005: Default-to-market framing**
When the model and market disagree, the trace **shall** include a "Possible reasons the market may be right" section in addition to "Possible reasons the model may be right," not less prominent than the latter.
*Verification:* trace renderer output inspection: both sections present and roughly equal in prominence.

### 5.4 Persistence

**REQ-MD-PERSIST-001: Comparison tables**
The detector **shall** persist outputs to a `comparisons` table (one row per (cycle, mapping) pair) and a `comparison_traces` table (one JSON-blob trace per comparison) and a `comparison_cycles` table (one row per cycle execution).
*Verification:* schema migration; round-trip test.

**REQ-MD-PERSIST-002: Time-series retention**
Historical comparisons **shall** be retained indefinitely. Auto-pruning is not implemented in v1.
*Verification:* repeated cycles accumulate comparisons.

**REQ-MD-PERSIST-003: Resolution linkage (calibration scaffolding)**
When a Polymarket market resolves (via `polymarket_resolutions`), the detector **shall** automatically link any comparisons referencing that market to the resolution by writing to a `comparison_resolutions` table: `comparison_id`, `condition_id`, `resolution_outcome` (YES/NO/INVALID), `resolution_ts`, `model_probability_at_comparison`, `outcome_observed`. This is the data the calibration backtest reads.
*Verification:* simulated resolution flow: comparison made → market resolves → linkage row appears.

### 5.5 Configuration

**REQ-MD-CONFIG-001: Per-sector surfacing thresholds**
The detector **shall** read surfacing thresholds from `config/mispricing.yaml` per sector. Defaults: log-odds delta of 0.5; tunable.
*Verification:* config-driven test.

**REQ-MD-CONFIG-002: Market price freshness threshold**
The market-price freshness threshold (REQ-MD-CMP-004) **shall** be configurable. Default: 12 hours (twice the `polymarket_connector` snapshot cadence).
*Verification:* config-driven test.

**REQ-MD-CONFIG-003: Liquidity floor**
The detector **shall** support a configurable minimum 24h volume threshold below which comparisons are flagged `low_liquidity` and surfacing is suppressed. Default: $10,000 24h volume.
*Verification:* config-driven test.

### 5.6 Logging & observability

**REQ-MD-LOG-001: Structured cycle log**
Each comparison cycle **shall** emit a structured JSON log: `cycle_id`, comparisons computed, surfaced count, suppressed counts (broken down by suppression reason), duration, warnings.
*Verification:* log inspection.

**REQ-MD-LOG-002: Per-comparison log on surface**
Every surfaced comparison **shall** be logged at INFO level with class_id, condition_id, model_p, market_p, delta. Non-surfaced are logged at DEBUG.
*Verification:* log inspection after representative run.

## 6. Non-Functional Requirements

**NFR-MD-PERF-001:** A daily comparison cycle (v1 scale: ≤8 seed classes × ≤5 mapped markets each = ≤40 comparisons) **shall** complete within 2 minutes on the operator's hardware.

**NFR-MD-AVAIL-001:** Detector failures **shall** degrade gracefully — `report_generator` and `position_engine` consumers see an absent or stale set of comparisons rather than crashes.

**NFR-MD-DISK-001:** Detector tables **shall** stay under 200 MB out of the 100 GB global cap, given v1 scale and daily cadence over the first year.

**NFR-MD-DETERMINISM-001:** A comparison cycle against the same `signal_scanner` and `polymarket_connector` snapshots **shall** produce identical comparisons (excluding cycle_id and timestamps).

## 7. Open Questions (carry to design phase)

- **OQ-MD-001:** Auto-mapping confidence heuristics — `inferred` vs `low` distinction. Settle the keyword/title-similarity threshold that separates them.
- **OQ-MD-002:** When the market is binary YES/NO, the YES outcome maps to "event happens." But some Polymarket markets are framed inverted (e.g. a "will the rule NOT pass" question maps to event = "rule does not pass"). The mapping needs to encode polarity. Decide if this is an extra field on `class_market_mappings` or whether all mappings are required to be in the model-event-direction.
- **OQ-MD-003:** Liquidity floor default — $10k 24h volume is a guess. Validate against actual Polymarket market distributions in the design phase.
- **OQ-MD-004:** Surfacing prioritization — when many comparisons surface in a cycle, how does `report_generator` choose which to show first? Sort by |delta|, or by confidence-weighted delta, or by a configurable score? Design picks one.
- **OQ-MD-005:** Calibration linkage timing — link to resolution at resolution-time (immediate, requires monitoring) or on-demand when calibration backtest runs (lazy). Design picks one.
- **OQ-MD-006:** Whether to compute and expose an "expected value" number. EV is conventional in trading contexts but in the educational framing it can read as "trade this." If included, framing should be clinical and operator should explicitly opt in. Default disposition: include in the comparison record but render only when operator-requested in `report_generator`.

## 8. Acceptance Criteria

The `mispricing_detector` v1 is considered complete when:

- A daily comparison cycle runs end-to-end within NFR-MD-PERF-001.
- Operator-curated mappings are honored exactly; auto-mappings produce flagged comparisons.
- Comparisons correctly compute deltas and CI overlap.
- Surfacing logic suppresses comparisons with critical warnings.
- Reasoning traces include both "model may be right" and "market may be right" sections at equal prominence.
- Resolution linkage fires when markets resolve, populating calibration scaffolding.
- Failure isolation works: bad mapping or missing market doesn't kill the cycle.

## 9. References

- LOOM v0.8.0 — `razorrooster.md`, subsystem registry entry for `mispricing_detector`.
- `signal_scanner` Requirements/Design/Tasks v0.1.0 — for scan records and reasoning traces.
- `polymarket_connector` Requirements/Design/Tasks v0.1.0 — for market metadata, price snapshots, sector mapping, and resolutions.
- `pattern_library` Requirements/Design/Tasks v0.1.0 — for event class registry.
- Open thread OT-003 — addressed via REQ-MD-PERSIST-003 (resolution linkage as calibration scaffolding).
- Open thread OT-004 — confirmed disposition: v1 is recommendation-only, no order placement.
- System prompt v0.2 — `razorrooster-prompt.md.txt` (educational framing, default-to-market disposition).
