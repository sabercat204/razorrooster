# MISPRICING_DETECTOR — Design

**Subsystem:** `mispricing_detector`
**Codename:** The Liver
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14
**Companion spec:** `MISPRICING_DETECTOR.md` (Requirements v0.1.0)

---

## 1. Overview

`mispricing_detector` produces a structured comparison between model probabilities (from `signal_scanner`) and market-implied probabilities (from `polymarket_connector`). It is a thin layer architecturally — most of the heavy lifting is upstream — but the framing rules around how comparisons are presented, what's surfaced vs. suppressed, and how default-to-market disposition is encoded carry weight.

Discipline rules:

1. **Source-native preservation** — both upstream artifacts are persisted verbatim; comparisons are read-only over them.
2. **Failure isolation** — bad mapping or missing market does not stop the cycle.
3. **No silent comparison** — every comparison carries provenance; suppressed comparisons log their suppression reason.
4. **Default-to-market disposition** — when in doubt, the trace makes the case that the market may be right with at least equal weight as the case for the model.
5. **No directives** — comparisons describe disagreements, never recommend action.

## 2. Resolved Open Questions

### OQ-MD-001 — Auto-mapping confidence heuristics

**Resolution:**
- `inferred`: Polymarket market's auto-derived `razor_sector` matches class's `domain_sector` AND ≥3 keyword overlaps between market question/description and class title/description AND market question contains a temporal qualifier consistent with class's resolution semantics (e.g. "in 2026," "by year-end").
- `low`: sector match only, fewer than 3 keyword overlaps OR no temporal alignment check possible.
- Anything weaker is not auto-mapped at all.

**Design implications:**
- A small `mapping/auto_heuristic.py` module computes confidence per pair.
- Keyword overlap uses a simple stemmer + stopword list; not ML.
- Temporal qualifier detection looks for date phrases via a small regex catalog.

### OQ-MD-002 — Mapping polarity

**Resolution:** Add a `polarity` column (`'aligned'` or `'inverted'`) to `class_market_mappings`. Default `'aligned'`. Operator must explicitly mark inverted mappings (e.g. "will X NOT happen" market mapped to a "X happens" event class).

**Reasoning:** Forcing all mappings into the model-event-direction would silently lose information. Polymarket markets are written in natural language and inversion is common. Encoding polarity explicitly keeps the comparison math correct: when polarity is `'inverted'`, the comparison computes `1 − market_implied_p` before deltaing against model_p.

**Design implications:**
- `class_market_mappings.polarity VARCHAR NOT NULL DEFAULT 'aligned'`.
- Comparison computation accounts for polarity.
- CLI mapping command accepts `--polarity inverted` flag.
- Auto-mapping never sets polarity; operator must override to inverted manually. (Auto-detecting "not" in market questions is too error-prone.)

### OQ-MD-003 — Liquidity floor

**Resolution:** Default $10,000 24h volume. Configurable per sector. Acceptance test (T-MD-080) measures empirical Polymarket volume distribution in mapped markets and adjusts default if needed.

**Design implications:**
- `config/mispricing.yaml` `liquidity_floors` per sector.
- Not all sectors will have liquid Polymarket markets; for those, the floor may be lower or `null` (skip the check).

### OQ-MD-004 — Surfacing prioritization

**Resolution:** Sort surfaced comparisons by `confidence_weighted_score = |log_odds_delta| × signature_confidence × (1 − low_liquidity_penalty)`, where the penalty linearly scales the score from 0 (zero volume) to 1 (volume above floor).

**Reasoning:** A confident model with a large delta on a liquid market is the operator's most-actionable signal; raw |delta| alone over-prioritizes low-confidence noise on illiquid markets. The score is a heuristic — exposed in the comparison record, used by `report_generator` for ranking.

**Design implications:**
- `comparisons.confidence_weighted_score DOUBLE NOT NULL` for surfaced comparisons, NULL for suppressed.
- Score is computed at comparison time and persisted; not a query-time ranking.

### OQ-MD-005 — Calibration linkage timing

**Resolution:** Lazy linkage at resolution-time, but driven by a per-cycle background pass rather than push from `polymarket_connector`.

**Mechanism:** Each comparison cycle includes a "linkage pass": for each `polymarket_resolutions` row whose `resolution_ts > last_linkage_ts`, find any `comparisons` referencing the resolved market and write `comparison_resolutions` rows. The pass is idempotent and re-runs on every cycle, so missed resolutions catch up automatically. This avoids tight coupling between `polymarket_connector` and `mispricing_detector`.

**Design implications:**
- `mispricing_detector_state.last_linkage_ts` tracks progress.
- The linkage pass is part of `razor-rooster mispricing run`, runs after the comparison pass.
- Decoupled architecture: `polymarket_connector` doesn't need to know about `mispricing_detector`.

### OQ-MD-006 — Expected-value computation

**Resolution:** Compute and persist EV per comparison, but do not surface it in default `report_generator` output. Operator opts in via `--show-ev` flag if they want to see it.

**Reasoning:** EV is operationally useful in calibration analysis (well-calibrated probabilities should produce EV-realistic outcomes over many comparisons) but its presence in cycle reports nudges toward "this is a trade." Persisting without default rendering preserves the analytical value while keeping the reports framing-aligned.

**Design implications:**
- `comparisons.expected_value DOUBLE NULL` populated for binary YES markets where market price ≠ 0 or 1.
- `report_generator` reads it but skips rendering unless flagged.
- The calibration meta-class in `pattern_library` reads EV for analysis.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      mispricing_detector/
        __init__.py
        cli.py                              # commands: run, map, unmap, list-mappings, show
        engines/
          __init__.py
          comparator.py                     # main comparison cycle
          mapping_resolver.py               # decides which mappings produce comparisons
          delta.py                          # probability + log-odds + EV math
          ci_overlap.py                     # CI overlap analysis
          surfacing.py                      # surfacing logic
          linkage.py                        # OQ-MD-005 lazy resolution linkage
          trace.py                          # comparison trace builder + renderer
        mapping/
          __init__.py
          auto_heuristic.py                 # OQ-MD-001 confidence heuristics
          operator_overrides.py             # CLI-driven operator mappings
        persistence/
          __init__.py
          schemas.py
          migrations/
            m0001_mispricing_detector_initial.py
        config/
          mispricing.yaml
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- `data_ingest`: DuckDBStore, staging-merge, structured logging.
- `signal_scanner.scan_records` and `scan_traces`: model probability source.
- `polymarket_connector` tables: market metadata, price snapshots, sector mapping, resolutions.
- `pattern_library.library` facade: class registry lookups (for class metadata in traces).

### 3.3 Tables

#### `class_market_mappings`

    mapping_id                    VARCHAR     PRIMARY KEY    -- UUID
    class_id                      VARCHAR     NOT NULL
    condition_id                  VARCHAR     NOT NULL
    mapping_type                  VARCHAR     NOT NULL          -- 'direct' | 'proxy' | 'aggregate'
    mapping_confidence            VARCHAR     NOT NULL          -- 'exact' | 'inferred' | 'low'
    polarity                      VARCHAR     NOT NULL DEFAULT 'aligned'    -- OQ-MD-002
    mapped_by                     VARCHAR     NOT NULL          -- 'operator' | 'auto'
    mapped_at                     TIMESTAMP   NOT NULL
    removed_at                    TIMESTAMP   NULL
    notes                         TEXT        NULL

Unique active mapping: a unique constraint on `(class_id, condition_id)` where `removed_at IS NULL`.
Indexes: `(class_id)`, `(condition_id)`, `(mapping_confidence)`.

#### `comparison_cycles`

    cycle_id                      VARCHAR     PRIMARY KEY
    started_at                    TIMESTAMP   NOT NULL
    completed_at                  TIMESTAMP   NULL
    comparisons_total             INTEGER     NOT NULL
    surfaced_count                INTEGER     NOT NULL
    suppressed_breakdown          JSON        NOT NULL    -- { reason → count }
    library_version_at_cycle      INTEGER     NOT NULL
    scan_id_consumed              VARCHAR     NOT NULL    -- reference to scan_summaries.scan_id

#### `comparisons`

    comparison_id                 VARCHAR     PRIMARY KEY
    cycle_id                      VARCHAR     NOT NULL
    mapping_id                    VARCHAR     NOT NULL
    class_id                      VARCHAR     NOT NULL
    condition_id                  VARCHAR     NOT NULL
    outcome_token_id              VARCHAR     NOT NULL          -- which side of binary market is the YES side
    polarity                      VARCHAR     NOT NULL          -- copied from mapping at cycle time
    -- model side (from signal_scanner)
    scan_id                       VARCHAR     NOT NULL
    model_probability             DOUBLE      NOT NULL
    model_ci_lower                DOUBLE      NOT NULL
    model_ci_upper                DOUBLE      NOT NULL
    -- market side (from polymarket_connector)
    market_probability            DOUBLE      NULL
    market_best_bid               DOUBLE      NULL
    market_best_ask               DOUBLE      NULL
    market_last_trade_price       DOUBLE      NULL
    market_volume_24h             DOUBLE      NULL
    market_spread_bps             INTEGER     NULL
    market_snapshot_ts            TIMESTAMP   NULL
    -- comparison results
    delta                         DOUBLE      NULL              -- model - market (after polarity applied)
    log_odds_delta                DOUBLE      NULL
    ci_overlap                    BOOLEAN     NOT NULL
    expected_value                DOUBLE      NULL
    confidence_weighted_score     DOUBLE      NULL              -- NULL if suppressed
    -- surfacing
    surfaced                      BOOLEAN     NOT NULL
    suppression_reasons           JSON        NULL              -- list of reasons if not surfaced
    -- warning flags (carry-through and detector-specific)
    low_signature_confidence      BOOLEAN     NOT NULL DEFAULT FALSE
    source_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE
    library_stale_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    definition_drift_warning      BOOLEAN     NOT NULL DEFAULT FALSE
    stale_market_price            BOOLEAN     NOT NULL DEFAULT FALSE
    no_market_price               BOOLEAN     NOT NULL DEFAULT FALSE
    degenerate_orderbook          BOOLEAN     NOT NULL DEFAULT FALSE
    low_liquidity                 BOOLEAN     NOT NULL DEFAULT FALSE
    low_mapping_confidence        BOOLEAN     NOT NULL DEFAULT FALSE
    -- timestamps
    computed_at                   TIMESTAMP   NOT NULL

Indexes: `(cycle_id, surfaced)`, `(class_id, computed_at)`, `(condition_id, computed_at)`.

#### `comparison_traces`

    comparison_id                 VARCHAR     PRIMARY KEY    -- 1:1 with comparisons
    trace_json                    JSON        NOT NULL

#### `comparison_resolutions`

    comparison_id                 VARCHAR     NOT NULL
    condition_id                  VARCHAR     NOT NULL
    resolution_outcome            VARCHAR     NOT NULL          -- 'yes' | 'no' | 'invalid'
    resolution_ts                 TIMESTAMP   NOT NULL
    model_probability_at_comparison DOUBLE    NOT NULL
    market_probability_at_comparison DOUBLE   NULL
    polarity_at_comparison        VARCHAR     NOT NULL
    outcome_observed              INTEGER     NOT NULL          -- 1 or 0 for binary, accounting for polarity
    linked_at                     TIMESTAMP   NOT NULL

Primary key: `(comparison_id)`.
Indexes: `(condition_id)`, `(resolution_ts)`.

#### `mispricing_detector_state`

Single-row table tracking subsystem state (especially `last_linkage_ts`).

### 3.4 Comparison Cycle

`engines/comparator.py`:

    def run_cycle(class_id: str | None = None) -> CycleReport:
        cycle_id = uuid()
        scan_summary = signal_scanner.latest_scan()  # may be None if no scans run yet
        if scan_summary is None: raise NoScanAvailable

        # Resolve mappings to evaluate
        active_mappings = mapping_resolver.resolve(class_id_filter=class_id)
        active_mappings.extend(mapping_resolver.derive_auto_mappings())  # respects existing operator mappings

        write_cycle_started(cycle_id, scan_summary.scan_id, active_mappings)

        for mapping in active_mappings:
            try:
                cmp = compute_comparison(cycle_id, mapping, scan_summary)
                persist_comparison(cmp)
            except Exception as e:
                persist_comparison_error(cycle_id, mapping, e)
                # cycle continues (REQ-MD-CMP failure isolation)

        # Linkage pass
        run_linkage_pass()

        complete_cycle(cycle_id)

`compute_comparison`:

    def compute_comparison(cycle_id, mapping, scan_summary):
        scan_record = signal_scanner.scan_record(scan_summary.scan_id, mapping.class_id)
        market = polymarket_connector.market(mapping.condition_id)
        if market is None: raise MissingMarket(mapping)
        outcome_token = pick_yes_token(market)  # binary v1 only
        snapshot = polymarket_connector.latest_price(mapping.condition_id, outcome_token.id)

        market_p = market_probability_from(snapshot, mapping.polarity)
        market_p = adjust_for_polarity(market_p, mapping.polarity)
        delta = scan_record.posterior - market_p if market_p is not None else None
        log_odds_delta = log_odds(scan_record.posterior) - log_odds(market_p) if market_p else None
        ci_overlap = check_ci_overlap(scan_record, snapshot)
        ev = expected_value(scan_record.posterior, market_p) if market_p else None

        warnings = collect_warnings(scan_record, snapshot, mapping)
        surfaced, suppression_reasons = surfacing_decision(
            log_odds_delta, ci_overlap, warnings, mapping.mapping_confidence
        )
        score = confidence_weighted_score(...) if surfaced else None

        trace = trace.build(scan_record, snapshot, market, mapping, delta, log_odds_delta,
                            ci_overlap, warnings, surfaced)

        return Comparison(...)

### 3.5 Mapping Resolver

`engines/mapping_resolver.py`:

    def resolve(class_id_filter=None) -> list[Mapping]:
        # Active operator mappings
        operator = query_operator_mappings(class_id_filter)
        # Auto-derived mappings respect existing operator mappings
        auto = derive_auto_mappings(exclude=operator)
        return list(operator) + list(auto)

    def derive_auto_mappings(exclude) -> list[Mapping]:
        # For each (class, polymarket market) pair where:
        # - market is active and binary
        # - market's razor_sector matches class's domain_sector
        # - (class_id, condition_id) is not in exclude
        # - (class_id, condition_id) is not in tombstoned removed mappings
        # Compute confidence via auto_heuristic.py and emit if >= 'low'
        ...

`mapping/auto_heuristic.py`:

    def confidence(class_, market) -> str | None:
        if not sectors_match(class_.domain_sector, market.razor_sector):
            return None
        kw_overlap = keyword_overlap(class_.title + ' ' + class_.description,
                                      market.question + ' ' + (market.description or ''))
        temporal_aligned = has_temporal_qualifier(market.question, class_.base_rate_window_default)
        if kw_overlap >= 3 and temporal_aligned:
            return 'inferred'
        return 'low'

### 3.6 Surfacing Logic

`engines/surfacing.py`:

    def surfacing_decision(log_odds_delta, ci_overlap, warnings, mapping_confidence) -> tuple[bool, list[str]]:
        if log_odds_delta is None: return False, ['no_market_price']
        reasons = []
        if abs(log_odds_delta) < threshold_for_sector(...): reasons.append('delta_below_threshold')
        if ci_overlap: reasons.append('ci_overlap')
        for warn in critical_warnings:
            if warnings.get(warn): reasons.append(warn)
        if mapping_confidence == 'low': reasons.append('low_mapping_confidence')
        return (len(reasons) == 0, reasons)

### 3.7 Reasoning Trace

`engines/trace.py` builds a structured dict:

    {
      "class_id": "...",
      "condition_id": "...",
      "model_probability": 0.18,
      "model_ci": [0.07, 0.34],
      "market_probability": 0.05,
      "market_spread_bps": 120,
      "market_volume_24h": 25000,
      "ci_overlap": false,
      "delta": 0.13,
      "log_odds_delta": 1.43,

      "embedded_scanner_trace": { ... full scanner trace ... },

      "case_for_model": [
        "Precursor 'who_don_volume' fired hard with hit rate 0.65 (FPR 0.20)",
        ...
      ],
      "case_for_market": [
        "Market has $25k 24h volume and 120 bps spread, suggesting active price discovery",
        "Market price has been stable at this level for 3 days",
        ...
      ],
      "ambiguity_factors": [
        "Mapping is 'inferred' confidence; class question and market question may differ in interpretation",
        ...
      ],
      "warnings": [...],
      "suppression_reasons": [],
      "is_surfaced": true,
      "confidence_weighted_score": 1.18
    }

The renderer produces a text version where "case_for_model" and "case_for_market" sections appear as adjacent equal-prominence blocks (REQ-MD-TRACE-005).

### 3.8 Linkage Pass

`engines/linkage.py`:

    def run_linkage_pass():
        last_ts = state.last_linkage_ts or epoch
        new_resolutions = polymarket.resolutions_since(last_ts)
        for res in new_resolutions:
            comparisons = query_comparisons_for(res.condition_id)
            for cmp in comparisons:
                if not already_linked(cmp.comparison_id):
                    outcome_observed = compute_outcome_observed(res, cmp.polarity)
                    write_resolution_link(cmp, res, outcome_observed)
        state.last_linkage_ts = max(r.resolution_ts for r in new_resolutions) if new_resolutions else last_ts

The linkage pass is idempotent: running it twice produces no duplicates.

### 3.9 CLI

    razor-rooster mispricing run [--class <id>]
    razor-rooster mispricing show <comparison_id>                    # render trace
    razor-rooster mispricing list-comparisons [--surfaced-only] [--since ...]
    razor-rooster mispricing map <class_id> <condition_id> --type <t> [--polarity inverted] [--notes ...]
    razor-rooster mispricing unmap <mapping_id>
    razor-rooster mispricing list-mappings [filters]
    razor-rooster mispricing relink                                  # force a linkage pass

## 4. Threat Model

Threat context: STANDARD.

Risks:
1. **Bad mapping producing nonsense comparisons.** Mitigation: per-mapping isolation (REQ-MD-CMP failure isolation), `low_mapping_confidence` flag suppresses surfacing, operator can unmap.
2. **Stale market price treated as live.** Mitigation: REQ-MD-CMP-004 freshness check + flag.
3. **Polarity error.** Mitigation: explicit polarity column, default 'aligned' (the safe-but-possibly-wrong default), operator must opt in to inverted. The trace explicitly displays polarity so the operator catches errors visually.
4. **Surfacing-threshold overconfidence.** Mitigation: confidence floor on signatures + liquidity floor on markets + CI-overlap suppression all gate surfacing independently. Multiple checks must pass for a comparison to surface.
5. **Calibration linkage corruption.** Mitigation: idempotent linkage pass, composite-key deduplication, lazy linkage avoids tight coupling.

## 5. Test Strategy

### 5.1 Unit Tests

- Delta and log-odds math.
- CI overlap edge cases (touching intervals, point estimates equal).
- Surfacing decision per suppression reason.
- Polarity adjustment: inverted mapping correctly inverts market_p.
- EV computation.
- Auto-heuristic confidence levels with synthetic class/market pairs.
- Trace builder/renderer round-trip; both case-for-model and case-for-market sections present.

### 5.2 Integration Tests

- Full cycle against synthetic scan + polymarket fixtures with mixed mapping confidences. Comparisons produced, surfaced subset matches expected set per fixtures.
- Failure isolation: bad mapping (missing market), cycle completes with comparison error logged.
- Linkage pass: synthetic resolutions populated, linkage rows produced, idempotent.
- Re-run idempotency: two cycles same data produce two cycle_ids and two sets of comparisons (immutable, like scan records).
- Polarity test: inverted mapping with model_p=0.7 and market_p=0.3 produces delta=0.7 - (1-0.3)=0.0.

### 5.3 Acceptance Test

On operator hardware against the real upstream system:
- Daily cycle within NFR-MD-PERF-001.
- Surfacing distribution validated against OQ-MD-003 — measure liquidity floor effects.
- Disk usage under NFR-MD-DISK-001.

## 6. Operational Model

### 6.1 First setup

After upstream subsystems are populated:

    razor-rooster mispricing map pheic_declaration_12mo 0xABC... --type direct --notes "PHEIC declaration in 2026"
    # ... operator-curated mappings for each seed class
    razor-rooster mispricing run

### 6.2 Daily cadence

Cron / launchd: after `signal_scanner` cycle completes, run `razor-rooster mispricing run`. Auto-mappings refresh on every cycle.

### 6.3 Reviewing surfaced comparisons

    razor-rooster mispricing list-comparisons --surfaced-only --since 2026-05-01
    razor-rooster mispricing show <comparison_id>

The trace tells the operator both why the model differs and why the market may be right anyway. Operator decides whether to escalate to a deeper analysis.

## 7. Performance Notes

- v1 scale: ~40 comparisons per cycle. Each comparison is ~5 SQL queries plus in-process math. Sub-second per comparison; cycle in under a minute.
- Linkage pass scales with resolution-rate (Polymarket resolves a few markets per day on average); negligible.

## 8. Deferred to Implementation

- **DEFER-MD-001:** Empirical liquidity-floor validation; revise default after first month of cycles.
- **DEFER-MD-002:** Confidence-weighted-score weights — current formula is a heuristic; revisit if surfacing prioritization seems wrong in practice.
- **DEFER-MD-003:** Multi-outcome market support (deferred to v1.1 alongside `polymarket_connector` multi-outcome support).

## 9. References

- Requirements: `MISPRICING_DETECTOR.md` v0.1.0
- `signal_scanner` Requirements/Design/Tasks v0.1.0
- `polymarket_connector` Requirements/Design/Tasks v0.1.0
- `pattern_library` Requirements/Design/Tasks v0.1.0
- LOOM v0.8.0
- Open thread OT-003: addressed via REQ-MD-PERSIST-003 + linkage pass.
- Open thread OT-004: confirmed v1 is recommendation-only.
