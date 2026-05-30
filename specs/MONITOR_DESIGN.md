# MONITOR — Design

**Subsystem:** `monitor`
**Codename:** The Comb
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14
**Companion spec:** `MONITOR.md` (Requirements v0.1.0)

---

## 1. Overview

`monitor` v1 is the simplest of the analytical subsystems: read watched analyses, snapshot current upstream state, compare to analysis-time state, classify changes, surface alerts. No new mathematics; entirely orchestration and bookkeeping.

Discipline rules: failure isolation per analysis, no automatic state transitions beyond resolution-triggered expiration, structured outputs.

## 2. Resolved Open Questions

### OQ-MON-001 — Magnitude thresholds

**Resolution:** v1 default thresholds (0.01 / 0.05 / 0.15) are best-guess. Per-sector overrides allowed. Acceptance test measures empirical distributions and revises.

**Design implications:**
- Configuration in `config/monitor.yaml` with per-sector overrides.

### OQ-MON-002 — Time-decay threshold

**Resolution:** 7 days global default with per-class override via `EventClass.time_decay_alert_days`.

### OQ-MON-003 — Trajectory-derived metrics

**Resolution:** Leave to downstream. Follow-up records contain raw current values; trajectory computation belongs in `report_generator` or the calibration backtest.

### OQ-MON-004 — Operator notes

**Resolution:** Implement `follow_up_notes` table with `note_id`, `follow_up_id`, `note_text`, `set_at`, `set_by`. CLI: `razor-rooster monitor note <follow_up_id> "..."`. Notes are append-only and queryable.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      monitor/
        __init__.py
        cli.py
        engines/
          __init__.py
          comb.py                          # main cycle orchestrator
          change_detector.py               # per-dimension change detection
          invalidation_evaluator.py        # criterion evaluation
          alert_ranker.py
          reasoning.py                     # follow-up reasoning text generator
        persistence/
          __init__.py
          schemas.py
          migrations/
            m0001_monitor_initial.py
        config/
          monitor.yaml
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- All upstream tables: `signal_scanner.scan_records`, `polymarket_connector.polymarket_price_snapshots` and `polymarket_resolutions`, `mispricing_detector.comparisons`, `position_engine.analyses` and `watch_states`.
- The `position_engine` watch-expiration entry point from T-PE-061 (monitor calls it on resolution detection).

### 3.3 Tables

#### `monitor_cycles`

    cycle_id                      VARCHAR     PRIMARY KEY
    started_at                    TIMESTAMP   NOT NULL
    completed_at                  TIMESTAMP   NULL
    follow_ups_total              INTEGER     NOT NULL
    follow_ups_with_alerts        INTEGER     NOT NULL
    alerts_by_tier                JSON        NOT NULL    -- { 'resolution': N, 'invalidation_triggered': N, ... }

#### `follow_ups`

    follow_up_id                  VARCHAR     PRIMARY KEY
    cycle_id                      VARCHAR     NOT NULL
    analysis_id                   VARCHAR     NOT NULL
    -- analysis-time snapshot (copied for trajectory queries)
    analysis_model_p              DOUBLE      NOT NULL
    analysis_market_p             DOUBLE      NULL
    analysis_computed_at          TIMESTAMP   NOT NULL
    -- current snapshot
    current_scan_id               VARCHAR     NULL
    current_model_p               DOUBLE      NULL
    current_model_ci              JSON        NULL
    current_market_p              DOUBLE      NULL
    current_market_snapshot_ts    TIMESTAMP   NULL
    -- shifts
    model_probability_shift       DOUBLE      NULL
    model_shift_band              VARCHAR     NULL          -- 'none' | 'minor' | 'material' | 'major'
    market_probability_shift      DOUBLE      NULL
    market_shift_band             VARCHAR     NULL
    -- precursor snapshot
    precursor_snapshot            JSON        NOT NULL    -- list per precursor with current value, threshold, crossed flag
    -- time
    days_since_analysis           INTEGER     NOT NULL
    days_to_resolution            INTEGER     NULL
    time_decay_alert              BOOLEAN     NOT NULL DEFAULT FALSE
    -- invalidation
    invalidation_evaluations      JSON        NOT NULL    -- list of {criterion, status, current_value}
    invalidation_triggered_count  INTEGER     NOT NULL
    -- resolution
    resolution_status             VARCHAR     NOT NULL          -- 'unresolved' | 'resolved_yes' | 'resolved_no' | 'resolved_invalid'
    -- review and alerts
    recommended_review            BOOLEAN     NOT NULL
    primary_alert_tier            VARCHAR     NULL              -- 'resolution' | 'invalidation_triggered' | 'material_shift' | 'precursor_shift' | 'time_decay'
    alert_tiers                   JSON        NOT NULL    -- list of all applicable tiers
    -- reasoning
    reasoning_text                TEXT        NOT NULL
    -- carry-through warnings
    source_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE
    library_stale_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    -- meta
    computed_at                   TIMESTAMP   NOT NULL

Indexes: `(analysis_id, computed_at)`, `(cycle_id, primary_alert_tier)`.

#### `follow_up_notes`

    note_id                       VARCHAR     PRIMARY KEY
    follow_up_id                  VARCHAR     NOT NULL
    note_text                     TEXT        NOT NULL
    set_at                        TIMESTAMP   NOT NULL
    set_by                        VARCHAR     NOT NULL    -- always 'operator' in v1

Index: `(follow_up_id, set_at DESC)`.

### 3.4 Cycle Orchestration

`engines/comb.py`:

    def run_cycle() -> CycleReport:
        cycle_id = uuid()
        write_cycle_started(cycle_id)

        active_analyses = position_engine.list_watched_and_acted_on()

        for analysis in active_analyses:
            try:
                follow_up = evaluate_analysis(cycle_id, analysis)
                persist_follow_up(follow_up)
                if follow_up.resolution_status != 'unresolved':
                    position_engine.expire_watch(analysis.analysis_id, reason='monitor_resolved_detection')
            except Exception as e:
                persist_follow_up_error(cycle_id, analysis, e)

        complete_cycle(cycle_id)

`evaluate_analysis`:

    def evaluate_analysis(cycle_id, analysis) -> FollowUp:
        # Resolution check first — short-circuits other detection
        resolution = polymarket.resolution(analysis.condition_id)
        if resolution:
            return resolution_follow_up(cycle_id, analysis, resolution)

        # Current snapshots
        current_scan = signal_scanner.latest_scan_record(analysis.class_id)
        current_price = polymarket.latest_price(analysis.condition_id, analysis.outcome_token_id)

        # Shifts
        model_shift = compute_model_shift(analysis, current_scan)
        market_shift = compute_market_shift(analysis, current_price)
        precursor_snapshot = snapshot_precursors(analysis, current_scan)

        # Time
        days_since = (now - analysis.computed_at).days
        market = polymarket.market(analysis.condition_id)
        days_remaining = (market.end_date - now).days if market.end_date else None
        time_decay = days_remaining is not None and days_remaining <= time_decay_threshold

        # Invalidation
        invalidations = evaluate_invalidations(analysis, current_scan, current_price)

        # Recommendation
        recommended = (
            invalidations.triggered_count > 0
            or model_shift.band in ('material', 'major')
            or market_shift.band in ('material', 'major')
            or time_decay
        )

        # Alert tiers
        tiers = compute_alert_tiers(model_shift, market_shift, precursor_snapshot, time_decay, invalidations)

        # Reasoning
        text = reasoning.build(analysis, current_scan, current_price, model_shift, market_shift,
                                precursor_snapshot, invalidations, recommended)

        return FollowUp(...)

### 3.5 Change Detection

`engines/change_detector.py`:

    def compute_model_shift(analysis, current_scan) -> ShiftResult:
        if current_scan is None:
            return ShiftResult(value=None, band=None)
        delta = current_scan.posterior - analysis.model_probability
        band = classify_band(abs(delta), config.model_shift_thresholds)
        return ShiftResult(value=delta, band=band)

    def classify_band(magnitude, thresholds) -> str:
        # thresholds = { 'minor': 0.01, 'material': 0.05, 'major': 0.15 }
        if magnitude < thresholds['minor']: return 'none'
        if magnitude < thresholds['material']: return 'minor'
        if magnitude < thresholds['major']: return 'material'
        return 'major'

    def snapshot_precursors(analysis, current_scan) -> list[PrecursorSnapshot]:
        # For each precursor in the underlying class signature
        # Compare analysis-time value (from analysis trace) to current value (from current_scan trace)
        # Tag threshold-crossing
        ...

### 3.6 Invalidation Evaluation

`engines/invalidation_evaluator.py`:

    def evaluate_invalidations(analysis, current_scan, current_price) -> InvalidationsResult:
        results = []
        for criterion in analysis.invalidation_criteria:
            if criterion.type == 'precursor_shift':
                results.append(evaluate_precursor_criterion(criterion, current_scan))
            elif criterion.type == 'market_move':
                results.append(evaluate_market_criterion(criterion, current_price))
            elif criterion.type == 'mapping_confidence':
                results.append(evaluate_mapping_criterion(criterion))
            else:
                results.append(EvaluationResult(criterion=criterion, status='cannot_evaluate', reason=f'unknown type: {criterion.type}'))
        triggered = sum(1 for r in results if r.status == 'triggered')
        return InvalidationsResult(evaluations=results, triggered_count=triggered)

### 3.7 Alert Ranking

`engines/alert_ranker.py`:

    TIER_ORDER = ['resolution', 'invalidation_triggered', 'material_shift', 'precursor_shift', 'time_decay']

    def compute_alert_tiers(...) -> tuple[str | None, list[str]]:
        tiers = []
        if resolution_status != 'unresolved': tiers.append('resolution')
        if invalidation_triggered_count > 0: tiers.append('invalidation_triggered')
        if model_shift.band in ('material', 'major') or market_shift.band in ('material', 'major'):
            tiers.append('material_shift')
        if any(p.threshold_crossed for p in precursor_snapshot):
            tiers.append('precursor_shift')
        if time_decay:
            tiers.append('time_decay')
        primary = tiers[0] if tiers else None  # tier order is the priority order
        return primary, tiers

### 3.8 Reasoning Text

`engines/reasoning.py` builds short human-readable text:

    "Watched analysis for class 'pheic_declaration_12mo' (mapped to market 0xABC...).
     Since analysis (5 days ago):
     - Model probability moved from 0.18 to 0.24 (material shift, +0.06).
     - Market price held at 0.05 (no shift).
     - Precursor 'who_don_volume' jumped from 12 to 18, deeper into elevated territory.
     - Invalidation criterion 'WHO DON volume drops below 8' is not triggered.
     - 87 days remaining to resolution.

     Review recommended due to material model shift in the same direction as the original analysis."

The text is template-driven, not generated. Renders are deterministic for the same inputs.

### 3.9 CLI

    razor-rooster monitor run
    razor-rooster monitor evaluate <analysis_id>
    razor-rooster monitor show <follow_up_id>
    razor-rooster monitor list-alerts [--tier <t>] [--since <iso>]
    razor-rooster monitor trajectory <analysis_id>           # show all follow-ups for an analysis over time
    razor-rooster monitor note <follow_up_id> "..."

### 3.10 Threat Model

Threat context: STANDARD.

Risks:
1. **Bad analysis input.** Mitigation: per-analysis isolation, error captured in follow-up, cycle continues.
2. **Stale upstream data.** Mitigation: `source_stale_warning` and `library_stale_warning` carry through to follow-ups.
3. **Auto-expire failure leaving stale watched states.** Mitigation: monitor's resolution check is independent of `position_engine`'s expiration pass; both run independently and converge on the same state.
4. **Untrusted source content.** Reasoning text uses fixed templates; class titles and market questions are rendered verbatim but never executed.

## 4. Test Strategy

### 4.1 Unit Tests

- Magnitude classification per band.
- Precursor snapshot threshold-crossing detection.
- Invalidation evaluation per criterion type.
- Alert tier ranking.
- Reasoning text template rendering.

### 4.2 Integration Tests

- Full cycle against synthetic upstream with mixed-state watched analyses. Follow-ups produced for each.
- Failure isolation: one analysis throws, others complete.
- Resolution detection: synthetic resolution triggers expiration call into `position_engine`.
- Trajectory queryability: multiple cycles produce queryable time-series.

### 4.3 Acceptance Test

On operator hardware against real upstream system:
- Daily cycle within NFR-MON-PERF-001.
- Empirical magnitude distribution measured for OQ-MON-001 validation.
- Disk usage under NFR-MON-DISK-001.

## 5. Operational Model

### 5.1 Daily cadence

After `position_engine` cycle: `razor-rooster monitor run`.

### 5.2 Reviewing alerts

    razor-rooster monitor list-alerts --since 2026-05-01
    razor-rooster monitor show <follow_up_id>
    razor-rooster monitor trajectory <analysis_id>

The trajectory command produces a chronological view of how an analysis's situation has evolved across cycles.

### 5.3 Adding notes

    razor-rooster monitor note <follow_up_id> "Reviewed; deciding to hold. Will reconsider if WHO DON crosses 25."

Notes are append-only retrospectives.

## 6. Performance Notes

- v1 scale: 5–30 watched analyses × ~5 SQL queries each = sub-minute cycle.

## 7. Deferred to Implementation

- **DEFER-MON-001:** Empirical magnitude distributions; revise OQ-MON-001 thresholds after a month of cycles.
- **DEFER-MON-002:** Per-class time-decay overrides — implement when seed classes show a need.

## 8. References

- Requirements: `MONITOR.md` v0.1.0
- Upstream subsystem specs.
- LOOM v0.9.0
