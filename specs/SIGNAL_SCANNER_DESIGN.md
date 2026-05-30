# SIGNAL_SCANNER — Design

**Subsystem:** `signal_scanner`
**Codename:** The Nose
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14
**Companion spec:** `SIGNAL_SCANNER.md` (Requirements v0.1.0)

---

## 1. Overview

`signal_scanner` is the bridge between historical patterns and current conditions. It is structurally simple: read `pattern_library` outputs, evaluate registered classes' precursors against current `data_ingest` state, combine via Bayesian update, persist the result. The complexity is concentrated in the probability-update mechanics and the trace generation.

Discipline rules carrying through:

1. **Source-native preservation** — the scanner reads from canonical schemas; no transformation.
2. **Failure isolation** — bad class evaluation cannot corrupt others.
3. **No silent ingestion** — scans tag staleness and version drift loudly.
4. **Honest fallback** — when data is missing or stale, the scan reports prior unchanged; it does not fabricate updates from sparse data.

## 2. Resolved Open Questions

### OQ-SCAN-001 — Bayesian-update formulation

**Resolution:** Naive Bayes with co-occurrence correction, mirroring `pattern_library`'s `combine_variables` mechanism.

**Reasoning:** Pure naive Bayes assumes conditional independence between precursors, which is often false (precursors that historically co-occur don't carry as much joint information as their independent product implies). The same correction the library applies in computing combined hit rates can be applied to live evaluation: use historical co-occurrence patterns to discount over-stating joint signal. This keeps the scanner consistent with the library's own combination semantics — same math, same calibration regime.

Logistic-regression-style combination was considered but rejected: it requires fitting weights on the historical record, which is what `pattern_library` already does for individual variable thresholds. Adding another fit step at the scanner layer creates an opaque interaction with the library's calibration.

**Design implications:**
- Update is a single function in `engines/posterior.py` consuming the per-variable hit/false-positive rates from the library plus the cached co-occurrence table.
- The trace records each step: per-variable likelihood ratio applied, joint correction term, posterior odds, posterior probability.

### OQ-SCAN-002 — Credible-interval propagation

**Resolution:** Monte Carlo sampling from the prior CI and per-variable rate uncertainties.

**Reasoning:** Closed-form propagation through a Bayesian update with multiple uncertain inputs requires distributional assumptions that don't hold cleanly for our setup. Monte Carlo (sample 1,000 prior values from base-rate posterior, sample variable hit/FPR pairs from beta posteriors, run the update for each sample, take percentiles of the posterior distribution) is exact at the cost of CPU. At v1 scale (≤20 classes × ≤10 variables × 1,000 samples) it's well under a second per class.

**Design implications:**
- `engines/posterior.py` provides `posterior_with_ci(prior_dist, variable_dists, current_values, n_samples=1000)`.
- Trace records the credible interval and the sample count used.
- The approximation note in the trace says "Monte Carlo, 1000 samples; estimate has ±X% sampling error at 95th percentile."

### OQ-SCAN-003 — Default divergence threshold

**Resolution:** Log-odds shift of ≥0.5 as the design default. Tunable per sector. Acceptance test (T-SCAN-080) measures the empirical divergence distribution against the seed library and reports whether the default is well-calibrated; if not, design notes get updated and the default revised.

**Design implications:**
- Threshold lives in `config/scanner.yaml` as a per-sector mapping.
- The acceptance-test phase explicitly checks whether ~10–25% of seed classes typically cross the threshold (a healthy noise/signal ratio); deviations trigger threshold revision.

### OQ-SCAN-004 — Class definition-version drift mid-scan

**Resolution:** Scans use the library version pinned at scan start. Definition-version drift detected at scan start (i.e., a class's `definition_version` advanced since `pattern_library` last refreshed for it) marks that class with a warning flag and proceeds with the existing library outputs. The warning is loud in the trace and in the scan summary.

**Reasoning:** Refusing to scan because of drift creates a chicken-and-egg with library refresh schedules; using stale outputs without warning hides drift. Flagging is the honest middle. Operator can fix drift by running library refresh.

**Design implications:**
- Scan startup compares `pattern_library.current_version()` and per-class definition versions against persisted outputs; mismatches set the `definition_drift_warning` flag on affected scan records.
- A `--strict` CLI flag opts in to refusal-on-drift behavior for operators who want it.

### OQ-SCAN-005 — Cross-class second-order indicator

**Resolution:** Defer to v2.

**Reasoning:** Real value, not v1 scope. Implementing it correctly requires defining cross-class similarity in a non-arbitrary way and producing meaningful aggregate output. Premature.

**Design implications:**
- None for v1. The data needed for v2 is captured in scan records anyway, so v2 implementation is additive.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      signal_scanner/
        __init__.py
        cli.py                              # commands: run, show, list-candidates, prune
        engines/
          __init__.py
          scanner.py                        # main scan orchestration
          posterior.py                      # Bayesian update + Monte Carlo CI propagation
          trace.py                          # reasoning trace builder + renderer
          candidates.py                     # candidate identification
        persistence/
          __init__.py
          schemas.py                        # scan_summaries, scan_records, scan_traces
          migrations/
            m0001_signal_scanner_initial.py
        config/
          scanner.yaml                      # thresholds, disabled classes, warnings
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- From `data_ingest`: `DuckDBStore`, staging-merge, structured logging, scheduler integration.
- From `pattern_library`: the `library` facade (T-PL-060) for reading base rates, signatures, and (where relevant) analogue features. The scanner does not query `pl_*` tables directly.

### 3.3 Tables

#### `scan_summaries`

    scan_id                       VARCHAR     PRIMARY KEY    -- UUID
    scan_started_at               TIMESTAMP   NOT NULL
    scan_completed_at             TIMESTAMP   NULL
    pattern_library_version       INTEGER     NOT NULL
    classes_total                 INTEGER     NOT NULL
    classes_succeeded             INTEGER     NOT NULL
    classes_failed                INTEGER     NOT NULL
    classes_skipped               INTEGER     NOT NULL
    candidates_count              INTEGER     NOT NULL
    library_stale_warning         BOOLEAN     NOT NULL DEFAULT FALSE

#### `scan_records`

    scan_id                       VARCHAR     NOT NULL    -- FK to scan_summaries
    class_id                      VARCHAR     NOT NULL
    class_definition_version      INTEGER     NOT NULL
    pattern_library_version       INTEGER     NOT NULL
    data_as_of                    TIMESTAMP   NOT NULL
    base_rate                     DOUBLE      NOT NULL
    base_rate_ci_lower            DOUBLE      NOT NULL
    base_rate_ci_upper            DOUBLE      NOT NULL
    posterior                     DOUBLE      NOT NULL
    posterior_ci_lower            DOUBLE      NOT NULL
    posterior_ci_upper            DOUBLE      NOT NULL
    log_odds_shift                DOUBLE      NOT NULL
    is_candidate                  BOOLEAN     NOT NULL
    candidate_direction           VARCHAR     NULL          -- 'elevated' | 'depressed' | NULL
    signature_confidence          DOUBLE      NULL
    -- warning flags
    low_signature_confidence      BOOLEAN     NOT NULL DEFAULT FALSE
    source_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE
    library_stale_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    definition_drift_warning      BOOLEAN     NOT NULL DEFAULT FALSE
    no_update_applied             BOOLEAN     NOT NULL DEFAULT FALSE    -- REQ-SCAN-PROB-003 fallback
    no_update_reason              VARCHAR     NULL
    error                         TEXT        NULL                       -- non-NULL if class evaluation failed

Primary key: `(scan_id, class_id)`.
Indexes: `(class_id, scan_started_at)`, `(is_candidate, scan_started_at)`.

#### `scan_traces`

    scan_id                       VARCHAR     NOT NULL
    class_id                      VARCHAR     NOT NULL
    trace_json                    JSON        NOT NULL

Primary key: `(scan_id, class_id)`. Stored separately from `scan_records` so queries against records aren't pessimized by full-trace JSON.

### 3.4 Scan Orchestration

`engines/scanner.py`:

    def run_scan(class_id: str | None = None, strict: bool = False) -> ScanReport:
        scan_id = uuid()
        now = utcnow()
        library_version = pattern_library.current_version()

        # Prepare classes
        classes = [pattern_library.list_classes()]
        if class_id is not None:
            classes = [c for c in classes if c.class_id == class_id]
        classes = [c for c in classes if c.class_id not in disabled_classes()]

        write_summary(scan_id, started_at=now, library_version=library_version, classes_total=len(classes))

        # Per-class evaluation, parallel-bounded
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(evaluate_class, scan_id, cls, library_version, strict): cls for cls in classes}
            for fut in as_completed(futures):
                record, trace = fut.result()  # exceptions captured in record.error
                persist_record(record)
                persist_trace(trace)

        complete_summary(scan_id, completed_at=utcnow())

`evaluate_class` is the per-class workhorse:

    def evaluate_class(scan_id, cls, library_version, strict) -> tuple[ScanRecord, Trace]:
        try:
            base_rate = pattern_library.base_rate(cls.class_id)
            signatures = pattern_library.signature(cls.class_id)
            if check_definition_drift(cls, base_rate, signatures):
                if strict: raise StrictDriftAbort(...)
                # else flag and proceed
            current_values = evaluate_precursors(cls, signatures)
            if missing_or_stale(current_values, signatures):
                return base_rate_only_record(...), no_update_trace(...)
            posterior, ci = posterior_with_ci(base_rate, signatures, current_values)
            log_odds_shift = log_odds(posterior) - log_odds(base_rate.point_estimate)
            is_candidate = identify_candidate(cls.domain_sector, log_odds_shift, signature_confidence)
            return ScanRecord(...), Trace(...)
        except Exception as e:
            return error_record(scan_id, cls, e), error_trace(scan_id, cls, e)

### 3.5 Posterior Computation Detail

`engines/posterior.py`:

```python
def posterior_with_ci(
    base_rate: BaseRateResult,
    signatures: list[SignatureResult],
    current_values: dict[str, float],
    n_samples: int = 1000,
) -> tuple[float, tuple[float, float]]:
    # Sample priors from base-rate beta posterior
    prior_samples = beta.rvs(base_rate.posterior_alpha, base_rate.posterior_beta, size=n_samples)

    # Sample per-variable hit / FPR rates from their beta posteriors
    rate_samples = {sig.variable_id: sample_rates(sig, n_samples) for sig in signatures}

    # For each MC sample, compute updated posterior
    posteriors = np.empty(n_samples)
    for i in range(n_samples):
        odds = prior_samples[i] / (1 - prior_samples[i])
        for sig in signatures:
            fired = current_value_above_threshold(current_values[sig.variable_id], sig.threshold, sig.direction)
            hit = rate_samples[sig.variable_id]['hit'][i]
            fpr = rate_samples[sig.variable_id]['fpr'][i]
            lr = (hit / fpr) if fired else ((1 - hit) / (1 - fpr))
            odds = odds * lr
        odds = apply_co_occurrence_correction(odds, signatures, current_values)
        posteriors[i] = odds / (1 + odds)

    return posteriors.mean(), (np.percentile(posteriors, 2.5), np.percentile(posteriors, 97.5))
```

The co-occurrence correction reuses the cached lookup `pattern_library` builds during signature computation.

### 3.6 Reasoning Trace

`engines/trace.py` builds a trace dict with the structure:

    {
      "class_id": "pheic_declaration_12mo",
      "class_definition_version": 1,
      "library_version": 1,
      "data_as_of": "...",
      "prior": {"point": 0.05, "ci": [0.02, 0.10]},
      "precursors": [
        {
          "variable_id": "who_don_volume",
          "title": "WHO DON entries (rolling 90d)",
          "current_value": 12,
          "threshold": 8,
          "direction": "high_signals_event",
          "fired": true,
          "hit_rate": 0.65,
          "false_positive_rate": 0.20,
          "likelihood_ratio_applied": 3.25
        },
        ...
      ],
      "co_occurrence_correction": -0.12,    # log-odds adjustment
      "posterior": {"point": 0.18, "ci": [0.07, 0.34]},
      "log_odds_shift": 1.43,
      "is_candidate": true,
      "candidate_direction": "elevated",
      "warnings": ["library_stale_14_days"],
      "ci_method": "monte_carlo_1000_samples"
    }

A renderer (`render_trace_text(trace) -> str`) produces the human-readable form `report_generator` consumes.

### 3.7 Configuration

`config/scanner.yaml`:

    version: 1
    candidate_thresholds:
      log_odds_shift_min: 0.5    # default for all sectors
      per_sector:
        public_health: 0.5
        geopolitical: 0.6        # tolerate slightly higher noise
        regulatory: 0.4
        commodity: 0.5
        climate: 0.5
        infrastructure_energy: 0.5
    confidence_floor: 0.3
    stale_source_eligible_for_candidate: false
    library_stale_threshold_days: 14
    disabled_classes: []
    monte_carlo_samples: 1000
    max_workers: 4

### 3.8 CLI

    razor-rooster scan run [--class <id>] [--strict]
    razor-rooster scan show <scan_id>                       # display scan summary + per-class records
    razor-rooster scan show-trace <scan_id> <class_id>      # render the trace
    razor-rooster scan list-candidates [--since <iso>]      # list recent candidate situations
    razor-rooster scan prune --before <iso> --confirm       # operator-initiated pruning

### 3.9 Threat Model

Threat context: STANDARD.

Risks:
1. **Bad class evaluation corrupting scan.** Mitigation: per-class try/except, error captured in record, scan continues.
2. **Library drift unnoticed.** Mitigation: `definition_drift_warning` and `library_stale_warning` flags; loud in summary.
3. **Stale source data treated as fresh.** Mitigation: `source_stale_warning` propagates from `data_ingest` freshness view; `stale_source_eligible_for_candidate` config defaults to false.
4. **Untrusted source content as instructions.** Scanner only consumes structured numeric/categorical fields produced by `pattern_library`. Trace rendering uses fixed format strings; class-author text in `class.title`/`class.description` is rendered verbatim but never executed.

## 4. Test Strategy

### 4.1 Unit Tests

- Posterior computation with known prior and known likelihood produces expected output.
- Monte Carlo CI propagation: high-uncertainty inputs produce wide CI, low-uncertainty inputs produce tight.
- Co-occurrence correction: reduces joint signal when variables historically co-fire.
- Candidate identification: divergence threshold respected per sector; confidence floor respected.
- Trace builder/renderer round-trip.
- No-update fallback when missing data.

### 4.2 Integration Tests

- Full scan against synthetic `pattern_library` outputs and `data_ingest` snapshot. All seed classes evaluated.
- Failure isolation: one class throws; others complete.
- Library drift: simulate definition_version mismatch; flag set.
- Strict mode: simulate drift with `--strict`; scan aborts.
- Re-scan idempotency: two scans on same data produce two distinct records.

### 4.3 Acceptance Test

On operator hardware, against real `data_ingest` corpus and refreshed `pattern_library`:

- Full scan completes within NFR-SCAN-PERF-001 (5 min).
- Records produced for all 8 seed classes plus operator-added.
- Empirical divergence distribution measured to validate OQ-SCAN-003 threshold.
- Disk usage under NFR-SCAN-DISK-001 (500 MB after first year of daily scans).

## 5. Operational Model

### 5.1 First scan

Run after `pattern_library` refresh:

    razor-rooster pattern-library refresh
    razor-rooster scan run

### 5.2 Daily cadence

`launchd` / cron: after `data_ingest` cycle completes, run `razor-rooster scan run`. Fully unattended.

### 5.3 Investigating a candidate

    razor-rooster scan list-candidates --since 2026-05-01
    razor-rooster scan show-trace <scan_id> <class_id>

The trace tells the operator why the system flagged the candidate. Operator decides whether to escalate to a deeper analysis.

## 6. Performance Notes

- Monte Carlo at 1,000 samples per class × 8 seed classes = 8k samples per scan. Sub-second.
- Per-class precursor evaluation is the dominant cost — each precursor's query hits `data_ingest` tables. Inherits SQL query performance from `data_ingest`'s index design.
- At v1 scale, NFR-SCAN-PERF-001 (5 min) has substantial headroom.

## 7. Deferred to Implementation

- **DEFER-SCAN-001:** Empirical divergence distribution. Measure once seed library produces real outputs. Adjust thresholds.
- **DEFER-SCAN-002:** Whether to expose Monte Carlo sample count as per-class config rather than global. Default: global. Revisit if some classes need higher precision.

## 8. References

- Requirements spec: `SIGNAL_SCANNER.md` v0.1.0
- `pattern_library` Requirements/Design/Tasks v0.1.0
- `data_ingest` Requirements/Design/Tasks v0.1.0
- LOOM v0.7.0
