# Razor-Rooster v1 Signal Scanner

Operator-facing reference for the live-evaluation layer. The
`signal_scanner` subsystem turns persisted `pattern_library` outputs
into per-class current-conditions probability estimates with reasoning
traces, and surfaces candidate situations where those estimates have
diverged materially from the base rate.

For requirements, design, and task tracking, see
`specs/SIGNAL_SCANNER.md`, `specs/SIGNAL_SCANNER_DESIGN.md`, and
`specs/SIGNAL_SCANNER_TASKS.md`.

## Design principles

- **Read-only consumer of pattern_library.** The scanner reads from
  the public `library` facade, never directly from `pl_*` tables.
- **Failure isolation.** A bad class's evaluation cannot poison
  others — exceptions are captured on the record's `error` field and
  the scan continues.
- **Honest fallbacks.** When current data is missing or stale, the
  posterior equals the prior with `no_update_applied=True`. The
  scanner never fabricates updates from sparse data.
- **Versioned outputs.** Every scan record carries
  `pattern_library_version` and `class_definition_version`. Drift
  between class definition and the persisted outputs is flagged
  loudly via `definition_drift_warning`.
- **Immutable scans.** Each `run_scan` invocation gets a fresh
  `scan_id`; re-running on the same data produces a new record set
  rather than overwriting prior. Calibration backtests have full
  history.
- **Loud warnings, quiet defaults.** Source-stale, library-stale,
  low-confidence-signature, definition-drift — all surface in the
  trace and on the persisted record.

## The five candidate-identification gates

A scan record is marked as a candidate situation only when all five
gates pass:

1. **Magnitude.** The absolute log-odds shift between prior and
   posterior exceeds the per-sector threshold from
   `config/scanner.yaml` (default 0.5 across the board, with
   `geopolitical` at 0.6 to absorb the higher noise floor).
2. **Confidence floor.** The mean signature confidence (across the
   class's precursors) is at or above the configured floor (default
   0.3).
3. **Stale-source eligibility.** When the underlying `data_ingest`
   sources are flagged stale, the record is ineligible by default
   (`stale_source_eligible_for_candidate: false`).
4. **No-update fallback.** Records flagged `no_update_applied`
   (REQ-SCAN-PROB-003) cannot be candidates regardless of magnitude.
5. **Definition-drift advisory.** Drift does not disqualify by
   itself; the operator sees the warning in the trace and decides.

When all five pass, the record is tagged `is_candidate=true` with a
`candidate_direction` of `elevated` (when the shift is positive) or
`depressed` (when negative).

## Posterior computation

The scanner combines the class's base-rate prior with per-precursor
likelihood ratios. For each precursor:

- If the current value crosses the threshold in the class-declared
  direction, the likelihood ratio is `hit_rate / fpr`.
- If it doesn't, the LR is `(1 - hit_rate) / (1 - fpr)`.

The product of LRs across precursors updates the prior odds. A
co-occurrence correction term (default 0 in v1; populated by the
class's signature engine in future revisions) discounts joint signal
when historical co-occurrence is high.

Credible-interval propagation uses 1,000-sample Monte Carlo by
default. Both the prior probability and per-variable hit/false-
positive rates are sampled from beta posteriors that match the
reported point estimates and CIs. The posterior CI is the 2.5th /
97.5th percentile of the resulting posterior sample.

The operator can override `monte_carlo_samples` in
`config/scanner.yaml`. v1 defaults trade off precision vs. cost:
1,000 samples gives sub-second per-class runtime and CI uncertainty
of around ±2 percentage points at the 95th percentile.

## The trace schema

Every scan record has an associated trace JSON with this stable
shape (see `engines/trace.py`):

```json
{
  "class_id": "pheic_declaration_12mo",
  "class_definition_version": 1,
  "library_version": 1,
  "data_as_of": "2026-05-15T08:00:00+00:00",
  "prior": {"point": 0.05, "ci": [0.02, 0.10]},
  "precursors": [
    {
      "variable_id": "who_don_publication_frequency",
      "title": "WHO DON publication frequency (daily)",
      "current_value": 12.0,
      "threshold": 8.0,
      "direction": "high_signals_event",
      "fired": true,
      "hit_rate": 0.65,
      "false_positive_rate": 0.20,
      "likelihood_ratio_applied": 3.25,
      "confidence_score": 0.82,
      "low_confidence_warning": false
    }
  ],
  "co_occurrence_correction": 0.0,
  "posterior": {"point": 0.18, "ci": [0.07, 0.34]},
  "log_odds_shift": 1.43,
  "is_candidate": true,
  "candidate_direction": "elevated",
  "warnings": ["low_sample"],
  "no_update_applied": false,
  "no_update_reason": null,
  "ci_method": "monte_carlo_1000_samples"
}
```

Render the trace to text with `razor-rooster scan show-trace
<scan_id> <class_id>`. Pass `--json` to get the raw payload.

The `report_generator` consumes this same payload to populate the
"top analyses" section of the daily report.

## Storage layout

Three `scan_*` tables live alongside the data_ingest canonical
tables, the `polymarket_*` namespace, and the `pl_*` namespace in
the same DuckDB store at `data/trough.duckdb`:

- `scan_summaries` — one row per scan execution. Aggregate stats.
- `scan_records` — one row per `(scan_id, class_id)` pair. Per-class
  posterior, divergence, candidate flag, warnings, error.
- `scan_traces` — full reasoning trace JSON per
  `(scan_id, class_id)`. Stored separately so queries against
  `scan_records` aren't pessimized by full-trace blobs.

Schema migration version namespacing: `signal_scanner` uses 3001+,
namespaced clear of `data_ingest` (1–999),
`polymarket_connector` (1001–1999), and `pattern_library`
(2001–2999).

## Configuration knobs

`config/scanner.yaml` controls the runtime knobs. The full set:

```yaml
candidate_thresholds:
  log_odds_shift_min: 0.5         # default for all sectors
  per_sector:
    public_health: 0.5
    geopolitical: 0.6             # tolerate slightly higher noise
    regulatory: 0.4
    commodity: 0.5
    climate: 0.5
    infrastructure_energy: 0.5
    macroeconomic: 0.5
    cross_cutting: 0.5
confidence_floor: 0.3
stale_source_eligible_for_candidate: false
library_stale_threshold_days: 14
disabled_classes: []
monte_carlo_samples: 1000
max_workers: 4
```

The defaults are deliberately conservative — they prioritise
keeping false candidates out over surfacing every interesting
signal. Operators tune them after the first real-hardware scan
(T-SCAN-081) once the empirical divergence distribution is known.

## Disk and performance

- v1 disk budget: **500 MB** out of the 100 GB global cap, given
  daily-cadence scans against the v1 seed library plus operator-added
  classes for the first year (NFR-SCAN-DISK-001).
- Full-scan duration target: **under 5 minutes** on EliteBook G8
  hardware (NFR-SCAN-PERF-001) for the eight seed classes against a
  populated `pattern_library` corpus.
- Single-class scan: **under 30 seconds**
  (NFR-SCAN-PERF-002).
- Determinism: a scan against the same `data_ingest` snapshot, the
  same `pattern_library` version, and the same RNG seed produces
  identical scan records (excluding timestamps and `scan_id`)
  per NFR-SCAN-DETERMINISM-001.

## When the scanner produces no_update records

A scan record can complete cleanly with `no_update_applied=true` and
`is_candidate=false` for several reasons:

- **No persisted base rate.** `pattern_library` hasn't refreshed
  this class yet. Run `razor-rooster pattern-library refresh`.
- **All precursor values are missing.** The class's precursor
  queries returned no data over the lookback window (default 30
  days). Often a sign of upstream `data_ingest` staleness — check
  `razor-rooster ingest status` for the relevant sources.
- **All sources stale.** Even if some precursor data exists, the
  freshness gate marks the entire class as no-update when no
  precursor produced a usable value.

These records are not failures; they correctly report "no signal in
either direction this cycle." The trace's `no_update_reason` field
explains which condition triggered the fallback.

## After T-SCAN-081 measurements

The first real-hardware scan against the operator's populated
`data_ingest` corpus and refreshed `pattern_library` produces the
empirical divergence distribution that validates the v1 default
threshold. Update DEFER-SCAN-001 in
`specs/SIGNAL_SCANNER_TASKS.md` with measured numbers:

- Per-sector empirical divergence distribution (5th, 25th, 50th,
  75th, 95th percentiles of `|log_odds_shift|`).
- Per-sector candidate rate at the v1 default threshold.
- Per-class single-scan duration.

If a sector's candidate rate is anomalous (~0% or ~100%), revise
the per-sector threshold in `config/scanner.yaml` and document the
revision in the task tracking. Healthy candidate rates are roughly
10–25% of evaluated classes per scan.

## See also

- `razorrooster.md` — LOOM (project state of truth)
- `specs/SIGNAL_SCANNER.md`, `specs/SIGNAL_SCANNER_DESIGN.md`,
  `specs/SIGNAL_SCANNER_TASKS.md` — full requirements / design / tasks
- `docs/pattern_library.md` — the eight seed classes the scanner
  evaluates by default
- `src/razor_rooster/signal_scanner/engines/` — the
  posterior, candidates, trace, and orchestration modules
