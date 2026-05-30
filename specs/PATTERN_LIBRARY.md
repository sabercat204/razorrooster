# PATTERN_LIBRARY — Requirements

**Subsystem:** `pattern_library`
**Codename:** The Bone Pile
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** MINIMAL_EXPOSURE
**Last updated:** 2026-05-14

---

## 1. Purpose

`pattern_library` is the historical-knowledge layer of Razor-Rooster. Its job is to answer three operationally distinct questions about real-world event classes:

1. **Base-rate question** — given an event class (e.g. "WHO declares a PHEIC in the next 12 months"), what is its historical frequency over a defined retrospective window?
2. **Precursor question** — when the event class has occurred historically, what observable conditions tended to be present in the months or years leading up to it?
3. **Analogue question** — given the current observable state, which historical events most closely resemble today's conditions, and how did those events resolve?

Downstream consumers:
- `signal_scanner` reads precursor signatures and compares them to current `data_ingest` state to surface candidate situations worth analyzing.
- `mispricing_detector` reads base-rate priors when forming model probabilities to compare against Polymarket-implied probabilities.
- `position_engine` reads outcome distributions for sizing analyses (Kelly fraction inputs).
- The calibration backtest (OT-006) reads historical predictions made by the system and compares them to actual outcomes recorded here.

`pattern_library` does not pull data from external sources directly. It reads from the canonical schemas maintained by `data_ingest` and from `polymarket_resolutions` via `polymarket_connector`. It does not make probability claims about specific contracts; it makes population-level claims about event classes.

## 2. Scope

### In scope (v1)

- A formal definition of "event class" with required metadata fields, encoded as a registry.
- A base-rate computation engine that, given an event class definition, computes the historical frequency from `data_ingest` records over a configurable retrospective window.
- A precursor-signature catalog: per event class, a structured description of observable variables that have historically preceded the event, with summary statistics (lead time distribution, magnitude thresholds, hit rate, false-positive rate).
- An analogue-matching engine: given a structured description of current conditions, returns the top-k most similar historical situations along with their resolutions and a similarity score.
- A v1 seed library of event classes — a small number, well-curated, covering at least one example per Razor-Rooster domain sector, sufficient to validate the framework end-to-end.
- A persistence layer for the library itself (event class definitions, precursor signatures, computed base rates, analogue feature spaces) that lives alongside `data_ingest` data in the same DuckDB store.
- A versioning mechanism: every base rate, signature, and analogue match output is tagged with the library version that produced it, so downstream subsystems can detect when they're consuming stale outputs after a library update.
- Provenance tracking: every output is traceable to the underlying `data_ingest` records that informed it.

### Out of scope (explicit)

- **Causal inference.** The library reports correlations and conditional frequencies, not causal claims. A precursor that historically preceded an event is not asserted to cause it.
- **Live event detection.** That is `signal_scanner`'s job. `pattern_library` provides reference patterns; `signal_scanner` watches live data against them.
- **Probability estimates for specific Polymarket contracts.** That is `mispricing_detector`'s job, which combines library outputs with Polymarket's contract specifications.
- **Machine-learning models trained on the data.** v1 is statistical aggregation and feature-space distance metrics. ML-based pattern recognition is a v2+ consideration.
- **Continuous re-fitting.** Library outputs (base rates, signatures) are computed on demand or on a slow refresh schedule (weekly), not in real time.
- **Comprehensive event-class coverage.** v1 ships with a seed library, not a complete one. Operator-driven expansion of the library is the expected workflow.
- **Cross-source identifier reconciliation beyond what `data_ingest` provides.** If `data_ingest` doesn't unify two sources for an event, the library treats them as separate.

## 3. Operating Assumptions

- **Operator:** single user. The library is a personal knowledge base, curated and extended by the operator over time.
- **Data freshness:** library outputs reflect `data_ingest` state as of the last library refresh. The library does not auto-invalidate when new data arrives mid-day; refresh cadence is operator-controlled.
- **Compute envelope:** all library operations run on the EliteBook G8 inside the existing DuckDB store. Base-rate computations and analogue lookups are SQL-shaped workloads, not GPU-accelerated inference.
- **Version discipline:** library version increments on any change that could alter outputs. Downstream subsystems compare expected vs. observed library version and refuse to use outputs from an older version without explicit operator override.
- **Hybrid interpretation:** v1 supports both statistical aggregation (base rates, summary statistics on precursors) and analogue-based matching (k-NN over event feature spaces). Both modes coexist; downstream subsystems can use either or both.

## 4. Conceptual Model

### 4.1 Event Class

An *event class* is a precisely defined, machine-evaluable description of a category of real-world event. Examples:

- "WHO declares a Public Health Emergency of International Concern in any 12-month window."
- "OPEC+ announces an unscheduled production cut between meetings."
- "U.S. Federal Register publishes a final rule from a named agency within X months of a proposed rule on the same docket."
- "ENSO state transitions from neutral to El Niño within a calendar quarter."

Every event class definition includes:

- A stable identifier and a human-readable title.
- A natural-language description.
- A formal *occurrence predicate*: a query (SQL or callable) over `data_ingest` tables that returns the set of historical occurrences with timestamps.
- A *resolution semantics* description: how an instance of this class is determined to have happened (which records, which fields, what threshold).
- A primary domain sector and optional secondary sectors.
- A *base-rate window* default (e.g. "10 years," "since 1980," etc.).

Event classes are the unit of analysis throughout the library.

### 4.2 Precursor Signature

A *precursor signature* is a structured description of observable conditions that historically preceded events in a given event class, derived empirically from the historical record.

A signature includes:

- A reference to the event class.
- A list of *precursor variables* — each is a query over `data_ingest` tables returning a numeric or categorical time-series.
- Per precursor variable: lead-time distribution (when relative to the event was the variable elevated/depressed), magnitude statistics (mean, percentiles), and hit/false-positive rates at configured thresholds.
- A signature confidence score reflecting sample size and signal strength.

Signatures are computed from the historical record; they are not hand-authored predictions.

### 4.3 Analogue

An *analogue* is a historical event instance described in a feature space such that distances between feature vectors are meaningful proxies for situational similarity.

An analogue feature space is per event class and includes:

- A list of *features* (queries over `data_ingest` returning numeric values at a given timestamp).
- A *normalization* (z-score, percentile rank, etc.) so features with different magnitudes contribute proportionally to distance.
- A *distance metric* (Euclidean over normalized features by default).

The analogue-matching engine takes a current-conditions feature vector, finds the k-nearest historical events, and returns them with their resolutions and similarity scores.

### 4.4 Outcome Record

An *outcome record* is a row representing what happened: an event class identifier, an occurrence timestamp, and observed magnitudes/durations. Outcome records are the population from which base rates are computed.

## 5. Functional Requirements

Requirements use EARS-style phrasing with stable IDs. PL = `pattern_library`.

### 5.1 Event class registry

**REQ-PL-CLASS-001: Event class definition format**
The library **shall** define event classes via a Python module convention: each class is a typed dataclass implementing an `EventClass` interface with the fields listed in §4.1. Classes are registered in a registry on import.
*Verification:* unit test confirms a representative event class registers and is discoverable.

**REQ-PL-CLASS-002: Persistent class registration**
On library startup or refresh, registered event classes **shall** be reflected in a `pl_event_classes` table including: `class_id`, `title`, `description`, `domain_sector`, `secondary_sectors`, `definition_version`, `registered_at`, `last_evaluated_at`, `library_version`.
*Verification:* schema migration produces the table; round-trip test confirms registration writes and reads cleanly.

**REQ-PL-CLASS-003: Class definition versioning**
A change to an event class definition (occurrence predicate, resolution semantics, default window) **shall** increment the `definition_version` for that class. Computed outputs (base rates, signatures, analogues) tied to the prior version **shall** be marked stale.
*Verification:* edit a synthetic event class, confirm version increments, confirm prior outputs flagged stale.

**REQ-PL-CLASS-004: Class definition validation**
Event class definitions **shall** be validated at registration time. Invalid definitions (missing fields, malformed predicates, unresolvable table references) **shall** fail registration with a clear error and **shall not** appear in the registry.
*Verification:* malformed class definition is rejected with informative error.

### 5.2 Base-rate computation

**REQ-PL-BR-001: Base-rate query operation**
The library **shall** provide `compute_base_rate(class_id, window=None) -> BaseRateResult` returning: the count of historical occurrences in the window, the window's total duration, the rate per unit time, the 95% credible interval (Jeffreys prior), and metadata (window bounds, library version, data-as-of timestamp).
*Verification:* unit test against synthetic occurrence data confirms count, rate, and credible-interval calculation.

**REQ-PL-BR-002: Window-conditional base rates**
The library **shall** support computing base rates over arbitrary user-specified windows (e.g. "1990–2010," "rolling 10-year") in addition to the class default.
*Verification:* unit test confirms different windows produce expected different results.

**REQ-PL-BR-003: Base-rate persistence**
Computed base rates **shall** be persisted to a `pl_base_rates` table with: `class_id`, `window_start`, `window_end`, `occurrences`, `rate_per_year`, `credible_interval_lower`, `credible_interval_upper`, `library_version`, `definition_version`, `data_as_of`, `computed_at`.
*Verification:* schema migration; round-trip test.

**REQ-PL-BR-004: Stale-output flagging**
A persisted base rate **shall** be flagged stale when the underlying event class's `definition_version` has changed since the rate was computed, or when `data_as_of` is older than a configurable threshold (default: 7 days).
*Verification:* simulated definition change and simulated data-aging both flip the stale flag.

**REQ-PL-BR-005: Sample-size warnings**
A base-rate result **shall** include a warning flag when the historical occurrence count is below a configurable threshold (default: 5). Downstream consumers **shall** be able to read this flag and decide how to handle low-sample classes.
*Verification:* synthetic class with 2 occurrences returns a warning; synthetic class with 50 occurrences does not.

### 5.3 Precursor signature computation

**REQ-PL-SIG-001: Precursor signature definition**
A precursor signature **shall** be defined per event class with: a list of precursor variable queries, a configurable lead-time window (e.g. "12 months before each occurrence"), a configurable threshold per variable (or a discovery procedure for thresholds), and a baseline-comparison window (non-event periods).
*Verification:* unit test confirms a representative signature parses and runs against synthetic data.

**REQ-PL-SIG-002: Empirical signature computation**
The library **shall** provide `compute_signature(class_id) -> SignatureResult` returning per precursor variable: the empirical distribution of values during pre-event windows vs. baseline windows, the implied threshold(s) at which signal/noise ratio is maximized, and hit/false-positive rates at those thresholds.
*Verification:* unit test against synthetic data with known signal confirms detection; against synthetic noise confirms low hit rate and clear false-positive estimate.

**REQ-PL-SIG-003: Signature persistence**
Signatures **shall** persist to `pl_precursor_signatures` keyed by `(class_id, variable_id, library_version)` with: lead-time stats, value distribution stats, optimal-threshold value, hit_rate, false_positive_rate, sample_size, and computed_at.
*Verification:* schema migration; round-trip test.

**REQ-PL-SIG-004: Multi-variable signature combination**
The library **shall** support multi-variable signature evaluation: given the current values of multiple precursor variables, compute a combined "signature match" score. The default combination method is the geometric mean of per-variable hit rates, calibrated against historical co-occurrence patterns.
*Verification:* unit test confirms combined score on a synthetic case where two precursors are jointly elevated produces a score consistent with their joint historical hit rate.

**REQ-PL-SIG-005: Signal-strength caveats**
A signature **shall** carry a confidence score that reflects sample size, base-rate variability, and the difference between pre-event and baseline distributions. Signatures with confidence below a configurable threshold (default: 0.3 on a 0–1 scale) **shall** be marked low-confidence and downstream consumers **shall** decide whether to use them.
*Verification:* synthetic low-sample class produces low-confidence flag; high-sample, well-separated class produces high-confidence flag.

### 5.4 Analogue matching

**REQ-PL-AN-001: Feature space definition**
An analogue feature space **shall** be defined per event class with: a list of feature queries returning numeric values at a given timestamp, a normalization rule per feature, and a distance metric.
*Verification:* unit test parses a representative feature space and computes distances between two synthetic feature vectors correctly.

**REQ-PL-AN-002: Historical feature-space population**
The library **shall** populate the feature space for each historical event in the class plus a sample of non-event timestamps as the searchable analogue population.
*Verification:* unit test confirms expected number of points in the feature space after population.

**REQ-PL-AN-003: Current-conditions matching**
The library **shall** provide `find_analogues(class_id, current_features, k=10) -> AnalogueResults` returning the top-k nearest historical situations with: timestamps, distance, event-or-non-event tag, and outcome description for event-tagged points.
*Verification:* unit test confirms the closest historical event to a synthetic current-conditions vector is the one with the most similar engineered features.

**REQ-PL-AN-004: Analogue persistence**
Computed analogue feature spaces **shall** persist to `pl_analogue_features` with: `class_id`, `event_or_baseline_id`, `timestamp`, feature vector (JSON), `library_version`, `definition_version`.
*Verification:* schema migration; round-trip test.

**REQ-PL-AN-005: Distance metric configurability**
The default distance metric is normalized Euclidean. The library **shall** allow per-class override (e.g. Mahalanobis for correlated features, weighted Euclidean for known feature importance).
*Verification:* unit test confirms a class with a custom metric produces different ranking than the default.

### 5.5 Refresh and lifecycle

**REQ-PL-REFRESH-001: Refresh command**
The library **shall** provide a `pattern_library refresh` CLI command that re-computes all base rates, signatures, and analogue feature spaces from current `data_ingest` state.
*Verification:* CLI integration test runs refresh and confirms `last_evaluated_at` updates for all classes.

**REQ-PL-REFRESH-002: Per-class refresh**
The library **shall** support `pattern_library refresh --class <class_id>` for refreshing a single class without recomputing the rest.
*Verification:* CLI integration test refreshes one class, confirms only that class's outputs are regenerated.

**REQ-PL-REFRESH-003: Refresh cadence default**
The library **shall** be designed for weekly refresh as the default cadence. Schedule-driven refresh **shall** be optional and configurable, not enforced.
*Verification:* documentation review; no hard-coded daemon/timer.

### 5.6 Library versioning

**REQ-PL-VER-001: Library version**
The library **shall** maintain a `library_version` integer that increments on any code change to the computation engines, on any change to the event class registry contents (additions, deletions, definition changes), and on any change to feature space definitions.
*Verification:* code review confirms version-bump points; integration test confirms version increments on a synthetic registry change.

**REQ-PL-VER-002: Output tagging**
Every output the library returns (base rate, signature, analogue match) **shall** include the `library_version` and `definition_version` in effect when the output was produced.
*Verification:* output dataclass field present and populated in every API response.

**REQ-PL-VER-003: Downstream version checking**
Downstream subsystems consuming library outputs **shall** be able to query the current library version via `pattern_library.current_version()` and compare to the version embedded in any cached output they hold.
*Verification:* unit test confirms version query returns expected value; downstream-style consumer correctly identifies a version mismatch.

### 5.7 Provenance

**REQ-PL-PROV-001: Per-output provenance**
Every persisted library output **shall** carry: the underlying `data_ingest` table(s) it consulted, the time range of records used, the count of records, and the data-as-of timestamp from `data_ingest`'s freshness view.
*Verification:* DuckDB query returns provenance for any persisted base rate, signature, or analogue.

**REQ-PL-PROV-002: Source-stale handling**
When the library is asked to compute against a class whose underlying `data_ingest` source(s) are flagged stale (per `data_ingest` REQ-PROV-002), it **shall** still compute the result but **shall** flag the output with a `source_stale_warning`. Downstream consumers decide whether to use stale-source-derived outputs.
*Verification:* synthetic stale-source scenario produces flagged output.

### 5.8 Seed library (v1 content)

**REQ-PL-SEED-001: Minimum seed coverage**
The v1 library **shall** ship with at least one event class per Razor-Rooster domain sector (six sectors → minimum six event classes). The classes **shall** be chosen to be: empirically tractable (clear occurrence predicate, sufficient historical data in the v1 source list), representative of typical analytical interest, and individually documented.
*Verification:* registry contains ≥6 classes covering all sectors; each has documentation file in `specs/seed_event_classes/<class_id>.md`.

**REQ-PL-SEED-002: Seed class evidence**
Each seed event class **shall** be accompanied by a written rationale documenting: why this class is analytically interesting, which `data_ingest` sources are required to evaluate it, the chosen occurrence predicate, the chosen precursor variables, the chosen analogue features, and known limitations.
*Verification:* per-class markdown documents exist and are non-trivial.

**REQ-PL-SEED-003: Seed class calibration check**
For each seed class with sufficient historical data (≥10 occurrences), the v1 library **shall** include a calibration plot (or equivalent tabular output) showing how well the precursor signature would have predicted historical events when applied retrospectively.
*Verification:* per-seed-class calibration output exists in `data/library/calibration/<class_id>.json` after refresh.

### 5.9 Operator extension

**REQ-PL-EXT-001: Adding a new event class**
The operator **shall** be able to add a new event class by writing a new class definition module, registering it, and running refresh. No changes to library core code **shall** be required for typical class additions.
*Verification:* operator-style test adds a synthetic class via the documented procedure and confirms it appears in registry and gets evaluated.

**REQ-PL-EXT-002: Modifying an existing event class**
The operator **shall** be able to modify an event class definition; the modification **shall** trigger a `definition_version` bump and outputs **shall** regenerate on next refresh.
*Verification:* operator-style test modifies a class definition and confirms version increment and refresh-time regeneration.

### 5.10 Logging & observability

**REQ-PL-LOG-001: Structured refresh log**
Each refresh run **shall** emit a structured JSON log entry consistent with `data_ingest` REQ-LOG-001 conventions: classes processed, base rates computed, signatures computed, analogue spaces populated, durations, and errors.
*Verification:* log file inspection after a refresh confirms structured entry.

**REQ-PL-LOG-002: Per-class computation log**
On per-class refresh, the library **shall** log the underlying record counts, time ranges, and any warnings (low sample, stale source, definition change).
*Verification:* refresh of a synthetic class produces expected log entries.

## 6. Non-Functional Requirements

**NFR-PL-PERF-001:** A full refresh of a v1-sized seed library (≤20 event classes) **shall** complete within 15 minutes on the operator's hardware after `data_ingest` has populated its tables.

**NFR-PL-PERF-002:** A single base-rate query **shall** return in under 1 second for any v1 class.

**NFR-PL-PERF-003:** A single analogue lookup over a v1-sized analogue space (≤10,000 historical points per class) **shall** return in under 2 seconds.

**NFR-PL-AVAIL-001:** Library failures (e.g. a malformed class definition encountered during refresh) **shall not** corrupt previously persisted outputs. Errors are isolated per class.

**NFR-PL-DISK-001:** v1 library tables (event classes, base rates, signatures, analogue features, calibration outputs) **shall** stay under 1 GB out of the 100 GB global cap, given the v1 seed scale.

**NFR-PL-VER-001:** Library version increments are deterministic — the same code state with the same registry contents produces the same `library_version`.

## 7. Open Questions (carry to design phase)

- **OQ-PL-001:** Choice of base-rate prior. Jeffreys prior (Beta(0.5, 0.5)) is a reasonable default but for very rare events it produces wide credible intervals that can be misleading. Decide whether to expose the prior choice as a per-class configuration.
- **OQ-PL-002:** Threshold-discovery method for precursor variables. Options: ROC-curve optimum (max Youden J), F1-optimum, fixed quantile (e.g. 90th percentile). Decide a default and whether to expose alternatives.
- **OQ-PL-003:** How to define "non-event" baseline windows for signature computation. Options: random sampling, all timestamps not within K months of an event, matched control windows. Decide methodology.
- **OQ-PL-004:** Analogue feature engineering — should the library provide a small library of feature transforms (z-score, percentile rank over rolling window, etc.) or expect class authors to compute features in their queries? Decide where the seam goes.
- **OQ-PL-005:** Calibration plot format — Brier score, reliability diagram, both? Match what `mispricing_detector` and the calibration backtest will consume.
- **OQ-PL-006:** Multi-resolution event classes — some events have continuous magnitudes (e.g. hurricane Saffir-Simpson category, ENSO Niño 3.4 anomaly). Should the library support classes that resolve to a magnitude rather than a binary occurred/didn't-occur? v1 default is binary; magnitude support is an open design question.
- **OQ-PL-007:** Cross-class precursor sharing. Different event classes might share precursor variables (e.g. ACLED event density informs both "regional conflict escalation" and "humanitarian-emergency declaration"). Should signatures be reusable across classes via a shared precursor catalog, or are they per-class?
- **OQ-PL-008:** v1 seed library content — six classes (one per sector) is the minimum. Are there specific classes the operator wants seeded vs. left to extend later? Settle in design.

## 8. Acceptance Criteria

The `pattern_library` v1 is considered complete when all the following are true:

- The seed library contains ≥6 event classes, ≥1 per domain sector.
- A full refresh runs end-to-end within NFR-PL-PERF-001.
- Base rates, signatures, and analogue spaces are computed and persisted for every seed class.
- Each seed class with ≥10 historical occurrences has a calibration output file.
- A new operator-defined class can be added per REQ-PL-EXT-001 without core code changes.
- Library version bumps correctly on registry/code changes and downstream consumers can detect mismatches.
- Source-stale warnings propagate through to library outputs.
- A library failure on one class does not corrupt outputs from other classes.

## 9. References

- LOOM v0.6.0 — `razorrooster.md`, subsystem registry entry for `pattern_library`.
- `data_ingest` Requirements/Design/Tasks v0.1.0 — for the canonical schemas and freshness contract this library reads from.
- `polymarket_connector` Requirements v0.1.0 — for `polymarket_resolutions` access (used in calibration backtest).
- Open thread OT-006 — partially addressed by REQ-PL-SEED-003 (per-class calibration) and the broader calibration-backtest infrastructure that will live in `mispricing_detector`.
- System prompt v0.2 — `razorrooster-prompt.md.txt` (educational framing; library outputs are evidence-anchored second opinions, not directives).
