# SIGNAL_SCANNER — Requirements

**Subsystem:** `signal_scanner`
**Codename:** The Nose
**Spec version:** 0.1.0 (Requirements draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14

---

## 1. Purpose

`signal_scanner` is the live-evaluation layer that connects historical patterns to current conditions. Its job is, on each cycle:

1. For every event class registered in `pattern_library`, evaluate the class's precursor variables against current `data_ingest` data.
2. Compute a per-class signature-match score and a derived current-conditions probability estimate, anchored on the class's base rate.
3. Surface classes whose current-conditions estimate has materially diverged from the base rate as *candidate situations* worth deeper analysis.
4. Maintain a time-series log of every scan so calibration backtests can later compare predicted probabilities against observed outcomes.

Downstream consumers:
- `mispricing_detector` reads candidate situations and joins them with Polymarket markets in the same sector to compare model probabilities against market-implied probabilities.
- `report_generator` reads the latest scan output to populate the cycle report's "watchlist" and "top analyses" sections.
- `monitor` reads scan-derived probabilities for active analyses to detect when conditions have changed enough to warrant re-evaluation.

`signal_scanner` does not itself produce position recommendations, market comparisons, or final analyses. It produces *probability estimates with reasoning traces* that downstream subsystems use as inputs.

## 2. Scope

### In scope (v1)

- A scan cycle that evaluates every registered event class against the current `data_ingest` state.
- Per-class current-conditions probability estimation that combines base rate (prior) with signature-match score (likelihood update).
- Candidate identification: classes where the current estimate diverges from the base rate by more than a configurable threshold.
- A reasoning trace per class explaining how the estimate was derived (which precursors fired, by how much, what the combined score was, how it shifted the prior).
- Time-series persistence of every scan's outputs, so historical scans are queryable for calibration.
- Configurable scan cadence (default: daily, after `data_ingest` cycle completes).
- Per-scan structured logging consistent with the rest of the system.
- Failure isolation: a bad event class does not poison other classes' scan results.

### Out of scope (explicit)

- **Probability calibration** — that is `pattern_library`'s job. The scanner uses calibrated outputs; it does not calibrate.
- **Comparison against Polymarket** — that is `mispricing_detector`'s job. The scanner produces model probabilities, not deltas.
- **Position recommendations or sizing** — that is `position_engine`'s job.
- **Real-time / sub-cycle scanning** — v1 is batch, daily-cadence. Real-time is a v2 consideration.
- **New event class authoring** — `pattern_library` is the registry. The scanner reads what's registered.
- **Backfilling historical scans** — v1 scans current conditions only. Historical reconstructions are out of scope; if needed for backtesting, they're a one-shot script using `pattern_library` directly.

## 3. Operating Assumptions

- **Cadence:** runs once per day after `data_ingest` cycle completes. Operator can run additional ad-hoc scans.
- **Compute envelope:** all scan operations run in the same DuckDB store. Per-class evaluation is SQL-shaped; expected to fit comfortably within memory at v1 scale.
- **Data freshness:** scans use `data_ingest` data as-of the most recent successful fetch per source. Stale sources produce flagged scan outputs.
- **Library version:** the scanner pins the `pattern_library` version it used at scan time. Re-running an old scan against a newer library version requires explicit operator action.
- **Scope of evaluation:** every registered event class is evaluated each scan unless explicitly disabled. There is no "active classes" subset configuration in v1.

## 4. Conceptual Model

### 4.1 Scan

A *scan* is a single end-to-end evaluation cycle. It produces:

- One *scan record* per event class (= n classes × 1 row).
- A *scan summary* (one row per scan execution) capturing aggregate stats.
- A list of *candidate situations* (subset of scan records that crossed the divergence threshold).

### 4.2 Scan Record

A scan record represents the system's current best estimate for one event class at one point in time:

- Class identifier and library version pinned.
- Current value of each precursor variable (with the variable's threshold and whether it's currently exceeded).
- Combined signature-match score.
- Current-conditions probability estimate (point estimate plus credible interval).
- Base rate snapshot used as prior.
- Divergence: how much current estimate differs from base rate.
- Confidence flags (low sample, stale data, low signature confidence, library version drift).
- Reasoning trace.

### 4.3 Candidate Situation

A *candidate situation* is a scan record whose current-conditions probability has diverged from the base rate by more than a configurable threshold. Candidates are surfaced to downstream subsystems as "things worth looking at this cycle."

The threshold is configurable per sector and per direction (above-base-rate vs below-base-rate). Default: log-odds shift of ≥0.5 (i.e., odds at least ~1.6× the base-rate odds, or equivalently for low base rates: probability moved enough to matter relative to the prior).

### 4.4 Reasoning Trace

A *reasoning trace* is a structured record explaining how a scan record's probability estimate was derived. Each trace includes:

- Prior (base rate from `pattern_library`).
- Per-precursor evaluation: variable, current value, threshold, fired (yes/no), per-variable hit rate.
- Combined signature-match score.
- Posterior (current-conditions estimate) with the inference method noted.
- Warnings carried from `pattern_library` (low confidence, source stale, etc.).
- Library and definition versions pinned.

The trace is meant to be human-readable when rendered, and machine-queryable when persisted.

## 5. Functional Requirements

Requirements use EARS-style phrasing. SCAN = `signal_scanner`.

### 5.1 Scan execution

**REQ-SCAN-EXEC-001: Scan command**
The scanner **shall** provide a `razor-rooster scan run` CLI command that executes a full scan over all registered event classes.
*Verification:* CLI integration test runs a scan against synthetic data, confirms one record per class produced.

**REQ-SCAN-EXEC-002: Scan-per-class invocation**
The scanner **shall** support `razor-rooster scan run --class <class_id>` for evaluating a single class without scanning the rest.
*Verification:* CLI integration test runs single-class scan; only that class's record produced.

**REQ-SCAN-EXEC-003: Failure isolation per class**
A failure in evaluating one event class **shall not** halt the scan or corrupt other classes' records. Failures are logged structured and surfaced in the scan summary.
*Verification:* integration test forces one class to error; scan completes for others, summary reflects partial success.

**REQ-SCAN-EXEC-004: Library version pinning**
A scan **shall** pin the `pattern_library` version in effect at the start of the scan and **shall** record that version on every output record. If the library version changes mid-scan (e.g. operator runs library refresh during a scan), the scan **shall** abort with a clear error.
*Verification:* simulated mid-scan library bump triggers abort.

### 5.2 Probability estimation

**REQ-SCAN-PROB-001: Posterior computation**
For each event class, the scanner **shall** compute a current-conditions probability estimate by combining the class's base rate prior (from `pattern_library`) with the signature-match likelihood derived from current precursor values. The combination method **shall** be Bayesian update with the per-variable hit rates and false-positive rates as the likelihood components.
*Verification:* unit test against synthetic data with known prior and known likelihood produces expected posterior; edge cases (all variables fired, no variables fired) handled correctly.

**REQ-SCAN-PROB-002: Credible interval propagation**
The current-conditions estimate **shall** include a credible interval that reflects both the prior's uncertainty (from base rate CI) and the signature's uncertainty (from confidence score and bootstrap if available). When propagation is approximate (typical case), the approximation method **shall** be documented in the reasoning trace.
*Verification:* unit test confirms wider intervals when signatures are low-confidence; tighter when high-confidence.

**REQ-SCAN-PROB-003: Honest fallback for missing data**
When a class's precursor variables cannot be evaluated due to missing or stale source data, the scanner **shall** report the current estimate as equal to the base rate (no information), with the trace explicitly noting why no update was applied.
*Verification:* simulated missing data produces base-rate-equal output with documented reason.

### 5.3 Candidate identification

**REQ-SCAN-CAND-001: Divergence threshold**
A scan record **shall** be marked as a candidate situation when the absolute difference between current-conditions probability (point estimate) and base rate exceeds a per-sector configurable threshold. Default threshold: log-odds shift of ≥0.5.
*Verification:* synthetic class with engineered divergence above threshold marked candidate; below threshold not.

**REQ-SCAN-CAND-002: Direction tagging**
Candidate situations **shall** be tagged with direction (`elevated` if current > base rate, `depressed` if current < base rate).
*Verification:* unit test confirms correct tagging in each direction.

**REQ-SCAN-CAND-003: Confidence-aware thresholding**
A scan record **shall not** be marked candidate if its underlying signature confidence is below a configurable threshold (default: 0.3 on the 0–1 confidence scale from `pattern_library`). Low-confidence signatures cannot, by themselves, produce candidates regardless of how much the point estimate has moved.
*Verification:* synthetic low-confidence class with high divergence is not marked candidate.

**REQ-SCAN-CAND-004: Stale-data exclusion option**
The operator **shall** be able to configure whether scans with `source_stale_warning` set are eligible for candidate marking. Default: stale-source scans are *not* eligible.
*Verification:* config-driven test confirms behavior in both modes.

### 5.4 Reasoning traces

**REQ-SCAN-TRACE-001: Per-class trace**
Every scan record **shall** include a reasoning trace per §4.4.
*Verification:* schema test confirms every record has a populated trace; format test confirms trace structure matches §4.4.

**REQ-SCAN-TRACE-002: Trace renderability**
The trace **shall** be renderable to human-readable text via a documented function. Output format compatible with `report_generator`'s consumption.
*Verification:* unit test renders a synthetic trace and confirms key fields appear in the rendered output.

**REQ-SCAN-TRACE-003: Trace queryability**
Traces **shall** be persisted in a structured (JSON) form that allows querying for properties like "scans where precursor X fired" or "scans where signature confidence was below 0.5."
*Verification:* DuckDB query against persisted traces returns expected matches for representative queries.

### 5.5 Persistence

**REQ-SCAN-PERSIST-001: Scan tables**
The scanner **shall** persist outputs to a `scan_summaries` table (one row per scan execution) and a `scan_records` table (one row per (scan, class) pair). Both tables share a `scan_id` foreign key.
*Verification:* schema migration creates the tables; round-trip test stores and reads representative rows.

**REQ-SCAN-PERSIST-002: Time-series retention**
All historical scan records **shall** be retained indefinitely. They **shall not** be auto-pruned. Operator can manually prune via a CLI command but it requires explicit confirmation.
*Verification:* repeated scans accumulate rows; no automatic deletion observed.

**REQ-SCAN-PERSIST-003: Idempotent re-scanning**
Re-running a scan on the same date **shall** produce a new `scan_id` and a new set of `scan_records`; it **shall not** overwrite prior scans. Each scan is its own immutable observation.
*Verification:* two scans on the same day produce two distinct `scan_id` values and two sets of records.

### 5.6 Provenance

**REQ-SCAN-PROV-001: Per-record provenance**
Every scan record **shall** carry: `scan_id`, `class_id`, `class_definition_version`, `library_version`, `data_as_of` (max of source publication timestamps used), `scan_started_at`, `scan_completed_at`.
*Verification:* DuckDB query returns full provenance per record.

**REQ-SCAN-PROV-002: Source-stale propagation**
When a class's underlying `data_ingest` sources are flagged stale, that staleness **shall** propagate into the scan record's `source_stale_warning` flag.
*Verification:* simulated stale source produces flagged record.

**REQ-SCAN-PROV-003: Library-stale propagation**
When `pattern_library`'s last refresh is older than a configurable threshold (default: 14 days), all scan records **shall** be flagged with `library_stale_warning`. The operator decides whether to refresh or proceed.
*Verification:* simulated old library refresh date produces flagged records.

### 5.7 Configuration

**REQ-SCAN-CONFIG-001: Per-sector candidate thresholds**
The scanner **shall** read candidate thresholds from `config/scanner.yaml` with a default for each of the six sectors. Defaults are tunable per sector.
*Verification:* config-driven test confirms per-sector threshold respected.

**REQ-SCAN-CONFIG-002: Class disable list**
The scanner **shall** support a per-class disable list in config (e.g., for classes the operator considers experimental). Disabled classes are not evaluated; their records are not produced.
*Verification:* config-driven test confirms disabled class skipped with logged reason.

### 5.8 Logging & observability

**REQ-SCAN-LOG-001: Structured scan log**
Each scan **shall** emit a structured JSON log entry with: `scan_id`, classes evaluated, classes succeeded/failed, candidates produced, duration, library version pinned, warnings.
*Verification:* log inspection confirms entry structure.

**REQ-SCAN-LOG-002: Per-class log**
Each class evaluation **shall** log structured: class_id, duration, posterior probability, divergence, warning flags. Failures additionally log the exception.
*Verification:* log scan after a representative run confirms expected entries.

## 6. Non-Functional Requirements

**NFR-SCAN-PERF-001:** A full scan over all registered event classes (v1 seed library = 8 classes plus operator-added) **shall** complete within 5 minutes on the operator's hardware after `pattern_library` is refreshed.

**NFR-SCAN-PERF-002:** A single-class scan **shall** complete within 30 seconds.

**NFR-SCAN-AVAIL-001:** Scanner failures **shall** degrade gracefully — `report_generator` and `mispricing_detector` consumers see an absent or stale scan rather than crashes.

**NFR-SCAN-DISK-001:** v1 scanner tables **shall** stay under 500 MB out of the 100 GB global cap, given the v1 seed scale and daily cadence over the first year of operation.

**NFR-SCAN-DETERMINISM-001:** A scan against the same `data_ingest` snapshot and the same `pattern_library` version **shall** produce identical scan records (excluding timestamps and `scan_id`).

## 7. Open Questions (carry to design phase)

- **OQ-SCAN-001:** Bayesian-update mechanics — the requirements specify Bayesian combination of prior with per-variable hit rates and false-positive rates, but the exact formulation needs to be settled in design. Naive likelihood ratios assume conditional independence between precursors, which is often violated. The design should pick: full naive Bayes, naive Bayes with co-occurrence correction (similar to `pattern_library`'s combine_variables), or a logistic-regression-style combination.
- **OQ-SCAN-002:** Credible-interval propagation — exact analytical propagation through Bayesian update with multiple uncertain inputs is intractable in general. The design should pick an approximation method and document its limitations.
- **OQ-SCAN-003:** "Materially diverged" threshold default — log-odds shift of 0.5 is a reasonable starting point but should be validated against the seed library's empirical divergence distribution. If 80% of seed classes routinely produce log-odds shifts above 0.5, the threshold is too low; if essentially zero do, it's too high.
- **OQ-SCAN-004:** When scan records reference a class whose `definition_version` has changed since the last `pattern_library` refresh, what should happen? Options: skip the class until library refresh, scan with stale library outputs (flagged), refuse the entire scan. Design picks one.
- **OQ-SCAN-005:** Whether to compute and persist a "second-order" indicator — scans where a precursor in one class's signature is also a precursor in another class's signature, and both fire together. This is potentially valuable for cross-class signal but adds substantial complexity. Default disposition: defer to v2.

## 8. Acceptance Criteria

The `signal_scanner` v1 is considered complete when all the following are true:

- A daily scan runs end-to-end within NFR-SCAN-PERF-001.
- Every seed class (and any operator-added class) produces a scan record per scan.
- Candidate situations are correctly identified per the divergence threshold and confidence floor.
- Reasoning traces are populated and renderable.
- Provenance is complete (library version, definition version, data_as_of, all warnings).
- Source-stale and library-stale propagation works.
- Per-class failures isolate; one bad class does not corrupt the scan.
- Re-running a scan produces a new immutable scan rather than overwriting prior.

## 9. References

- LOOM v0.7.0 — `razorrooster.md`, subsystem registry entry for `signal_scanner`.
- `pattern_library` Requirements/Design/Tasks v0.1.0 — for the calibrated outputs the scanner consumes.
- `data_ingest` Requirements/Design/Tasks v0.1.0 — for canonical schemas and freshness.
- System prompt v0.2 — `razorrooster-prompt.md.txt` (output format including reasoning trace).
