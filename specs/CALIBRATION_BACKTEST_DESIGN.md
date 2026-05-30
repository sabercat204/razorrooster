# CALIBRATION_BACKTEST — Design

**Subsystem:** `calibration_backtest`
**Codename:** The Cockerel's Mirror
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-29
**Companion spec:** `CALIBRATION_BACKTEST.md` (Requirements v0.1.0)

---

## 1. Overview

`calibration_backtest` is the gating artifact behind operator reliance on the rest of the Razor-Rooster stack. It replays the system as it currently stands against historical Polymarket resolutions, computes Brier and reliability statistics, and stores the result as a versioned, idempotent record. It produces no trading directives, no sizing recommendations, and no orders; the output is descriptive calibration evidence the operator reads alongside their own thinking. Per OT-004, v1 remains paper-analysis only regardless of how favourable the calibration record turns out to be.

The subsystem is a downstream consumer: it reads from `data_ingest.*`, `polymarket_resolutions`, `comparison_resolutions`, `class_market_mappings`, and registered `pattern_library` event classes; it writes only to `backtest_runs`, `backtest_predictions`, and `backtest_traces`. It performs no network egress. All operator-facing renders pass through `position_engine.frame.linter.check_text` so the conditional-only framing is preserved (REQ-CB-CLI-002).

Discipline rules:

1. **Time honesty** — every replayed prediction would only see data published by `prediction_ts` (REQ-CB-FREEZE-001), with a hard `--lag-days` floor (default 7, minimum 1, never 0).
2. **Reuse, do not fork** — the replay loop calls existing `signal_scanner` entry points unchanged (REQ-CB-REPLAY-002); calibration_backtest owns time-freezing, not posterior math.
3. **Determinism** — the same parameter tuple, library version, and system revision would yield the same `run_id` and persisted summary (REQ-CB-RUN-001, REQ-CB-RUN-004).
4. **Honest fallback** — missing data, invalidated resolutions, or absent mappings would be recorded as `skipped` with a structured reason rather than silently dropped (REQ-CB-REPLAY-004).
5. **Conditional language only** — every rendered output frames findings as observations the operator could consider, never as directives to act.

## 2. Resolved Open Questions

### OQ-CB-001 — Scanner posterior at frozen time

**Resolution:** Precompute the time-frozen state in the backtest and call `signal_scanner.engines.posterior.posterior_with_ci()` directly, with calibration_backtest orchestrating precursor evaluation against frozen data. The scanner's internal `evaluate_class()` (in `engines/scanner.py`) is not itself reused; instead, calibration_backtest owns the orchestration that scanner.run_scan() owns in production, but with frozen-time precursor inputs.

**Reasoning:** Satisfying REQ-CB-FREEZE-001 by adding `WHERE source_publication_ts <= as_of_ts` to precursor queries is a data-source concern. The signal_scanner's `engines/scanner.py.evaluate_class()` is an internal workhorse with signature `(scan_id, cls, library_version, strict) -> tuple[ScanRecord, Trace]` and is not a public API entry point. Rather than extend the scanner API (which would conflict with REQ-CB-REPLAY-002 "called without modification"), calibration_backtest reuses the lower-level public functions: precursor evaluation against a frozen view, then `posterior_with_ci()` for the posterior arithmetic. This keeps the scanner unchanged and concentrates the freezing logic in calibration_backtest.

**Design implications:**
- The replay engine derives `as_of_ts = prediction_ts` and threads it into the precursor invocation layer (`signal_scanner.engines.posterior.evaluate_precursors` or equivalent), not into a higher-level scanner signature.
- calibration_backtest exposes a thin orchestration helper `evaluate_class_at_frozen_time(class_id, prediction_ts, frozen_state) -> (model_p, trace)` that wraps the precursor + posterior calls.
- Precursor queries lacking `source_publication_ts` filtering would be flagged `source_data_not_frozen` and skipped from scoring; legacy classes degrade visibly.

### OQ-CB-002 — Mapping state at prediction time

**Resolution:** v1 uses the current `class_market_mappings` row, with polarity routed through `comparison_resolutions.polarity_at_comparison` when available; an explicit `polarity_source` flag records which path was taken. Full historical mapping state is deferred to v2.

**Reasoning:** For predictions made after the system shipped, the linkage pass already captures `polarity_at_comparison`. For pre-system replays, current-mapping fallback is acceptable provided it is visible and quantified. A schema bump for effective-date tracking would couple `calibration_backtest` to the `mispricing_detector` core schema and delay OT-003 closure with no proportionate v1 benefit.

**Design implications:**
- `backtest_predictions` carries `polarity_source` and a `mapping_mismatch_warning` boolean.
- The polarity resolution mechanism is unified with OQ-CB-005; see that resolution for the exact preference order and helper signature.
- The run summary surfaces a `fallback_polarity_rate` field computed as `fallback_polarity_count / predictions_scored`. The terminal/markdown render highlights this rate in the run summary header. If the rate exceeds five percent on a real run, the render emits a one-line note recommending operator review and DEFER-CB-003 (v2 historical-mapping promotion) is triggered as a next-cycle task. The threshold is informational; it does not gate the run's `status='complete'` transition.
- REQ-CB-REPLAY-001 reads with the documented exemption: mappings are evaluated as-of backtest invocation time.

### OQ-CB-003 — Compare ranking knob

**Resolution:** A CLI flag `--compare-rank-by {absolute|percent}` defaulting to `absolute`. Both metrics are surfaced regardless (REQ-CB-SCORE-005); the flag controls only sort order.

**Reasoning:** The `report_generator` digest already establishes run-time sort flags rather than config-file knobs. Per-run parameterisation keeps the `run_id` hash clean since sort order is a visualisation concern. Absolute Brier delta is the more intuitive default; operators interested in relative fragility could pass `--compare-rank-by percent`.

**Design implications:**
- The compare engine carries both metrics on every cell; a single sort-key swap suffices.
- A future `threshold` mode (rank by miscalibration-flag transitions) is parked for v2.

### OQ-CB-004 — Trace storage policy

**Resolution:** Traces are always-on, stored compressed (zstd) in `backtest_traces`, decoded on read.

**Reasoning:** Per-prediction traces are the diagnostic backbone of `compare`. Opt-in flags create discoverability gaps, and sampling breaks determinism. Compression keeps a 4000-prediction run at roughly 1.5 MB on disk, fitting inside the 100 MB allocation across many consecutive runs.

**Design implications:**
- `backtest_traces.trace_json_compressed` is a `BLOB`, with a `compression_algorithm` column for forward compatibility.
- Decompression overhead is single-digit milliseconds per trace and would not pressure REQ-CB-PERF-001.

### OQ-CB-005 — Polarity reconciliation

**Resolution:** A single helper `polarity.resolve(prediction_ts, condition_id, class_id) -> (polarity, source)` centralises the logic. The preference order, unified with OQ-CB-002, is:

1. Query `comparison_resolutions.polarity_at_comparison` for the `(condition_id, class_id)` pair; if a row exists, return `(polarity, 'comparison_resolutions')`.
2. Otherwise, fall back to the current `class_market_mappings` row for the same pair; if present, return `(polarity, 'current_mapping_fallback')` and set `mapping_mismatch_warning=True` on the prediction row.
3. If neither exists, raise `NoPolarityError`; the caller skips the prediction with `skip_reason='no_polarity_resolution'`.

**Reasoning:** The forward-going linkage pass produces `polarity_at_comparison` precisely so downstream consumers do not need to reason about temporal mapping state. Where the comparison exists, using the frozen polarity aligns exactly with how `mispricing_detector` records the outcome.

**Design implications:**
- All three return paths populate `polarity_source` for auditing; the run summary aggregates by source so the operator can see how much of the score depended on the fallback.
- Predictions with no resolvable polarity are skipped under reason `no_polarity_resolution`.

### OQ-CB-006 — Reliability bin count source

**Resolution:** Default bin count is loaded from `config/report.yaml` (`thresholds.reliability_bin_count`); CLI overrides `--bin-count N` and `--bin-count-per-sector SECTOR=N` apply per-run. Bin count is excluded from the `run_id` hash.

**Reasoning:** REQ-CB-SCORE-004 calls for bit-equal output between the daily report and the backtest over overlapping windows. Aligning defaults with the report's config eliminates accidental drift; a CLI override preserves the per-run parameterisation pattern. Bin count is a visualisation parameter; including it in `run_id` would invalidate cache entries for cosmetic changes.

**Design implications:**
- `calibration_backtest.scoring` imports `report_generator.config.loader` to read `ReportThresholds` at run start.
- Override resolution order: CLI flag > per-sector config override > global config value > module default.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      calibration_backtest/
        __init__.py
        api.py                              # public surface: run_backtest, compare, list_runs, show_run
        cli.py                              # commands: run, list, show, compare, prune
        models.py                           # BacktestRun, BacktestPrediction, ScoreSummary, CompareCell
        version.py                          # SUBSYSTEM_REVISION + run_id canonicalisation
        errors.py                           # DiskBudgetError, RecentWindowError, NoPolarityError
        engines/
          __init__.py
          replay.py                         # main replay loop; iterates polymarket_resolutions
          freezer.py                        # source_publication_ts guard + lag enforcement
          polarity.py                       # comparison_resolutions / mapping fallback resolution
          scoring.py                        # Brier per overall/sector/class + reliability binning
          compare.py                        # run-vs-run cell-level diff and ranking
          trace_codec.py                    # zstd encode/decode for backtest_traces
        persistence/
          __init__.py
          schemas.py                        # backtest_runs, backtest_predictions, backtest_traces
          operations.py                     # idempotent insert/update; cached-summary fast path
          migrations/
            m6001_calibration_backtest_initial.py
            m6002_polarity_source_columns.py
        config/
          backtest.yaml                     # disk cap, default lag_days, allow_recent default false
        tests/
          fixtures/

### 3.2 Reuse from Other Subsystems

- From `data_ingest`: `DuckDBStore`, structured logging, source-publication-timestamp filtering pattern.
- From `pattern_library`: `LIBRARY_VERSION` (constant for static reference), `current_version()` (function for live queries), `list_classes()`, the registered `EventClass` set, and the upgraded `polymarket_resolution_calibration` meta-class (REQ-CB-PL-001). The meta-class queries DuckDB directly (joining `comparison_resolutions` to `polymarket_resolutions`) and does not import from `calibration_backtest`, preserving the no-circular-dependency constraint (REQ-CB-PL-002).
- From `signal_scanner`: `engines.posterior.posterior_with_ci()` and the precursor evaluation entry point in `engines.posterior`, both called without modification. The internal `engines.scanner.evaluate_class()` is intentionally NOT reused; calibration_backtest re-implements the orchestration step against frozen-time precursor inputs (see OQ-CB-001).
- From `mispricing_detector`: `comparison_resolutions` (read-only), `class_market_mappings` (read-only), the polarity convention used in `engines.linkage`.
- From `polymarket_connector`: `polymarket_resolutions` (ground truth source).
- From `report_generator`: `config.loader.load_config`, `engines.section_assemblers.reliability` (binning helper), `frame.linter.check_text`.
- From `position_engine`: `frame.linter.check_text` (shared with report_generator) for the conditional-language guard.

### 3.3 Tables

#### `backtest_runs`

    run_id                        VARCHAR     PRIMARY KEY    -- SHA-256 hex
    since_ts                      TIMESTAMP   NOT NULL
    until_ts                      TIMESTAMP   NOT NULL
    lag_days                      INTEGER     NOT NULL
    class_ids_json                JSON        NOT NULL        -- sorted array
    sectors_json                  JSON        NOT NULL        -- sorted array
    venues_json                   JSON        NOT NULL        -- sorted array
    library_version               INTEGER     NOT NULL
    system_revision               VARCHAR     NOT NULL        -- git rev-parse HEAD
    started_at                    TIMESTAMP   NOT NULL
    completed_at                  TIMESTAMP   NULL
    status                        VARCHAR     NOT NULL        -- 'in_progress' | 'complete' | 'failed'
    error_summary                 TEXT        NULL
    predictions_total             INTEGER     NOT NULL DEFAULT 0
    predictions_scored            INTEGER     NOT NULL DEFAULT 0
    predictions_skipped           INTEGER     NOT NULL DEFAULT 0
    overall_brier                 DOUBLE      NULL
    summary_json                  JSON        NULL            -- per-sector / per-class aggregates
    bin_count_global              INTEGER     NOT NULL
    bin_count_per_sector_json     JSON        NOT NULL
    fallback_polarity_count       INTEGER     NOT NULL DEFAULT 0
    allow_recent                  BOOLEAN     NOT NULL DEFAULT FALSE
    disclaimer_version            VARCHAR     NOT NULL

Indexes: `(status, started_at)`, `(library_version, system_revision)`.

#### `backtest_predictions`

    run_id                        VARCHAR     NOT NULL
    prediction_id                 VARCHAR     NOT NULL        -- UUID per row
    class_id                      VARCHAR     NOT NULL
    condition_id                  VARCHAR     NOT NULL
    venue                         VARCHAR     NOT NULL
    sector                        VARCHAR     NOT NULL
    prediction_ts                 TIMESTAMP   NOT NULL
    resolution_ts                 TIMESTAMP   NOT NULL
    model_p                       DOUBLE      NULL
    observed                      DOUBLE      NULL            -- already polarity-corrected
    polarity                      VARCHAR     NULL            -- 'direct' | 'inverted'
    polarity_source               VARCHAR     NOT NULL        -- 'comparison_resolutions' | 'current_mapping_fallback' | 'unresolved'
    mapping_mismatch_warning      BOOLEAN     NOT NULL DEFAULT FALSE
    definition_version            INTEGER     NOT NULL
    status                        VARCHAR     NOT NULL        -- 'scored' | 'skipped'
    skip_reason                   VARCHAR     NULL            -- e.g. 'insufficient_lag', 'invalid_resolution', 'source_data_not_frozen'
    brier_contribution            DOUBLE      NULL

Primary key: `(run_id, prediction_id)`.
Indexes: `(run_id, sector)`, `(run_id, class_id)`, `(run_id, status)`.

#### `backtest_traces`

    run_id                        VARCHAR     NOT NULL
    prediction_id                 VARCHAR     NOT NULL
    trace_json_compressed         BLOB        NOT NULL        -- zstd
    compression_algorithm         VARCHAR     NOT NULL DEFAULT 'zstd'
    decompressed_size_bytes       INTEGER     NOT NULL

Primary key: `(run_id, prediction_id)`. Stored separately so summary queries against `backtest_predictions` would not be pessimised by trace payloads.

### 3.4 Run Identification

`version.py` canonicalises the input parameter tuple before hashing:

```python
def compute_run_id(p: RunParameters, library_version: int, system_revision: str) -> str:
    canonical = json.dumps({
        "since_ts": p.since_ts.isoformat(), "until_ts": p.until_ts.isoformat(),
        "lag_days": p.lag_days,
        "class_ids": sorted(p.class_ids), "sectors": sorted(p.sectors), "venues": sorted(p.venues),
        "library_version": library_version, "system_revision": system_revision,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Bin count, output format, and `--compare-rank-by` are intentionally absent per OQ-CB-006. A `definition_version` change on any in-scope class would shift `library_version` (per `pattern_library.bump_for_reason`) and therefore the `run_id` (REQ-CB-FREEZE-003).

### 3.5 Replay Loop

`engines/replay.py` walks resolutions in ascending `resolution_ts`. The iteration pre-filters resolutions to only those with at least one active mapping for the in-scope `class_ids`, so the inner mapping loop never runs zero times for a yielded resolution. Pre-filtering happens in the SQL query (a `JOIN class_market_mappings cm ON cm.condition_id = pr.condition_id WHERE cm.class_id IN :params.class_ids`) rather than in Python, to keep the iterator memory-bounded.

```python
def run_backtest(params: RunParameters) -> BacktestRun:
    # Bin count resolution: CLI flag > per-sector config > global config > module default
    bin_count_global, bin_count_per_sector = resolve_bin_counts(params)
    library_version = pattern_library.current_version()
    system_revision = resolve_system_revision()                # see below
    run_id = compute_run_id(params, library_version, system_revision)
    cached = persistence.get_run(run_id)
    if cached and cached.status == "complete":
        return cached                                          # REQ-CB-RUN-004
    if params.until_ts > now() - timedelta(days=30) and not params.allow_recent:
        raise RecentWindowError(...)                           # REQ-CB-RUN-002
    projected_mb = estimate_disk_footprint(params)             # see Section 3.8
    if projected_mb > config.disk_cap_mb:
        raise DiskBudgetError(projected_mb, config.disk_cap_mb)
    persistence.insert_run(
        run_id, library_version=library_version, system_revision=system_revision,
        bin_count_global=bin_count_global, bin_count_per_sector_json=bin_count_per_sector,
        status="in_progress", ...,
    )
    # Pre-filtered iterator: only resolutions whose condition_id has at least one
    # active mapping into params.class_ids; resolutions with zero mapped classes
    # never reach Python.
    for resolution in iter_mapped_resolutions(params.since_ts, params.until_ts,
                                              params.venues, params.class_ids):
        if resolution.invalidated:
            persistence.insert_skip(run_id, resolution, reason="invalid_resolution"); continue
        for mapping in active_mappings_for(resolution.condition_id, params.class_ids):
            try:
                prediction_ts = derive_prediction_ts(resolution, params.lag_days)
                if (resolution.resolution_ts - prediction_ts).days < params.lag_days:
                    persistence.insert_skip(..., reason="insufficient_lag"); continue
                frozen = freezer.freeze(prediction_ts)
                if frozen is None:                              # precursor sources lacked source_publication_ts
                    persistence.insert_skip(..., reason="source_data_not_frozen"); continue
                try:
                    polarity_value, source = polarity.resolve(
                        prediction_ts, resolution.condition_id, mapping.class_id)
                except NoPolarityError:
                    persistence.insert_skip(..., reason="no_polarity_resolution"); continue
                try:
                    model_p, trace = evaluate_class_at_frozen_time(
                        mapping.class_id, prediction_ts, frozen)
                except InsufficientPrecursorData:
                    persistence.insert_skip(..., reason="insufficient_data"); continue
                observed = polarity_correct(resolution.outcome, polarity_value)
                persistence.insert_prediction(run_id, ..., model_p, observed, source)
                persistence.insert_trace(run_id, prediction_id, trace_codec.encode(trace))
            except Exception as exc:                            # REQ-CB-RUN-005 isolation
                log.exception("per-prediction failure", run_id=run_id, mapping=mapping)
                persistence.insert_skip(..., reason="exception", error_summary=str(exc))
                continue                                        # never aborts the run
    persistence.complete_run(
        run_id,
        scoring.aggregate(run_id, bin_count_global, bin_count_per_sector),
        status="complete",
    )
    return persistence.get_run(run_id)


def resolve_system_revision() -> str:
    # REQ-CB-RUN-003: capture system identity even when git is unavailable.
    try:
        rev = git_head()                                        # `git rev-parse HEAD`
        if rev:
            return rev
    except (GitNotInstalledError, NotAGitRepoError, GitCommandError):
        pass
    # Fallbacks, in order:
    if env := os.environ.get("RAZOR_ROOSTER_SYSTEM_REVISION"):
        return f"env:{env}"
    if pkg := package_version("razor_rooster"):                  # importlib.metadata
        return f"pkg:{pkg}"
    return "unversioned"
```

Per-resolution work is embarrassingly parallel; a bounded `ThreadPoolExecutor` (default four workers) is used as in `signal_scanner`.

**Failure isolation (REQ-CB-RUN-005).** The outer `try/except Exception` wraps every per-prediction unit of work; any uncaught error is converted into a `skipped` row with `skip_reason='exception'` and the exception's string captured in `error_summary`. Specific recoverable failures map to dedicated reasons before the catch-all engages: `NoPolarityError` → `no_polarity_resolution`, `InsufficientPrecursorData` → `insufficient_data`, frozen-state unavailable → `source_data_not_frozen`, lag-floor violation → `insufficient_lag`, invalidated resolution → `invalid_resolution`. The closed enumeration is documented in Section 3.13.

**Prediction timestamp derivation (REQ-CB-FREEZE-002).** `derive_prediction_ts(resolution, lag_days)` returns `resolution.resolution_ts - timedelta(days=lag_days)` — the exact wall-clock instant `lag_days` before settlement. Precursor data is then frozen at that instant via `freezer.freeze(prediction_ts)` (i.e., `WHERE source_publication_ts <= prediction_ts`). This means `prediction_ts` is the simulated decision instant, not the timestamp of the most recent data point that happens to fall before it; data that lands exactly at `prediction_ts` is admitted (boundary equality), data at `prediction_ts + 1ns` is excluded.

### 3.6 Scoring and Aggregation

`engines/scoring.py` implements REQ-CB-SCORE-001 through REQ-CB-SCORE-004: overall Brier as `sum((model_p - observed) ** 2) / count`; per-sector and per-class restrictions of the same arithmetic, with zero-prediction sectors reported under `zero_resolutions_sectors`; reliability diagrams produced by invoking `report_generator.engines.section_assemblers.reliability` directly so the output would be bit-equal to the daily report over overlapping windows. The aggregate is stored as JSON on `backtest_runs.summary_json`.

### 3.7 Compare Engine

`engines/compare.py` joins two runs on `(sector, class_id)` (full outer join) and emits one `CompareCell` per cell that appears in either run, carrying `brier_a`, `brier_b`, `delta_absolute`, `delta_percent`, `crossed_miscalibration_threshold`, `present_in` (one of `'both' | 'a_only' | 'b_only'`), and an optional `trace_diff_summary`. Sorting honours `--compare-rank-by` per OQ-CB-003.

**Missing-cell semantics (REQ-CB-SCORE-005).** When one side has no rows for a `(sector, class_id)` cell:

- `brier_a` or `brier_b` is `None`.
- `delta_absolute` and `delta_percent` are `None`; the cell appears at the bottom of the ranked output, regardless of `--compare-rank-by` mode, with a `present_in` annotation so the operator can see which run introduced or dropped the cell.
- `crossed_miscalibration_threshold` is `None` for asymmetric cells; the boolean only fires when both sides have a comparable score. The terminal/markdown render shows the flag as a dash for these rows.

Trace diffs are produced lazily via `trace_codec.decode` so the compare command would not pay decompression cost for cells the operator does not request.

### 3.8 Configuration

`config/backtest.yaml`:

    version: 1
    default_lag_days: 7
    minimum_lag_days: 1
    disk_cap_mb: 100
    allow_recent_default: false
    max_workers: 4
    trace_compression: zstd
    trace_compression_level: 3

`reliability_bin_count` is intentionally absent; the loader would consult `config/report.yaml` per OQ-CB-006. The resolution order, applied by `resolve_bin_counts(params)` at run start, is: (1) CLI flag `--bin-count N` for the global value and `--bin-count-per-sector S=N` for per-sector overrides; (2) `report.yaml` `thresholds.reliability_bin_count_per_sector` for any sector still unset; (3) `report.yaml` `thresholds.reliability_bin_count` as the global default; (4) module default of 10. Both `bin_count_global` and `bin_count_per_sector_json` are persisted on `backtest_runs` so the resolved values are auditable.

**Disk footprint estimation (REQ-CB-PERSIST-003).** Before the run begins, `estimate_disk_footprint(params)` projects total bytes:

    estimated_predictions = count_predictions_in_window(params)        # SQL prefilter count
    raw_row_bytes         = 1024     # backtest_predictions row overhead
    raw_trace_bytes       = 4096     # uncompressed trace JSON, conservative
    compression_ratio     = 0.25     # zstd level 3 on structured JSON, ~4x shrink (Section 6)
    projected_bytes       = estimated_predictions * (raw_row_bytes + raw_trace_bytes * compression_ratio)
    projected_mb          = projected_bytes / (1024 * 1024) + summary_overhead_mb(=2)

If `projected_mb > config.disk_cap_mb`, `DiskBudgetError` is raised before any rows are inserted. The 1.5 MB-per-4000-predictions empirical figure (Section 6) bounds the projection sanity-check; DEFER-CB-001 promotes a real-corpus measurement that can refine `compression_ratio`.

### 3.9 CLI

    razor-rooster calibration-backtest run [--since ISO] [--until ISO] [--lag-days N] [--class-id ID]...
                                           [--sector S]... [--venue V]... [--bin-count N]
                                           [--bin-count-per-sector S=N]... [--allow-recent]
                                           [--output {terminal,markdown,html,json}]
    razor-rooster calibration-backtest list [--since ISO] [--limit N]
    razor-rooster calibration-backtest show RUN_ID [--output FORMAT]
    razor-rooster calibration-backtest compare RUN_A RUN_B [--compare-rank-by {absolute,percent}] [--top N]
    razor-rooster calibration-backtest prune --before ISO --confirm

Every render path except `--output json` would pass through `frame.linter.check_text` per REQ-CB-CLI-002. The JSON output would bypass the linter (REQ-CB-CLI-003) but would carry an explicit `disclaimer` field with the standard footer disclaimer text (Section 3.12).

**Framing discipline reminder for implementers.** All strings used in terminal, markdown, and html renders must use conditional language only: "would", "could", "might", "if the operator chose to". The linter (Section 3.12) rejects forbidden imperative phrases sourced from `config/forbidden_phrases.yaml`. Implementers must not work around the linter by substituting indirect phrasing or synonyms; if a needed phrase appears to require a directive form, raise the question rather than rephrase silently. The forbidden_phrases.yaml file is shared with `position_engine` and `report_generator` per REQ-PE-FRAME-002. If the file is missing at runtime, the render fails with a clear error rather than bypassing the check.

### 3.10 Threat Model

Threat context: STANDARD. Risks:

1. **Future-leaked data corrupts calibration evidence.** Mitigation: `freezer` adds `source_publication_ts <= prediction_ts` filters; classes lacking the filter would be flagged `source_data_not_frozen` and excluded from scoring (OQ-CB-001).
2. **Settlement-window contamination.** Mitigation: hard `--lag-days` floor (default 7, minimum 1) plus `RecentWindowError` for `until_ts` within 30 days unless `--allow-recent` is set (REQ-CB-RUN-002).
3. **Polarity confusion biases the score.** Mitigation: `polarity_source` recorded on every row; fallback rate surfaced in run summary (OQ-CB-002, OQ-CB-005).
4. **Operator over-relies on a green Brier number.** Mitigation: (a) every terminal, markdown, and html render passes the conditional-language linter; (b) every such render includes the standard disclaimer block (Section 3.12) at the top of the report; (c) JSON output includes the disclaimer as a top-level `disclaimer` field; (d) the run summary header explicitly restates the v1 paper-analysis contract: "This backtest is decision-support analysis. The system does not place orders or recommend actions. Paper-analysis remains the v1 contract regardless of calibration outcome." (REQ-CB-CLI-002, OT-004).
5. **Disk budget overrun.** Mitigation: `DiskBudgetError` raised before insert when projected footprint would exceed 100 MB (REQ-CB-PERSIST-003).
6. **Untrusted source content as instructions.** The backtest only consumes structured numeric/categorical fields; trace rendering uses fixed format strings.

### 3.11 Trace Serialization Pipeline

The trace artifact flows through four explicit stages, with `trace_codec` owning the boundary:

1. **Producer.** `signal_scanner.engines.posterior` (and its precursor helpers) returns a `Trace` object whose `to_dict()` method yields a structured dict per the scanner trace schema (precursor names, frozen counts, posterior intermediates, definition-version stamp).
2. **Serialize.** `trace_codec.encode(trace_dict)` calls `json.dumps(trace_dict, sort_keys=True, separators=(",", ":"))` to produce a canonical UTF-8 byte string. Sorted keys guarantee determinism for trace diffing.
3. **Compress.** The encoder pipes the JSON bytes through `zstd.compress(level=config.trace_compression_level)` and records `decompressed_size_bytes` for the persistence row. The compression algorithm is recorded in `compression_algorithm` so future readers can branch on alternate codecs without a schema migration.
4. **Persist / decode.** `persistence.insert_trace` writes the BLOB. On read, `trace_codec.decode(blob, algorithm)` decompresses to bytes, `json.loads` to a dict, and returns the dict to callers (`compare`, `show`). The codec is symmetric: `decode(encode(t)) == t` under sorted-key normalisation.

Round-trip equality is guarded by a unit test that pickles a representative scanner trace and asserts `decode(encode(t)) == t`. This pipeline is the single contract between the scanner's trace shape and calibration_backtest's storage; if the scanner's trace schema changes, only `trace_codec` (not the persistence layer) needs to adapt.

### 3.12 Disclaimer Text

Every operator-facing render (terminal, markdown, html, json) carries the disclaimer block below. The exact text is centralised as a module constant `calibration_backtest.frame.DISCLAIMER` and is the same string used by `position_engine` and `report_generator` per REQ-PE-FRAME-001 and REQ-RG-FRAME-004.

**Standard disclaimer block** (placed at the top of every terminal/markdown/html render and as a top-level `disclaimer` field in JSON):

> This output is decision-support calibration evidence, not a trading recommendation. Razor-Rooster does not place orders, execute trades, or size positions on behalf of the operator. Brier scores, reliability bins, and Kelly figures shown anywhere in the system are theoretical optima derived from the model's stated probabilities; whether the model's probabilities are accurate, whether displayed market prices are tradeable in the operator's chosen venue, and whether to act on any disagreement between model and market are operator judgments. Any per-cell, per-sector, or aggregate calibration result describes how the system would have performed if it had run historically; it does not predict future calibration. Paper-analysis remains the v1 contract regardless of calibration outcome.

**Footer note** (appended to terminal/markdown/html only, not json):

> Disagreement between the model and a market is an observation. The operator decides, the system describes.

The disclaimer version is recorded on `backtest_runs.disclaimer_version` so any future rewording is auditable. The linter (`config/forbidden_phrases.yaml`) rejects strings containing `place an order`, `execute the trade`, `you should buy`, `you should sell`, `guaranteed profit`, `will profit`, and similar imperative or certainty-claiming phrases. The forbidden-phrases file is shared across subsystems; if it cannot be loaded, all render paths fail rather than skip the check.

### 3.13 Skip Reason Enumeration

`backtest_predictions.skip_reason` is a closed enumeration; any value outside this list is a bug and would cause the run to fail loudly:

| Reason                       | Cause                                                                                  |
|------------------------------|----------------------------------------------------------------------------------------|
| `insufficient_lag`           | `resolution_ts - prediction_ts < lag_days`                                             |
| `invalid_resolution`         | `polymarket_resolutions.invalidated = TRUE`                                            |
| `source_data_not_frozen`     | A precursor source lacks `source_publication_ts` filtering; freezer returns None       |
| `no_polarity_resolution`     | Neither `comparison_resolutions.polarity_at_comparison` nor `class_market_mappings` resolves polarity |
| `insufficient_data`          | Frozen-time precursors yield fewer rows than the class definition's minimum support     |
| `exception`                  | Any uncaught error during per-prediction work; `error_summary` carries the exception string |

The enumeration is enforced by a `CHECK` constraint on the column and by a unit test that exhaustively iterates all literals against the migration schema.

### 3.14 GUI Surface (REQ-CB-CLI-004)

The v1 design specifies two read-only routes; full template implementation is DEFER-CB-005:

- `GET /calibration-backtest` — index. Lists runs from `backtest_runs` ordered by `started_at DESC`, with columns: `run_id` (truncated 12-char prefix linked to detail page), `started_at`, `library_version`, `system_revision` (truncated), `lag_days`, `predictions_total`, `predictions_scored`, `overall_brier`, `fallback_polarity_rate`, `status`. The page carries the standard disclaimer block at the top (Section 3.12).
- `GET /calibration-backtest/{run_id}` — detail. Renders the full `summary_json` with per-sector Brier table, reliability diagrams (rendered as inline SVG by reusing `report_generator.engines.section_assemblers.reliability` HTML output), the resolved bin counts, the fallback-polarity rate banner if greater than 5 percent, and a paged view of `backtest_predictions` filtered by `status` and `skip_reason`. The standard disclaimer is at the top; the footer note is at the bottom.

Both routes pass every rendered string through `frame.linter.check_text` exactly as the CLI does. Authentication and session management reuse `report_generator`'s existing operator auth; no new auth surface is introduced. No write routes exist; pruning remains CLI-only.

### 3.15 No Circular Dependency (REQ-CB-PL-002)

`calibration_backtest` is strictly downstream. The dependency graph is one-directional:

    pattern_library / signal_scanner / mispricing_detector / report_generator / position_engine
                                            |
                                            v
                                  calibration_backtest

No upstream subsystem imports from `calibration_backtest`. In particular:

- The `polymarket_resolution_calibration` meta-class lives in `pattern_library` and queries DuckDB tables (`comparison_resolutions`, `polymarket_resolutions`, `class_market_mappings`) directly. It does not import `calibration_backtest.*`.
- `report_generator` consumes `backtest_runs.summary_json` only by reading the table; it does not import calibration_backtest functions.
- `signal_scanner` is unaware of calibration_backtest; calibration_backtest depends on signal_scanner, never the reverse.

An integration test (Section 4.2) statically asserts that `grep -r "from calibration_backtest" razor_rooster/{pattern_library,signal_scanner,mispricing_detector,report_generator,position_engine,data_ingest,polymarket_connector}` returns zero matches. The same test asserts the AST-level absence of any `import calibration_backtest` in the same packages.

### 3.16 Meta-Class Query (REQ-CB-PL-001)

The `polymarket_resolution_calibration` meta-class in `pattern_library` upgrades from a synthetic precursor placeholder to a real-occurrence query against the comparison/resolution tables. The meta-class owns its query; calibration_backtest provides no helper. The query shape:

```sql
SELECT
  cr.condition_id,
  cr.class_id,
  cr.polarity_at_comparison,
  pr.outcome,
  pr.resolution_ts,
  pr.invalidated
FROM comparison_resolutions cr
JOIN polymarket_resolutions pr
  ON pr.condition_id = cr.condition_id
WHERE pr.resolution_ts BETWEEN :since_ts AND :until_ts
  AND pr.invalidated = FALSE
  AND cr.class_id = :class_id
```

The meta-class then computes its own `(model_p, observed)` pair using the polarity-corrected outcome. The query lives in `pattern_library/classes/meta/polymarket_resolution_calibration.py` and is exercised by a unit test in pattern_library's suite, not in calibration_backtest's suite, preserving the directional dependency in Section 3.15.

### 3.17 Properties

The following invariants would hold across every successful run:

- **P-CB-1 (Determinism).** For a fixed `(params, library_version, system_revision)`, two invocations would produce the same `run_id` and bit-equal `summary_json` (REQ-CB-RUN-001, REQ-CB-RUN-004).
- **P-CB-2 (Time honesty).** Every scored prediction would satisfy `source_publication_ts <= prediction_ts` for every precursor consumed and `resolution_ts - prediction_ts >= lag_days` (REQ-CB-FREEZE-001, REQ-CB-FREEZE-002).
- **P-CB-3 (Polarity coherence).** Every scored prediction would carry a non-null `polarity_source`; `observed` would be the polarity-corrected projection of `polymarket_resolutions.outcome` (REQ-CB-REPLAY-003).
- **P-CB-4 (Bin alignment).** For any overlapping `[since_ts, until_ts]` window between a backtest run and the daily report run with matching `bin_count`, the per-sector reliability bins would be bit-equal within 1e-9 tolerance (REQ-CB-SCORE-004).
- **P-CB-5 (Skip transparency).** Every prediction with `status='skipped'` would carry a `skip_reason` from the closed enumeration; no silent drops (REQ-CB-REPLAY-004).
- **P-CB-6 (Append-only persistence).** Once `status='complete'`, a `backtest_runs` row would not be mutated; new evidence would land in a new run (REQ-CB-PERSIST-001).
- **P-CB-7 (Framing linter).** No operator-facing render except `--output json` would ship without passing `frame.linter.check_text`; the JSON payload would carry an explicit disclaimer field (REQ-CB-CLI-002, REQ-CB-CLI-003).

## 4. Test Strategy

### 4.1 Unit Tests

- `compute_run_id` is deterministic across input ordering permutations (sorted lists, dict-key order).
- `freezer.freeze(prediction_ts)` rejects rows with `source_publication_ts > prediction_ts` and admits boundary equality.
- Lag enforcement skips with `insufficient_lag` for resolutions within the floor.
- Polarity resolver prefers `comparison_resolutions.polarity_at_comparison`; falls back to current mapping with the correct flag; raises on unresolvable.
- Brier arithmetic: known `(model_p, observed)` pairs reproduce hand-computed contributions.
- Reliability binning matches `report_generator.engines.section_assemblers.reliability` cell-for-cell on shared input.
- Trace codec round-trips a representative scanner trace through zstd.
- Compare ranking honours `--compare-rank-by` and stable-sorts ties.

### 4.2 Integration Tests

- Full replay against a synthetic 90-day corpus exercises every skip reason at least once.
- Idempotent re-run with the same parameters returns the cached summary without inserting new rows (REQ-CB-RUN-004).
- Definition-version bump invalidates `run_id` and produces a fresh row (REQ-CB-FREEZE-003).
- `RecentWindowError` raised when `until_ts` is within 30 days; `--allow-recent` clears it (REQ-CB-RUN-002).
- Disk-budget guard raises `DiskBudgetError` before exceeding 100 MB.
- CLI renders all pass the framing linter; JSON render carries the disclaimer field.
- Compare command returns zero deltas for `compare RUN_A RUN_A` regardless of rank mode.
- **No circular dependency (REQ-CB-PL-002).** Static AST scan of the upstream subsystem packages (`pattern_library`, `signal_scanner`, `mispricing_detector`, `report_generator`, `position_engine`, `data_ingest`, `polymarket_connector`) asserts zero `import calibration_backtest` and zero `from calibration_backtest` statements anywhere in those packages. The test would fail if an upstream subsystem grew an inadvertent reverse dependency.
- Meta-class realism: `pattern_library.classes.meta.polymarket_resolution_calibration.run()` against a seeded fixture corpus produces `(model_p, observed)` pairs whose count and polarity matches the SQL query in Section 3.16.

### 4.3 Acceptance Test

On reference hardware (EliteBook G8: i7-8665U, 16 GB DDR4, NVMe SSD), against the real `data_ingest` corpus and a refreshed `pattern_library`:

- Five-year replay of ~4000 prediction attempts completes within 5 minutes (REQ-CB-PERF-001) with peak resident memory under 2 GB (REQ-CB-PERF-002).
- Reliability diagrams over the overlapping window are bit-equal to the daily report's reliability diagrams within the 1e-9 tolerance (REQ-CB-SCORE-004).
- Operator-facing outputs (terminal, markdown, html) carry the standard disclaimer block (defined in Section 3.12) and contain no forbidden imperative phrases per the linter against `config/forbidden_phrases.yaml`.
- Fallback-polarity rate is recorded and made visible in the run summary; if it exceeds five percent, the v2 historical-mapping task would be promoted.

## 5. Operational Model

### 5.1 First calibration run

After `pattern_library` refresh and at least one full `mispricing_detector` linkage cycle:

    razor-rooster pattern-library refresh
    razor-rooster calibration-backtest run

The default-runnable invocation (REQ-CB-CLI-001) would replay against the full corpus minus the 30-day tail and the full registered class set.

### 5.2 Investigating a sector that looks miscalibrated

    razor-rooster calibration-backtest show <run_id>
    razor-rooster calibration-backtest show <run_id> --output markdown > /tmp/run.md

The summary would surface per-sector Brier and reliability bins, and a `fallback_polarity_rate` line in the header. If that rate exceeds 5 percent, the render emits a one-line note: "Fallback polarity rate is X% (>5%); operator could consider promoting the v2 historical-mapping task before relying on this calibration evidence." The operator could read the markdown alongside the daily report's reliability section.

### 5.3 Comparing two class-definition cycles

    razor-rooster calibration-backtest run --since 2020-01-01 --until 2024-12-31    # run A
    # operator edits a class definition; library_version bumps
    razor-rooster calibration-backtest run --since 2020-01-01 --until 2024-12-31    # run B
    razor-rooster calibration-backtest compare <run_a> <run_b> --top 10

The compare output would rank cells by absolute Brier delta by default; both metrics would be present so the operator could form their own view of whether the edit improved or degraded calibration.

### 5.4 Pruning

    razor-rooster calibration-backtest list --limit 50
    razor-rooster calibration-backtest prune --before 2024-01-01 --confirm

Prune is operator-initiated and explicit; the append-only contract on completed runs is preserved by deleting whole rows rather than mutating them (REQ-CB-PERSIST-001).

## 6. Performance Notes

- Per-prediction cost is dominated by precursor SQL against `data_ingest`, not posterior arithmetic. The 5-minute budget for 4000 predictions implies roughly 75 ms per prediction including persistence, comfortably above measured `signal_scanner` per-class cost.
- Trace compression at zstd level 3 takes single-digit milliseconds per trace and shrinks payloads by roughly 4-5x for structured JSON.
- The compare join is keyed on `(sector, class_id)` and touches only `backtest_predictions` summary rows; trace decompression is lazy.
- Memory ceiling is held by streaming the resolution iterator; the bounded thread pool keeps in-flight per-prediction state to four at a time.

## 7. Deferred to Implementation

- **DEFER-CB-001** — Empirical trace compression ratio. Measure against a real 4000-prediction run; if zstd level 3 yields under 2x, level 4-5 or brotli could be reconsidered.
- **DEFER-CB-002** — Whether the precursor query audit (OQ-CB-001 follow-up) ships as a static linter or a runtime check. Default: runtime check that flags non-frozen classes during the backtest itself.
- **DEFER-CB-003** — Historical mapping state (v2 promotion). Triggered if the fallback-polarity rate exceeds five percent on real runs.
- **DEFER-CB-004** — `--compare-rank-by threshold` mode for ranking by miscalibration-flag transitions. Parked unless operator feedback would request it.
- **DEFER-CB-005** — Full GUI implementation. The v1 design specifies the surface (Section 3.14); template implementation, JS interactivity, and operator workflow polish ship after CLI parity is proven on real data.
- **DEFER-CB-006** — Streaming or incremental backtest mode. Compressed-trace storage already permits resume semantics in principle.

## 8. References

- Requirements spec: `CALIBRATION_BACKTEST.md` v0.1.0
- `pattern_library` Requirements/Design/Tasks v0.1.0 (REQ-CB-PL-001 target)
- `signal_scanner` Requirements/Design/Tasks v0.1.0 (posterior reused unchanged)
- `mispricing_detector` Requirements/Design/Tasks v0.1.0 (`comparison_resolutions`, `class_market_mappings`, polarity convention)
- `polymarket_connector` Requirements/Design/Tasks v0.1.0 (`polymarket_resolutions` ground truth)
- `data_ingest` Requirements/Design/Tasks v0.1.0 (source-publication-timestamp pattern)
- `report_generator` Requirements/Design/Tasks v0.1.0 (reliability binning, framing linter)
- `position_engine` Requirements/Design/Tasks v0.1.0 (`frame.linter.check_text`)
- LOOM v0.53.0
- Open threads: OT-003 (HIGH, paper-trading validation; advances to `IN_DESIGN`), OT-004 (v1 remains paper-analysis only), OT-005 (reference hardware performance validation), OT-006 (forward-going calibration scaffolding)
