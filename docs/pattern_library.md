# Razor-Rooster v1 Pattern Library

Operator-facing reference for the historical event-pattern catalogue. The pattern_library subsystem turns `data_ingest` corpora into per-class base rates, precursor signatures, analogue feature spaces, and calibration outputs. Downstream subsystems (signal_scanner, mispricing_detector, position_engine, monitor, report_generator) consume the library through the public `library` facade rather than touching `pl_*` tables directly.

For the requirements, design, and task tracking, see `specs/PATTERN_LIBRARY.md`, `specs/PATTERN_LIBRARY_DESIGN.md`, and `specs/PATTERN_LIBRARY_TASKS.md`.

## Design principles

- **Per-class isolation.** A bad class definition cannot corrupt the library; a failing refresh on one class does not poison outputs for others. Every class's predicate and precursor functions run inside a `try`/`except` boundary owned by the refresh runner.
- **Versioned outputs.** Every persisted row carries `library_version` and `definition_version`. Downstream consumers compare returned versions against `library.current_version()` to detect a stale cache. Library version auto-bumps on registry changes; class `definition_version` is the operator's responsibility.
- **Operator-extensible registry.** Adding a class is a new `.py` module under `src/razor_rooster/pattern_library/classes/` — no framework changes required. Auto-discovery via `pkgutil` finds and imports the module on first registry access.
- **Read-from-storage public API.** The `library` facade reads from persisted `pl_*` tables; it never recomputes on demand. Refresh is the only path that writes.
- **Conservative defaults.** Jeffreys prior on base rates with a per-class override permitted; Youden-J threshold discovery with F1 / quantile-95 / manual fallbacks; weighted-Euclidean analogue distance with Mahalanobis override; 10-bin reliability diagrams; sample-size guard at n=10 for calibration; n=5 for low-sample warning. See `specs/PATTERN_LIBRARY.md` for the full normative list.

## The eight v1 seed classes

The v1 seed library covers all six Razor sectors plus two cross-cutting scenarios — multi-precursor combination logic and a Polymarket-resolution calibration meta-class scaffold for OT-006. Each class lives at `src/razor_rooster/pattern_library/classes/<class_id>.py`.

| Class id | Sector | Predicate | Precursors | Notes |
| - | - | - | - | - |
| `pheic_declaration_12mo` | PUBLIC_HEALTH | WHO DON entries flagged with PHEIC text (5–6 historical declarations) | `who_don_publication_frequency` | Tests the rare-event base-rate path. Wide credible interval; `low_sample_warning` fires by design. |
| `gdelt_conflict_intensification` | GEOPOLITICAL | Country-day with GDELT conflict-coded event count ≥ 50 | `gdelt_event_density` | Inverse stress case to PHEIC: dense, abundant data. Per-country threshold tuning deferred (DEFER-PL-001). |
| `final_rule_within_12mo` | REGULATORY | Federal Register paired (proposed_rule, rule) on the same docket within 12 months | `federal_register_proposed_rule_count` | Tests the document_docket schema joined predicates. |
| `opec_unscheduled_cut` | COMMODITY | Heuristic 5-day Brent crude jump ≥ 10% (FRED `DCOILBRENTEU`) | `brent_price_level` | Scaffold occurrence list; production tuning awaits a curated OPEC announcements table in v1.1. |
| `enso_neutral_to_elnino` | CLIMATE | Quarter where rolling 3-month ENSO 3.4 anomaly crosses +0.5°C from below | `enso_anomaly_value` (manual threshold 0.3) | Tests time-series threshold predicates against NOAA-derived data. |
| `eia_grid_reliability_event` | INFRASTRUCTURE_ENERGY | EIA series tagged `grid_disturbance` or `reliability_event` with positive value | `grid_load_demand` | v1 scaffold returning empty until EIA connector adds the relevant series. |
| `multi_signal_geopolitical_alert` | GEOPOLITICAL | Country-week with ACLED density ≥ 30 | `acled_event_density`, `gdelt_tone_shift`, `federal_register_diplomatic_filings` | Three precursors → exercises multi-variable combination logic (REQ-PL-SIG-004) and the co-occurrence lookup. |
| `polymarket_resolution_calibration` | CROSS_CUTTING | Empty by design until downstream subsystems log predictions | (none) | Linchpin meta-class for OT-006 (full calibration backtest infrastructure). |

### Sector coverage

The seed library is one class per sector plus the multi-signal and meta-class extensions:

- **PUBLIC_HEALTH** — `pheic_declaration_12mo`
- **GEOPOLITICAL** — `gdelt_conflict_intensification`, `multi_signal_geopolitical_alert`
- **REGULATORY** — `final_rule_within_12mo`
- **COMMODITY** — `opec_unscheduled_cut`
- **CLIMATE** — `enso_neutral_to_elnino`
- **INFRASTRUCTURE_ENERGY** — `eia_grid_reliability_event`
- **CROSS_CUTTING** — `polymarket_resolution_calibration`

## Per-class documentation convention

Each seed class has (or will have) a companion documentation file under `specs/seed_event_classes/<class_id>.md`. The convention for the per-class file:

1. **Title and one-line summary** — what this class detects.
2. **Sector** — the primary domain sector and any secondary tags.
3. **Sources** — which `data_ingest` connectors feed the predicate, with their freshness contracts.
4. **Predicate** — the SQL or Python that identifies an occurrence, with the rationale for the chosen threshold.
5. **Precursors** — each `PrecursorVariable` with its lead-time window, direction, and threshold method.
6. **Analogue features** — the feature vector dimensions, normalization choices, and any per-class distance-metric overrides.
7. **Calibration sample size** — the `baseline_sample_size` and the rationale.
8. **Known limitations** — what v1 doesn't capture and why; what would be needed to refine the predicate.
9. **References** — design sections, related open threads, and any per-source notes.

When a class author later adds an entry, the documentation file becomes the audit trail for definition changes. Combined with `definition_version` bumps and `pl_library_versions.bump_reason` history, the operator can answer "why is this class's hit rate different from last quarter?" without reading code.

## Adding a new class — worked example

Suppose the operator wants a class detecting weeks where the World Bank's "current account balance, % of GDP" indicator drops by more than 2 percentage points quarter-over-quarter for a given country.

1. **Sketch the predicate.** The relevant `data_ingest` series sits in the `time_series` table with `source_id = 'worldbank'` and a series id matching the indicator code. The predicate compares each quarter's value against the prior quarter for the same country.

2. **Pick the precursors.** Reasonable candidates: prior-quarter value change, FX reserve adequacy (if available), inflation, exchange-rate volatility. Each precursor is a `pd.Series` indexed by daily timestamps within the lead window.

3. **Write the class module** at `src/razor_rooster/pattern_library/classes/current_account_collapse.py`:

   ```python
   from datetime import timedelta
   from razor_rooster.pattern_library.models.event_class import (
       EventClass, PrecursorVariable, Sector,
   )

   def _occurrences(conn):
       # SQL returning a DataFrame with at least 'occurrence_ts'.
       ...

   def _prior_quarter_balance(conn, window_start, window_end):
       # Daily series of the relevant World Bank indicator.
       ...

   CLASS = EventClass(
       class_id="current_account_collapse_qoq",
       title="Current account balance drop > 2pp quarter-over-quarter",
       description="Country-quarters where current account % of GDP "
                   "drops by more than 2 percentage points QoQ.",
       domain_sector=Sector.MACROECONOMIC,
       occurrence_query=_occurrences,
       precursors=(
           PrecursorVariable(
               variable_id="prior_quarter_balance",
               title="World Bank current account % of GDP",
               query=_prior_quarter_balance,
               direction="low_signals_event",
               lead_time_window=timedelta(days=180),
           ),
       ),
       base_rate_window_default=timedelta(days=365 * 30),
       refractory_months=3,
       baseline_sample_size=300,
   )
   ```

4. **Validate, refresh, and inspect.**

   ```bash
   razor-rooster pattern-library validate current_account_collapse_qoq
   razor-rooster pattern-library refresh --class current_account_collapse_qoq
   razor-rooster pattern-library show current_account_collapse_qoq
   ```

5. **Document the class** at `specs/seed_event_classes/current_account_collapse_qoq.md` per the convention above.

The library version auto-bumps the next time `refresh` runs (registry diff shows a class added). Downstream subsystems that already cached the prior version detect the mismatch on next read and invalidate their cache.

## Storage layout

The eight `pl_*` tables (defined by migrations 2001+) live alongside `data_ingest`'s canonical tables and `polymarket_*` tables in the same DuckDB store at `data/trough.duckdb`. The pattern_library schema migrations are namespaced in versions 2001+ to avoid collisions with `data_ingest` (1–999) and `polymarket_connector` (1001–1999). Schema migration version numbering is shared across all three subsystems via a single `schema_migrations` table.

Calibration prediction-trace JSON files are written to `data/library/calibration/<class_id>.json`. The trace path is recorded in the `pl_calibration` row so consumers can re-open the per-event prediction history.

## Disk and performance

- v1 disk budget: **1 GB** out of the 100 GB global cap. Most of the budget is the calibration trace files; the `pl_*` tables themselves are small (≤100 MB across all eight seed classes against a five-year corpus).
- Full-refresh duration target: **under 15 minutes** on EliteBook G8 hardware (NFR-PL-PERF-001) for the eight seed classes against a populated `data_ingest` corpus.
- Per-class refresh: **under 5 minutes** for typical classes; the rare-event classes (e.g. `pheic_declaration_12mo`) finish in seconds because their occurrence count is small.
- Analogue lookup: **under 2 seconds** on a 10,000-point feature space (NFR-PL-PERF-003).

## When refresh produces empty outputs

Several seed classes are scaffolds: they produce empty occurrence lists until the operator's data corpus catches up. This is correct behavior; the refresh pipeline still runs end-to-end against zero occurrences:

- Base rate: occurrences=0, posterior credible interval reflects the Jeffreys prior; `low_sample_warning` fires.
- Signatures: skipped (no events to compare against baselines).
- Analogue features: skipped (no feature-space rows).
- Calibration: returns the `insufficient_data` sentinel; trace file still written for path stability.

Classes in this state at v1 release:

- `eia_grid_reliability_event` — waits for the EIA connector to add `grid_disturbance` series.
- `polymarket_resolution_calibration` — waits for downstream subsystems to start logging predictions.
- `opec_unscheduled_cut` — only finds occurrences where the FRED Brent series shows a 5-day jump ≥ 10% in the operator's backfilled window.

When the upstream data lands and the next refresh runs, the empty outputs are replaced with real ones automatically.

## Updating after T-PL-081

After the operator runs the first refresh against the real `data_ingest` backfill (T-PL-081), update the design's deferred-implementation items with measured numbers:

- **DEFER-PL-001** — empirical base rates per seed class (currently placeholder).
- **DEFER-PL-002** — optimal `baseline_sample_size` per class (v1 uses 1,000 default).
- **DEFER-PL-003** — whether the geometric-mean co-occurrence correction is materially better than a simpler product (test on the `multi_signal_geopolitical_alert` class).
- **DEFER-PL-004** — reliability-diagram bin count (v1 default is 10; drop to 5 for sparse classes if needed).

The findings inform v1.1 priorities. Classes that consistently flag low-confidence may need redefinition; classes with >2-second analogue lookups may need feature-space reduction. The operator records measurements in `specs/PATTERN_LIBRARY_TASKS.md` under the T-PL-081 deliverable.

## See also

- `razorrooster.md` — LOOM (project state of truth)
- `specs/PATTERN_LIBRARY.md`, `specs/PATTERN_LIBRARY_DESIGN.md`, `specs/PATTERN_LIBRARY_TASKS.md` — full requirements / design / tasks
- `docs/sources.md` — per-source reference (data_ingest connectors used by the seed classes)
- `src/razor_rooster/pattern_library/classes/` — the seed-class modules
