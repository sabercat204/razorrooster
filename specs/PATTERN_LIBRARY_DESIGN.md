# PATTERN_LIBRARY — Design

**Subsystem:** `pattern_library`
**Codename:** The Bone Pile
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** MINIMAL_EXPOSURE
**Last updated:** 2026-05-14
**Companion spec:** `PATTERN_LIBRARY.md` (Requirements v0.1.0)

---

## 1. Overview

This document specifies the technical design for `pattern_library` v1. It maps the requirements in `PATTERN_LIBRARY.md` to a concrete architecture: event class registry mechanics, base-rate computation methodology, precursor signature extraction, analogue feature spaces, persistence, and the v1 seed library content.

Three discipline rules carry over from prior subsystems:

1. **Source-native preservation.** The library reads from `data_ingest` canonical schemas; it does not transform or correct the source data.
2. **Failure isolation.** A bad class definition does not corrupt the library; per-class refresh is independent.
3. **No silent ingestion.** Library outputs carry full provenance and version tags.

Two more specific to this subsystem:

4. **Empirical, not prescriptive.** Precursor signatures and analogue feature spaces are computed from the data, not asserted. The library may produce signatures that contradict the operator's prior belief about what should predict an event; that's the point.
5. **Honest uncertainty.** Low sample sizes, definition changes, and stale data all produce flagged outputs. Downstream consumers see warnings prominently and decide how to handle them.

## 2. Resolved Open Questions

### OQ-PL-001 — Base-rate prior

**Resolution:** Jeffreys prior (Beta(0.5, 0.5)) as the default. Per-class override permitted but discouraged.

**Reasoning:** Jeffreys is a defensible non-informative prior for binomial counts and produces wider credible intervals on small samples than uniform(Beta(1, 1)) or empirical-Bayes shortcuts. For very rare events the wide intervals are a feature, not a bug — they correctly communicate that we don't know much. If a class author has strong domain prior, they can specify an alternate prior in the class definition; the library logs a warning when a non-default prior is used.

**Design implications:**
- `BaseRateResult` carries `prior` field naming the prior used.
- Override via optional `prior_alpha` / `prior_beta` fields on `EventClass`.
- Refresh logs flag any non-default prior in use.

### OQ-PL-002 — Threshold-discovery method

**Resolution:** ROC-curve optimum (maximum Youden's J = sensitivity + specificity − 1) as the default. F1 and fixed-quantile alternatives configurable per signature.

**Reasoning:** Youden's J is interpretable (it's the maximum vertical distance from the ROC diagonal), doesn't require choosing a positive-class prevalence, and produces a single threshold without tuning. For event classes where false positives are very costly, an F1-based threshold or a fixed-quantile (e.g. 95th-percentile-of-baseline) override is supported.

**Design implications:**
- Per-precursor-variable `threshold_method` field: `'youden_j'` (default) | `'f1'` | `'quantile_95'` | `'manual:<value>'`.
- Signatures persist the method used so re-computation is reproducible.

### OQ-PL-003 — Non-event baseline windows

**Resolution:** Stratified-random sampling with a refractory exclusion zone around event timestamps.

**Reasoning:** Random sampling alone risks the baseline window overlapping a pre-event period (which has signal in it), inflating false-positive estimates. Stratified-random with a refractory exclusion (default: ±12 months around each occurrence is excluded from baseline) is a defensible compromise between methodological rigor and computational simplicity.

**Design implications:**
- Per-class `baseline_window_strategy` with default `stratified_random`.
- Per-class `refractory_months` defaulting to 12 (configurable; should match the typical lead-time window of interest).
- Baseline sample size per class default: 1,000 timestamps or 5× the event count, whichever is greater. Clamped to whatever the data supports.

### OQ-PL-004 — Feature-engineering seam

**Resolution:** Class authors compute features in their queries. The library provides a small set of *transformation* helpers that can be applied post-query (z-score normalization, percentile rank over rolling window, lag operators) but does not encapsulate feature engineering as a configurable step.

**Reasoning:** Feature engineering is where domain knowledge lives. Pushing it into the class definitions keeps each class self-documenting and avoids a leaky abstraction where the library's "feature catalog" inevitably fails to express what's needed for a new event class.

**Design implications:**
- `pl/transforms.py` module with `zscore(series)`, `percentile_rank(series, window)`, `lag(series, n)`, `rolling_mean(series, window)`. Class queries import these as helpers, not configuration.
- No "feature definition" persistence layer separate from the class definition itself.

### OQ-PL-005 — Calibration output format

**Resolution:** Three artifacts per calibrated class — Brier score, reliability diagram (binned predicted-vs-observed), and per-event prediction trace. Stored as JSON; downstream subsystems can render their own visualizations.

**Reasoning:** Brier score is the standard scalar measure of calibration quality. Reliability diagrams expose where calibration is poor across the probability range. The prediction trace lets operators inspect specific historical predictions for sanity-checking.

**Design implications:**
- `pl_calibration` table per design §3.4.
- Output JSON files at `data/library/calibration/<class_id>.json` containing all three artifacts.
- Format compatible with what `mispricing_detector` and the calibration-backtest infrastructure (OT-006) consume — keep the schema versioned.

### OQ-PL-006 — Continuous-magnitude event classes

**Resolution:** v1 supports binary classes only. Continuous-magnitude support deferred to v1.1 with a separate spec amendment.

**Reasoning:** Magnitude support adds substantial complexity (regression rather than classification, different calibration metrics, different precursor methodology). The seed library can express most analytically interesting things via thresholding (e.g., "ENSO Niño 3.4 anomaly exceeds +0.5°C" is a binary class derived from the continuous index). v1.1 can add native continuous support if the seed-class workflow proves limiting.

**Design implications:**
- `EventClass.outcome_type = 'binary'` is the only supported value in v1.
- The class registry validates this and refuses other values with a clear error referencing this resolution.

### OQ-PL-007 — Cross-class precursor sharing

**Resolution:** Precursor variables are class-local in v1. No shared catalog.

**Reasoning:** Even when two classes use a "similar" variable, the lead-time window, threshold, and combination logic differ. A shared catalog would either constrain class authors or end up being a thin abstraction. v1 keeps it simple: each class's signature is self-contained, and a precursor that's analytically useful across many classes can be defined as a Python function and imported into multiple class definitions.

**Design implications:**
- No `pl_precursor_catalog` table.
- Documentation in the operator README encourages writing reusable precursor query functions in `pl/precursors/<sector>.py` that class definitions can import.

### OQ-PL-008 — v1 seed library content

**Resolution:** Eight seed classes, with at least one per sector and a couple of classes that exercise the harder design corners (long lead time, multi-precursor combination, sparse data).

**Seed list:**

| Class ID | Sector | Description | Why |
|---|---|---|---|
| `pheic_declaration_12mo` | Public Health | WHO declares any PHEIC in a 12-month window | Tests low-sample base rate (5 PHEICs in WHO history at v1 time) |
| `gdelt_conflict_intensification` | Geopolitical | Country-week with GDELT escalation tone delta exceeding threshold | Tests dense/abundant data |
| `final_rule_within_12mo` | Regulatory | Federal Register publishes a final rule within 12 months of a proposed rule on the same docket | Tests document/docket schema, well-specified predicate |
| `opec_unscheduled_cut` | Commodity | OPEC announces a production cut between scheduled meetings | Tests sparse-event signature with FRED + EIA precursors |
| `enso_neutral_to_elnino` | Climate | ENSO state transitions from neutral to El Niño in a calendar quarter | Tests time-series threshold predicate, NOAA-derived |
| `eia_grid_reliability_event` | Infrastructure/Energy | EIA records grid reliability event meeting NERC thresholds | Tests infrastructure data quality |
| `multi_signal_geopolitical_alert` | Geopolitical (cross) | Combined signal: ACLED density up + GDELT tone down + relevant Federal Register filing within window | Tests multi-precursor combination logic |
| `polymarket_resolution_calibration` | Cross-cutting | Meta-class: tracks how well the library's own predictions calibrated against Polymarket resolutions | Tests the calibration-output pathway end-to-end |

The last meta-class is the linchpin for OT-006; it consumes the library's prior predictions (when downstream subsystems eventually log them) and the `polymarket_resolutions` table.

**Design implications:**
- One markdown documentation file per seed class under `specs/seed_event_classes/<class_id>.md`.
- One Python module per class under `razor_rooster/pattern_library/classes/<class_id>.py`.
- Calibration output exists for any seed class with ≥10 occurrences in the historical record.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      pattern_library/
        __init__.py
        cli.py                              # commands: refresh, list, show, validate, eval
        registry.py                         # class registration & lookup
        models/
          __init__.py
          event_class.py                    # EventClass dataclass + base
          outcomes.py                       # OutcomeRecord
          base_rate.py                      # BaseRateResult
          signature.py                      # PrecursorSignature, SignatureResult
          analogue.py                       # AnalogueFeatureSpace, AnalogueResults
          calibration.py                    # CalibrationOutput
        engines/
          __init__.py
          base_rates.py                     # compute_base_rate
          signatures.py                     # compute_signature, threshold finders
          analogues.py                      # compute_feature_space, find_analogues
          calibration.py                    # compute calibration artifacts
        transforms.py                       # zscore, percentile_rank, lag, rolling_mean (OQ-PL-004)
        precursors/                         # operator-extensible precursor query helpers
          __init__.py
          public_health.py
          geopolitical.py
          regulatory.py
          commodity.py
          climate.py
          infrastructure_energy.py
        classes/                            # one file per registered event class
          __init__.py
          pheic_declaration_12mo.py
          gdelt_conflict_intensification.py
          final_rule_within_12mo.py
          opec_unscheduled_cut.py
          enso_neutral_to_elnino.py
          eia_grid_reliability_event.py
          multi_signal_geopolitical_alert.py
          polymarket_resolution_calibration.py
        persistence/
          __init__.py
          schemas.py                        # pl_* table DDL
          migrations/
            m0001_pattern_library_initial.py
        version.py                          # library_version source of truth
        tests/
          fixtures/
            ...

### 3.2 Reuse from `data_ingest`

The library consumes:

- `DuckDBStore` and connection pool (data_ingest T-012) for persistence.
- The migrations framework (T-013) for `pl_*` schema changes.
- The staging-merge upsert pattern (T-014) for batch writes of recomputed outputs.
- The structured logging layer (T-021) for refresh logs.
- The schedule machinery is *not* used — pattern_library refresh is operator-initiated, not part of the daily ingest cycle.

### 3.3 Event Class Definition

```python
# razor_rooster/pattern_library/models/event_class.py
@dataclass(frozen=True)
class EventClass:
    class_id: str
    title: str
    description: str
    domain_sector: Sector
    secondary_sectors: tuple[Sector, ...] = ()
    definition_version: int = 1
    outcome_type: Literal['binary'] = 'binary'    # v1 only

    # Required queries
    occurrence_query: Callable[[DuckDBConnection], pd.DataFrame]    # returns OutcomeRecord rows
    precursors: tuple[PrecursorVariable, ...] = ()
    analogue_features: tuple[AnalogueFeature, ...] = ()

    # Configuration
    base_rate_window_default: timedelta = timedelta(days=365 * 10)
    refractory_months: int = 12
    baseline_strategy: BaselineStrategy = BaselineStrategy.STRATIFIED_RANDOM
    baseline_sample_size: int = 1000
    prior_alpha: float = 0.5
    prior_beta: float = 0.5
```

Each event class is a Python module that imports `EventClass` and instantiates a module-level `CLASS = EventClass(...)`. Modules are auto-discovered under `pattern_library/classes/` via `pkgutil.iter_modules`.

`PrecursorVariable` is a dataclass:

```python
@dataclass(frozen=True)
class PrecursorVariable:
    variable_id: str
    title: str
    query: Callable[[DuckDBConnection, datetime, datetime], pd.Series]    # returns time-indexed series
    direction: Literal['high_signals_event', 'low_signals_event']
    lead_time_window: timedelta = timedelta(days=180)
    threshold_method: ThresholdMethod = ThresholdMethod.YOUDEN_J
    manual_threshold: float | None = None
```

`AnalogueFeature`:

```python
@dataclass(frozen=True)
class AnalogueFeature:
    feature_id: str
    query: Callable[[DuckDBConnection, datetime], float]    # returns single value at timestamp
    normalization: Normalization = Normalization.ZSCORE
    weight: float = 1.0    # for weighted-Euclidean distance metric
```

### 3.4 Tables

All tables live under the `pl_*` namespace in the same DuckDB store as `data_ingest` and `polymarket_connector` data.

#### `pl_event_classes`

    class_id              VARCHAR     PRIMARY KEY
    title                 TEXT        NOT NULL
    description           TEXT        NOT NULL
    domain_sector         VARCHAR     NOT NULL
    secondary_sectors     JSON        NULL
    definition_version    INTEGER     NOT NULL
    outcome_type          VARCHAR     NOT NULL
    registered_at         TIMESTAMP   NOT NULL
    last_evaluated_at     TIMESTAMP   NULL
    library_version_at_last_eval INTEGER NULL

Index: `(domain_sector)`.

#### `pl_outcomes`

    class_id              VARCHAR     NOT NULL
    occurrence_id         VARCHAR     NOT NULL    -- deterministic hash of class + occurrence_ts
    occurrence_ts         TIMESTAMP   NOT NULL
    end_ts                TIMESTAMP   NULL        -- for events with duration
    description           TEXT        NULL
    source_records        JSON        NOT NULL    -- list of {table, source_record_id} tuples

Primary key: `(class_id, occurrence_id)`.
Index: `(class_id, occurrence_ts)`.

#### `pl_base_rates`

    class_id                       VARCHAR     NOT NULL
    window_start                   TIMESTAMP   NOT NULL
    window_end                     TIMESTAMP   NOT NULL
    occurrences                    INTEGER     NOT NULL
    rate_per_year                  DOUBLE      NOT NULL
    credible_interval_lower        DOUBLE      NOT NULL
    credible_interval_upper        DOUBLE      NOT NULL
    prior_alpha                    DOUBLE      NOT NULL
    prior_beta                     DOUBLE      NOT NULL
    library_version                INTEGER     NOT NULL
    definition_version             INTEGER     NOT NULL
    data_as_of                     TIMESTAMP   NOT NULL
    computed_at                    TIMESTAMP   NOT NULL
    low_sample_warning             BOOLEAN     NOT NULL DEFAULT FALSE
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE
    stale                          BOOLEAN     NOT NULL DEFAULT FALSE

Primary key: `(class_id, window_start, window_end, library_version)`.

#### `pl_precursor_signatures`

    class_id                       VARCHAR     NOT NULL
    variable_id                    VARCHAR     NOT NULL
    library_version                INTEGER     NOT NULL
    definition_version             INTEGER     NOT NULL
    threshold_method               VARCHAR     NOT NULL
    threshold_value                DOUBLE      NULL
    direction                      VARCHAR     NOT NULL
    lead_time_window_days          INTEGER     NOT NULL
    pre_event_mean                 DOUBLE      NULL
    pre_event_p25                  DOUBLE      NULL
    pre_event_p50                  DOUBLE      NULL
    pre_event_p75                  DOUBLE      NULL
    baseline_mean                  DOUBLE      NULL
    baseline_p25                   DOUBLE      NULL
    baseline_p50                   DOUBLE      NULL
    baseline_p75                   DOUBLE      NULL
    hit_rate                       DOUBLE      NULL    -- TPR at threshold
    false_positive_rate            DOUBLE      NULL    -- FPR at threshold
    sample_size_events             INTEGER     NOT NULL
    sample_size_baseline           INTEGER     NOT NULL
    confidence_score               DOUBLE      NOT NULL
    low_confidence_warning         BOOLEAN     NOT NULL DEFAULT FALSE
    computed_at                    TIMESTAMP   NOT NULL

Primary key: `(class_id, variable_id, library_version)`.

#### `pl_analogue_features`

    class_id                       VARCHAR     NOT NULL
    point_id                       VARCHAR     NOT NULL    -- 'event:<occurrence_id>' or 'baseline:<hash>'
    timestamp                      TIMESTAMP   NOT NULL
    is_event                       BOOLEAN     NOT NULL
    feature_vector_raw             JSON        NOT NULL
    feature_vector_normalized      JSON        NOT NULL
    library_version                INTEGER     NOT NULL
    definition_version             INTEGER     NOT NULL

Primary key: `(class_id, point_id, library_version)`.
Index: `(class_id, library_version, is_event)`.

#### `pl_calibration`

    class_id                       VARCHAR     NOT NULL
    library_version                INTEGER     NOT NULL
    definition_version             INTEGER     NOT NULL
    method                         VARCHAR     NOT NULL    -- e.g. 'leave_one_out_signature'
    brier_score                    DOUBLE      NOT NULL
    reliability_bins               JSON        NOT NULL    -- list of {bin_low, bin_high, predicted_mean, observed_freq, count}
    prediction_trace_path          VARCHAR     NOT NULL    -- file path under data/library/calibration/
    computed_at                    TIMESTAMP   NOT NULL

Primary key: `(class_id, library_version, method)`.

#### `pl_library_versions`

    library_version                INTEGER     PRIMARY KEY
    bumped_at                      TIMESTAMP   NOT NULL
    bump_reason                    VARCHAR     NOT NULL    -- 'code_change' | 'class_added' | 'class_modified' | 'class_removed'
    affected_class_ids             JSON        NULL

#### `pl_refresh_log`

    refresh_id                     VARCHAR     PRIMARY KEY
    started_at                     TIMESTAMP   NOT NULL
    ended_at                       TIMESTAMP   NULL
    library_version                INTEGER     NOT NULL
    classes_processed              JSON        NOT NULL    -- list of {class_id, status, duration_seconds, warnings}
    error_summary                  JSON        NULL

### 3.5 Computation Engines

#### Base rates (`engines/base_rates.py`)

```python
def compute_base_rate(
    conn: DuckDBConnection,
    cls: EventClass,
    window: tuple[datetime, datetime] | None = None,
) -> BaseRateResult:
    window_start, window_end = window or _default_window(cls)
    occurrences_df = cls.occurrence_query(conn)
    n = ((occurrences_df['occurrence_ts'] >= window_start) &
         (occurrences_df['occurrence_ts'] < window_end)).sum()
    duration_years = (window_end - window_start).days / 365.25
    rate_per_year = n / duration_years
    # Jeffreys prior credible interval (or per-class override)
    a = cls.prior_alpha + n
    b = cls.prior_beta + duration_years - n  # treating duration_years as effective trials
    ci_lower, ci_upper = _beta_credible_interval(a, b, level=0.95)
    return BaseRateResult(
        ...,
        low_sample_warning=(n < 5),
        source_stale_warning=_check_source_freshness(conn, cls),
    )
```

The "trials" framing (treating duration as denominator) is a simplification appropriate for rare events viewed as a Poisson process. The credible interval is computed from the conjugate Gamma posterior in that framing, not the literal Beta. Implementation detail; the math is in code comments and in the per-class documentation.

#### Precursor signatures (`engines/signatures.py`)

For each precursor variable:

1. Pull the variable's time-series for the historical window via `precursor.query(conn, window_start, window_end)`.
2. For each event occurrence in the class, extract the variable's value within the lead-time window (e.g. mean over the 6 months before the event).
3. Build the baseline distribution by sampling timestamps per `cls.baseline_strategy`, excluding the refractory zone around each occurrence.
4. Compute pre-event vs. baseline summary statistics.
5. Discover threshold per `precursor.threshold_method`:
   - `youden_j`: scan candidate thresholds, pick the one maximizing TPR − FPR.
   - `f1`: pick the threshold maximizing F1 against the event labels.
   - `quantile_95`: 95th percentile of baseline distribution.
   - `manual:<value>`: take the configured value.
6. At the chosen threshold, compute hit rate (fraction of events where the variable was past threshold during lead-time window) and false-positive rate (fraction of baseline samples past threshold).
7. Compute confidence score: combination of sample size, distributional separation (Cohen's d between pre-event and baseline), and bootstrap-based uncertainty in the threshold.

The combined multi-variable score (REQ-PL-SIG-004) is computed by:

1. For each variable, the indicator "current value past threshold in expected direction" → 0/1.
2. Per-variable hit rate is a probability.
3. Combined score = geometric mean of hit rates for variables currently past threshold, calibrated by historical co-occurrence (if two precursors historically tend to fire together, the joint signal is less independent than naive product would suggest).
4. The calibration step uses a small lookup table built during signature computation: per each subset of precursors that fires together at least once historically, the empirical joint hit rate. Falls back to geometric mean for never-jointly-fired subsets.

#### Analogue feature spaces (`engines/analogues.py`)

1. For each event occurrence and each baseline timestamp, compute the feature vector via `feature.query(conn, timestamp)` for each `AnalogueFeature` in the class.
2. Apply normalization per feature (default z-score using the population of all sampled points in the class).
3. Persist normalized vectors to `pl_analogue_features`.
4. `find_analogues(class_id, current_features, k)`: load the normalized population, normalize the current features using the *same* normalization parameters (means/stds saved alongside), compute weighted-Euclidean distance, return top-k.

Distance metric: weighted Euclidean by default, where weights come from `AnalogueFeature.weight`. Mahalanobis distance is supported by per-class override but requires the class to provide a covariance estimate (or compute it from the feature population).

#### Calibration (`engines/calibration.py`)

For classes with ≥10 historical occurrences:

1. Leave-one-out evaluation: for each historical event, hold it out, recompute the precursor signature and base rate from the remaining data, and predict the held-out event's probability.
2. Concatenate predictions for events and baseline samples → predicted-vs-observed pairs.
3. Compute Brier score, reliability diagram (10 bins by default), and full prediction trace.
4. Persist scalar metrics to `pl_calibration`; write the prediction trace to `data/library/calibration/<class_id>.json`.

For classes with <10 occurrences, calibration is not computed; the calibration table contains a row with `method='insufficient_data'` and Brier score NULL.

### 3.6 Library Version Mechanics

`version.py` exposes a single `LIBRARY_VERSION` integer. It bumps in three cases:

1. **Code change.** Manually bumped via the version file when computation engines or core models change. CI check (or pre-commit hook) verifies the version was bumped if any file in `engines/`, `models/`, `transforms.py`, or `version.py` itself changed.
2. **Class registry change.** Refresh detects a class added/removed and bumps. Recorded in `pl_library_versions.bump_reason`.
3. **Class definition change.** Refresh detects a class's `definition_version` changed and bumps the library version too. The class's prior outputs are marked stale.

`current_version()` returns the live value. Outputs are tagged with the version that produced them. Downstream consumers can detect mismatches by comparing.

### 3.7 Refresh Workflow

`razor-rooster pattern-library refresh [--class <id>] [--force]`:

    1. Lock: acquire a file-based lock at data/library/.refresh.lock to prevent concurrent refresh.
    2. Discover registered classes (or single class if --class).
    3. Resolve library version (bump if necessary per §3.6).
    4. For each class (parallel-bounded, default max_workers=2 — these are SQL-heavy):
       a. Validate class definition (re-run REQ-PL-CLASS-004 checks).
       b. Pull occurrences via cls.occurrence_query.
       c. Persist outcomes to pl_outcomes via staging-merge.
       d. Compute base rate → persist to pl_base_rates.
       e. Compute precursor signatures → persist to pl_precursor_signatures.
       f. Compute analogue feature space → persist to pl_analogue_features.
       g. If event count >= 10, compute calibration → persist to pl_calibration + JSON file.
       h. Update pl_event_classes.last_evaluated_at and library_version_at_last_eval.
    5. Write refresh log entry to pl_refresh_log and structured JSON log.
    6. Release lock.

Per-class failures are isolated: on exception, the class's prior outputs are left in place (stale flag may or may not be set), the failure is logged with traceback, and refresh continues with the next class.

### 3.8 CLI

    razor-rooster pattern-library refresh [--class <id>] [--force]
    razor-rooster pattern-library list [--sector <s>]
    razor-rooster pattern-library show <class_id>
    razor-rooster pattern-library validate <class_id>
    razor-rooster pattern-library eval <class_id> [--window-start ...] [--window-end ...]

`show` displays the class's documentation, current base rate, signature summary, and calibration metrics. `validate` runs REQ-PL-CLASS-004 checks without persisting. `eval` runs an ad-hoc base-rate / signature evaluation without committing to the persistence layer.

### 3.9 Threat Model

Threat context: MINIMAL_EXPOSURE.

Principal risks:

1. **Bad class definitions corrupting the library.** Mitigation: per-class isolation, validation at registration, validation again at refresh, transactional persistence. A malformed class fails its own refresh; everything else proceeds.
2. **Stale outputs being mistakenly trusted.** Mitigation: explicit stale flag on outputs, version mismatch detection in `current_version()`, source-stale propagation from `data_ingest` freshness.
3. **Untrusted source content treated as instructions.** Library reads from `data_ingest` tables which by contract are untrusted data. Library does not interpret any text content; predicates and queries operate on structured fields and numeric values only. Class authors are operator-trusted (they're operator-written Python modules); the operator is responsible for not introducing predicates that execute arbitrary text content.
4. **Calibration overfitting via class definition tweaks.** Mitigation: every definition change bumps `definition_version`, and the refresh log records every definition change so the operator can audit whether they're tweaking a class to "fix" calibration rather than for legitimate reasons. This is a discipline issue more than a code issue.

The library has no external network access. It reads from the local DuckDB store and writes back to it.

## 4. Test Strategy

### 4.1 Unit Tests

- Each model dataclass: validation, default values, `__post_init__` checks.
- Base-rate engine: synthetic occurrences with known counts produce expected rates and credible intervals.
- Signature engine: synthetic data with known signal-to-noise ratio confirms threshold discovery (per method) finds the right threshold.
- Analogue engine: synthetic two-class population with engineered separable features confirms k-NN lookup returns the correct cluster.
- Calibration engine: synthetic well-calibrated and badly-calibrated predictions produce expected Brier scores.
- Transforms: standard tests for z-score, percentile rank, lag, rolling mean.

### 4.2 Integration Tests

- Full refresh against a synthetic `data_ingest` snapshot with one class, confirms all tables populated.
- Full refresh with multiple classes, one of which has a malformed predicate — confirms isolation (others succeed).
- Definition-version bump test: edit a class, refresh, confirm prior outputs flagged stale.
- Source-stale propagation: simulate stale `data_ingest` source, confirm `source_stale_warning` flows through.
- Library-version bump tests for each of the three bump triggers.

### 4.3 Seed Library Tests

- For each seed class, an integration test: against a `data_ingest` test snapshot containing the relevant historical data, refresh the class and confirm:
  - At least one occurrence is found (where applicable).
  - Base rate is non-NULL.
  - Precursor signatures populate.
  - For classes with sufficient data, calibration runs.

### 4.4 Acceptance Test

On operator hardware, against the real `data_ingest` corpus produced by `data_ingest` T-072:

- Full refresh of all 8 seed classes completes within NFR-PL-PERF-001 (15 min).
- Each calibrated class produces a calibration output file.
- Library version bumps tracked correctly across simulated edits.
- Disk usage under NFR-PL-DISK-001 (1 GB).

## 5. Operational Model

### 5.1 First refresh

    razor-rooster pattern-library refresh

Runs after `data_ingest` has been backfilled and is producing daily incrementals. Expected duration: under 15 minutes on the EliteBook G8 for the v1 seed library.

### 5.2 Adding a new class

    1. Create razor_rooster/pattern_library/classes/<class_id>.py per template.
    2. (Optionally) write reusable precursor functions in razor_rooster/pattern_library/precursors/<sector>.py.
    3. Bump LIBRARY_VERSION (or rely on refresh-time auto-bump).
    4. Run razor-rooster pattern-library validate <class_id>.
    5. Run razor-rooster pattern-library refresh --class <class_id>.
    6. (Optionally) commit the class file + a per-class docs file under specs/seed_event_classes/.

### 5.3 Modifying a class

Same as adding, but bumping the class's own `definition_version` is required. Refresh will mark prior outputs stale.

### 5.4 Reading library outputs from downstream code

```python
from razor_rooster.pattern_library import library

br = library.base_rate(class_id="pheic_declaration_12mo")
assert library.current_version() == br.library_version  # consumer-side check
```

The `library` module is the public API; downstream subsystems should not query `pl_*` tables directly.

## 6. Performance Notes & Risks

- **Bootstrap-based confidence scoring** in signature engine is the most CPU-intensive step. Per-class bootstrap with 1,000 samples takes seconds; across 8 classes plus operator-added classes, this is not a concern at v1 scale.
- **Analogue feature-space size** at v1 scale (≤10,000 points per class) fits in memory comfortably. Distance computations are vectorized via NumPy.
- **DuckDB query planning for occurrence_query and precursor.query** is operator's responsibility — class authors should profile their queries against realistic data sizes before persisting them.
- **Risk: overfitting to historical patterns.** Precursor signatures are computed from in-sample data; if the operator adjusts class definitions until calibration looks good, the calibration metrics overstate real predictive power. Mitigation: REQ-PL-VER-001 logs every definition change so the operator can audit their own tweaks.
- **Risk: very rare event classes.** Classes with <5 occurrences have base rates with credible intervals approaching the prior — i.e., we know almost nothing. The low_sample_warning flag exists; downstream consumers must respect it.

## 7. Deferred to Implementation

- **DEFER-PL-001:** Empirical distribution of seed-class base rates against the actual `data_ingest` corpus once T-072 (data_ingest backfill) is complete. Numbers in this design are placeholders until measured.
- **DEFER-PL-002:** Optimal `baseline_sample_size` per class. v1 uses 1,000 / 5×events default; tune per class once empirical signal-to-noise is observed.
- **DEFER-PL-003:** Whether multi-precursor combination should use the geometric-mean-with-co-occurrence-correction described in §3.5 or a simpler product, based on whether the correction makes a measurable difference on the seed classes.
- **DEFER-PL-004:** Calibration plot reliability-bin count. v1 default is 10 bins; if seed classes are too sparse for 10 bins to be meaningful, drop to 5.

## 8. References

- Requirements spec: `PATTERN_LIBRARY.md` v0.1.0
- `data_ingest` Requirements/Design/Tasks v0.1.0 — for canonical schemas and freshness contract.
- `polymarket_connector` Requirements v0.1.0 — for `polymarket_resolutions` (used by `polymarket_resolution_calibration` meta-class).
- LOOM v0.7.0: `razorrooster.md`
- Open thread OT-006 — partially addressed via REQ-PL-SEED-003 and the meta-class.
- System prompt v0.2: `razorrooster-prompt.md.txt` (educational framing).
