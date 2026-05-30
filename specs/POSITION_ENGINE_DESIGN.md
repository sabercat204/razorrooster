# POSITION_ENGINE — Design

**Subsystem:** `position_engine`
**Codename:** The Spur
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD (v1 paper-analysis only)
**Last updated:** 2026-05-14
**Companion spec:** `POSITION_ENGINE.md` (Requirements v0.1.0)

---

## 1. Overview

`position_engine` v1 is a paper-analysis subsystem. Architecturally it's small: read surfaced comparisons, run sizing math, render analyses with disclaimers, persist. The discipline that distinguishes it from a typical sizing engine is the framing constraint: every output uses conditional language, every output displays warnings before numbers, the renderer linter rejects imperative phrasing.

Discipline rules:

1. **Source-native preservation** — the engine reads but does not transform comparisons.
2. **Failure isolation** — bad comparison input produces flagged-incomplete analysis, not crash.
3. **No silent analysis** — every analysis carries provenance and warning flags prominently.
4. **Conditional language only** — outputs frame sizing as "if the operator chose to act."
5. **No execution code** — no Polymarket trading SDK, no signing, no wallet.

## 2. Resolved Open Questions

### OQ-PE-001 — Alternate Kelly fractions

**Resolution:** Half-Kelly only. No quarter-Kelly, no confidence-weighted Kelly.

**Reasoning:** Adding configurability invites the operator to dial up risk by lowering the divisor under various rationalizations. v1 is opinionated: the conservative default is half-Kelly. The operator can manually pick a smaller fraction in their head if they want; the engine doesn't help them pick a larger one.

**Design implications:**
- `kelly_fraction_default` config knob is bounded `[0, 0.5]`. Setting >0.5 fails validation with a documented error.

### OQ-PE-002 — Liquidity feasibility threshold

**Resolution:** 5% of 24h volume default. Per-sector configurable. Acceptance test measures empirical distributions and revises if the default is too tight or too loose.

**Design implications:**
- `config/position_engine.yaml` has per-sector overrides under `liquidity_feasibility`.
- Suggested-fraction adjustment is part of the analysis, with the un-adjusted fraction also stored.

### OQ-PE-003 — Long-time-to-resolution threshold

**Resolution:** 365 days as the threshold. Per-class override allowed.

**Reasoning:** Long-resolution markets carry compounding uncertainty (operator's model probability could be drift, market price could swing on news, the comparison's surfacing was a snapshot a year ago). 365 days is operator-experiential default; revisit if seed-class data suggests otherwise.

**Design implications:**
- Threshold per-class via `EventClass.long_resolution_days_threshold` optional field.

### OQ-PE-004 — Kelly-under-model-error sensitivity

**Resolution:** Include in every analysis, render only in verbose mode.

**Reasoning:** The sensitivity analysis (how does suggested fraction change if model_p ± 10% or ± 20%) is real-world useful. Defaulting to render-on-request keeps the standard analysis output focused without losing the information.

**Design implications:**
- `analyses.sensitivity_analysis JSON` populated by default.
- Renderer's normal mode skips the section; `--verbose` flag includes it.

### OQ-PE-005 — Watch state expiration

**Resolution:** Auto-clear `'watching'` and `'acted_on'` to `'expired'` when the underlying market resolves. Operator can re-set state explicitly post-resolution if they want to track retrospectively, but auto-clearing avoids the watched-list growing unbounded with stale entries.

**Design implications:**
- `watch_states` table has automatic expiry transitions handled in a per-cycle pass: when comparison_resolutions linkage fires, update watch state to `'expired'` for any active states on that analysis.
- `'expired'` is queryable separately via `list-expired`.

### OQ-PE-006 — Renderer linter

**Resolution:** Regex catalog with explicit phrase list, sourced from `config/forbidden_phrases.yaml`. Easy to extend; transparent about what's being rejected.

**Design implications:**
- `frame/linter.py` reads the YAML catalog and runs against rendered output.
- Linter failure raises `ImperativeLanguageDetected` with the offending phrase highlighted.
- Catalog seeded with: "you should buy", "you should sell", "I recommend", "the trade is", "buy this", "sell this", "go long", "go short", "take this position", and obvious variants.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      position_engine/
        __init__.py
        cli.py
        engines/
          __init__.py
          analyzer.py                    # main analysis cycle
          kelly.py                       # Kelly + half-Kelly + suggested fraction math
          bankroll.py                    # bankroll-survival scenarios
          liquidity.py                   # liquidity-feasibility computation
          invalidation.py                # invalidation-criteria extraction from scanner trace
          sensitivity.py                 # model-error sensitivity analysis
          time_to_resolution.py
        watch/
          __init__.py
          state.py                       # watch state operations
          expiration.py                  # auto-expire on resolution
        frame/
          __init__.py
          renderer.py                    # text rendering with conditional language
          linter.py                      # imperative-language linter
        persistence/
          __init__.py
          schemas.py
          migrations/
            m0001_position_engine_initial.py
        config/
          position_engine.yaml
          forbidden_phrases.yaml
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- `data_ingest`: DuckDBStore, staging-merge, structured logging.
- `mispricing_detector` tables: `comparisons`, `comparison_traces`, `comparison_resolutions`.
- `polymarket_connector` tables: `polymarket_markets` (for end_date, volume).
- `signal_scanner` tables: `scan_traces` (for invalidation-criteria extraction).
- `pattern_library` library facade: class metadata.

### 3.3 Tables

#### `bankroll_config`

    config_id                     VARCHAR     PRIMARY KEY    -- UUID
    analytical_bankroll_usd       DOUBLE      NOT NULL
    max_single_position_pct       DOUBLE      NOT NULL
    kelly_fraction_default        DOUBLE      NOT NULL
    min_edge_threshold            DOUBLE      NOT NULL
    effective_at                  TIMESTAMP   NOT NULL
    updated_by                    VARCHAR     NOT NULL    -- always 'operator' in v1

Latest config wins; query the most recent `effective_at`.

#### `analysis_cycles`

    cycle_id                      VARCHAR     PRIMARY KEY
    started_at                    TIMESTAMP   NOT NULL
    completed_at                  TIMESTAMP   NULL
    bankroll_config_id            VARCHAR     NOT NULL
    analyses_total                INTEGER     NOT NULL
    analyses_with_positive_kelly  INTEGER     NOT NULL
    analyses_clamped_by_cap       INTEGER     NOT NULL
    analyses_clamped_by_liquidity INTEGER     NOT NULL
    duration_seconds              DOUBLE      NULL

#### `analyses`

    analysis_id                   VARCHAR     PRIMARY KEY
    cycle_id                      VARCHAR     NOT NULL
    comparison_id                 VARCHAR     NOT NULL
    class_id                      VARCHAR     NOT NULL
    condition_id                  VARCHAR     NOT NULL
    -- inputs
    model_probability             DOUBLE      NOT NULL
    market_probability            DOUBLE      NOT NULL
    bankroll_config_id            VARCHAR     NOT NULL
    -- Kelly outputs
    kelly_unclamped               DOUBLE      NOT NULL
    kelly_negative                BOOLEAN     NOT NULL DEFAULT FALSE
    kelly_clamped_by_max_cap      BOOLEAN     NOT NULL DEFAULT FALSE
    kelly_clamped_by_liquidity    BOOLEAN     NOT NULL DEFAULT FALSE
    suggested_fraction            DOUBLE      NOT NULL
    suggested_dollar_size         DOUBLE      NOT NULL
    -- expected value (analytical)
    ev_per_dollar                 DOUBLE      NOT NULL
    -- bankroll survival
    bankroll_after_1_loss_pct     DOUBLE      NOT NULL
    bankroll_after_3_losses_pct   DOUBLE      NOT NULL
    bankroll_after_5_losses_pct   DOUBLE      NOT NULL
    -- liquidity
    suggested_pct_of_24h_volume   DOUBLE      NOT NULL
    -- time to resolution
    days_to_resolution            INTEGER     NULL
    long_time_to_resolution       BOOLEAN     NOT NULL DEFAULT FALSE
    -- sensitivity
    sensitivity_analysis          JSON        NULL
    -- invalidation
    invalidation_criteria         JSON        NOT NULL    -- list
    -- warning carry-through
    low_signature_confidence      BOOLEAN     NOT NULL DEFAULT FALSE
    source_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE
    library_stale_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    definition_drift_warning      BOOLEAN     NOT NULL DEFAULT FALSE
    low_mapping_confidence        BOOLEAN     NOT NULL DEFAULT FALSE
    low_liquidity                 BOOLEAN     NOT NULL DEFAULT FALSE
    -- meta
    computed_at                   TIMESTAMP   NOT NULL

Indexes: `(comparison_id)`, `(class_id, computed_at)`, `(cycle_id)`.

#### `analysis_traces`

    analysis_id                   VARCHAR     PRIMARY KEY
    rendered_text                 TEXT        NOT NULL    -- output that passed linter
    structured_dict               JSON        NOT NULL    -- machine-queryable form

#### `watch_states`

    state_id                      VARCHAR     PRIMARY KEY
    analysis_id                   VARCHAR     NOT NULL
    state                         VARCHAR     NOT NULL    -- 'watching' | 'acted_on' | 'dismissed' | 'expired'
    notes                         TEXT        NULL
    set_at                        TIMESTAMP   NOT NULL
    set_by                        VARCHAR     NOT NULL    -- 'operator' | 'system' (for expiry)

Index: `(analysis_id, set_at DESC)` for "latest state per analysis" queries.

### 3.4 Analysis Cycle

`engines/analyzer.py`:

    def run_cycle(include_suppressed=False) -> CycleReport:
        cycle_id = uuid()
        config = bankroll_config.latest()
        write_cycle_started(cycle_id, config.id)

        comparisons = mispricing_detector.surfaced_comparisons_since(state.last_analysis_ts)
        if include_suppressed:
            comparisons.extend(mispricing_detector.suppressed_comparisons_since(state.last_analysis_ts))

        for cmp in comparisons:
            try:
                analysis = analyze_comparison(cycle_id, cmp, config)
                trace_text = frame.render(analysis)
                frame.linter.check(trace_text)  # raises if imperative language found
                persist_analysis(analysis, trace_text)
            except Exception as e:
                persist_analysis_error(cycle_id, cmp, e)

        # Expiration pass
        run_expiration_pass()

        complete_cycle(cycle_id)
        state.last_analysis_ts = utcnow()

`analyze_comparison`:

    def analyze_comparison(cycle_id, cmp, config) -> Analysis:
        if abs(cmp.delta) < config.min_edge_threshold:
            return Analysis(... sub_threshold=True ...)

        # Kelly math
        b = (1.0 / cmp.market_probability) - 1.0
        f_unclamped = (cmp.model_probability * b - (1 - cmp.model_probability)) / b
        kelly_negative = f_unclamped < 0
        kelly_clamped = max(0.0, f_unclamped)

        # Apply half-Kelly default
        suggested = config.kelly_fraction_default * kelly_clamped

        # Cap by max_single_position_pct
        clamped_by_cap = suggested > config.max_single_position_pct
        suggested = min(suggested, config.max_single_position_pct)

        # Liquidity feasibility
        market = polymarket.market(cmp.condition_id)
        suggested_dollars = config.analytical_bankroll_usd * suggested
        pct_volume = suggested_dollars / market.volume_24h if market.volume_24h > 0 else inf
        clamped_by_liquidity = pct_volume > liquidity_threshold(...)
        if clamped_by_liquidity:
            suggested_dollars = market.volume_24h * liquidity_threshold(...)
            suggested = suggested_dollars / config.analytical_bankroll_usd

        # Bankroll survival
        survival = compute_bankroll_survival(suggested)

        # Time to resolution
        days_remaining = (market.end_date - now).days if market.end_date else None
        long_resolution = days_remaining and days_remaining > long_resolution_threshold(class_id)

        # Invalidation criteria
        invalidation = invalidation_criteria_from_scanner_trace(scan_trace, cmp, config)

        # Sensitivity (model error)
        sensitivity = compute_sensitivity(cmp, config)

        # EV
        ev = expected_value(cmp.model_probability, cmp.market_probability)

        return Analysis(...)

### 3.5 Renderer

`frame/renderer.py`:

```python
TEMPLATE = """
═══════════════════════════════════════════════
ANALYSIS: {class_title}
SECTOR: {sector}
═══════════════════════════════════════════════

WARNINGS:
{warnings_block}

SOURCE COMPARISON:
  Model probability: {model_p:.3f}  (CI: [{model_ci_lower:.3f}, {model_ci_upper:.3f}])
  Market-implied probability: {market_p:.3f}  (spread: {spread_bps} bps)
  Delta: {delta:+.3f}  (log-odds: {log_odds_delta:+.2f})

SIZING ANALYSIS (if the operator chose to act):
  Kelly fraction (theoretical maximum): {kelly_unclamped:.3f}
  Suggested fraction (half-Kelly, conservative): {suggested:.3f}
  Suggested dollar size: ${suggested_dollars:.0f} of ${bankroll:.0f} analytical bankroll
  This represents {pct_volume:.1%} of the market's 24h volume.

  {clamping_notes}

BANKROLL-SURVIVAL SCENARIOS:
  After 1 adverse outcome: bankroll at {bankroll_1:.1%} of starting
  After 3 adverse outcomes: bankroll at {bankroll_3:.1%}
  After 5 adverse outcomes: bankroll at {bankroll_5:.1%}

EXPECTED VALUE (analytical metric, not a recommendation):
  EV per dollar (if held to resolution): ${ev:.3f}

INVALIDATION CRITERIA:
{invalidation_lines}

TIME TO RESOLUTION:
  {days_to_resolution} days remaining{long_resolution_caveat}

{sensitivity_block_if_verbose}

DISCLAIMER:
  This is decision-support analysis. Kelly figures are theoretical optima before
  accounting for model error, transaction costs, slippage, and the possibility that
  the model is wrong. Half-Kelly is the conservative default and should still be
  considered an upper bound. The system does not place orders; the operator decides
  whether and how to act, and is responsible for any real-world outcomes.

═══════════════════════════════════════════════
""".strip()
```

The renderer fills in the template; the linter (`frame/linter.py`) checks the output against `forbidden_phrases.yaml` and raises if any phrase is found.

### 3.6 Watch State

`watch/state.py`:

    def set_state(analysis_id, new_state, notes, set_by='operator'):
        # New row is appended; latest row wins for queries
        ...

    def latest_state(analysis_id) -> WatchState | None:
        ...

`watch/expiration.py`:

    def run_expiration_pass():
        # For each comparison_resolution row whose analysis has an active 'watching' or 'acted_on' state
        # set state to 'expired' (set_by='system')
        ...

Run as part of the cycle.

### 3.7 CLI

    razor-rooster position-engine config --bankroll <usd> [--max-pct <p>] [--kelly-fraction <f>] [--min-edge <e>] [--no-prompt --acknowledge-analytical]
    razor-rooster position-engine run [--include-suppressed]
    razor-rooster position-engine analyze <comparison_id>
    razor-rooster position-engine show <analysis_id> [--verbose]
    razor-rooster position-engine list [--watched | --acted-on | --dismissed | --expired]
    razor-rooster position-engine watch <analysis_id> --note "..."
    razor-rooster position-engine acted-on <analysis_id> --note "..."
    razor-rooster position-engine dismiss <analysis_id> --reason "..."

### 3.8 Threat Model

Threat context: STANDARD.

Risks:
1. **Imperative language slipping into output.** Mitigation: linter on every render; refusal-to-ship if forbidden phrase found.
2. **Operator confusing analytical bankroll for tracked capital.** Mitigation: standard disclaimer in every analysis + confirmation-required on bankroll-config update.
3. **Order-placement code path leaking in.** Mitigation: code review checklist explicitly forbids importing Polymarket trading SDK; the codebase contains no signing libs in `pyproject.toml`.
4. **Bad comparison input crashing analyzer.** Mitigation: per-comparison try/except, error analysis persisted with explanation.
5. **Sub-threshold deltas producing noise.** Mitigation: `min_edge_threshold` filter (default 0.03) suppresses sub-noise analyses early.
6. **Operator dialing up risk via configuration.** Mitigation: `kelly_fraction_default` validated to `[0, 0.5]`; `max_single_position_pct` validated to `[0, 0.25]` (25% max even if operator sets it that high; the engine refuses higher).

When v2+ adds order placement, threat context for those paths returns to FULL and the spec amendment specifies wallet handling, custody, signing, and operator-side authorization.

## 4. Test Strategy

### 4.1 Unit Tests

- Kelly math edge cases.
- Bankroll-survival calculations.
- Liquidity-feasibility clamping.
- Invalidation-criteria extraction from synthetic scanner traces.
- Sensitivity-analysis math.
- Renderer template fills correctly across input shapes.
- Linter rejects every phrase in the seed forbidden list.
- Linter passes on standard-format outputs.
- Watch state transitions.

### 4.2 Integration Tests

- Full cycle against synthetic mispricing comparisons. All seeds analyzed.
- Failure isolation: bad comparison crashes per-comparison handler, cycle continues.
- Linter failure: deliberately crafted output triggers linter rejection (proves it's wired).
- Sub-threshold delta: analysis marked sub-threshold, no Kelly math performed.
- Long-resolution market: caveat appears in rendered output.
- Watch state expiration: synthetic resolution triggers expiration pass.

### 4.3 Acceptance Test

On operator hardware against real upstream system:
- Daily cycle within NFR-PE-PERF-001.
- Disk usage under NFR-PE-DISK-001.
- Empirical liquidity-threshold validation.

## 5. Operational Model

### 5.1 First setup

    razor-rooster position-engine config --bankroll 1000 --max-pct 0.05 --kelly-fraction 0.5 --min-edge 0.03

Engine prompts for confirmation that this is an analytical figure.

### 5.2 Daily cadence

After `mispricing_detector` cycle: `razor-rooster position-engine run`.

### 5.3 Reviewing analyses

    razor-rooster position-engine list --watched
    razor-rooster position-engine show <analysis_id>
    razor-rooster position-engine show <analysis_id> --verbose    # includes sensitivity

The operator decides whether to act; if so, marks the analysis `acted-on`. Otherwise dismisses or watches.

## 6. Performance Notes

- v1 scale: ≤20 analyses per cycle; sub-second math each. Cycle in well under a minute.

## 7. Deferred to Implementation

- **DEFER-PE-001:** Empirical liquidity-threshold validation; revise after first month.
- **DEFER-PE-002:** Long-resolution threshold per-class tuning.
- **DEFER-PE-003:** Whether to include time-to-resolution decay in EV computation. v1 doesn't; this is conservative (treats long-dated and short-dated EV equivalently).

## 8. References

- Requirements: `POSITION_ENGINE.md` v0.1.0
- `mispricing_detector` Requirements/Design/Tasks v0.1.0
- `polymarket_connector` Requirements/Design/Tasks v0.1.0
- `signal_scanner` Requirements/Design/Tasks v0.1.0
- LOOM v0.9.0
- Open thread OT-004: confirmed and embedded.
