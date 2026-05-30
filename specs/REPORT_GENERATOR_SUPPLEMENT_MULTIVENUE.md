# REPORT_GENERATOR — Multi-Venue Calibration Supplement

**Subsystem:** `report_generator`
**Codename:** The Crow
**Spec version:** 0.14.0 (Supplement; v0.51.0 follow-on rendering/ergonomic enhancements landed)
**Status:** SHIPPED (LOOM v0.51.0)
**Threat context:** MINIMAL_EXPOSURE (unchanged from base spec)
**Last updated:** 2026-05-16

---

## 0. Status of this document

This is a supplement to `specs/REPORT_GENERATOR.md` (Requirements
v0.1.0) and `specs/REPORT_GENERATOR_DESIGN.md` (Design v0.1.0). It
describes the multi-venue calibration features that landed in LOOM
v0.38.0. The base spec triple is unchanged.

This document records:

1. The compatible subset of the multi-venue calibration directive
   that the operator authorized for implementation.
2. The four passes that landed.
3. The deliberate rejections — items the operator's broader
   directive proposed that were not implemented because they
   conflict with the v0.2.0 educational framing or the LOOM's
   recommendation-only stance (OT-004 v1 resolution).
4. New requirements (REQ-RG-COMPAT-*) and acceptance criteria.
5. Open questions and forward work.

This supplement does not unwind any v1 framing. It extends the
report renderer with descriptive, multi-venue-aware sections that
preserve the educational framing.

### Changes in spec v0.2.0 (LOOM v0.39.0)

- DEFER-RG-COMPAT-001 resolved: the four threshold constants are
  now operator-tunable in `config/report.yaml` under the
  `thresholds:` block.
- DEFER-RG-COMPAT-002 resolved: per-sector overrides added for
  every threshold knob via the `<knob>_per_sector` keys.
- DEFER-RG-COMPAT-003 resolved: new opt-in `reliability` section
  with per-sector calibration bins.
- Two new threshold knobs (`reliability_bin_count`,
  `reliability_min_resolutions_per_bin`) live alongside the
  multi-venue knobs in the same block.

### Changes in spec v0.3.0 (LOOM v0.40.0)

- T-RG-COMPAT-MEAS-001 added: per-cycle threshold-distribution
  measurement helper. New table `report_threshold_measurements`
  (m7002 migration) records the cross-venue spread distribution
  at each cycle. New CLI subcommand
  `razor-rooster report measurements` exposes the historical
  view. Operators use this to decide whether to re-tune the
  multi-venue thresholds for their corpus.
- Per-sector reliability overrides added:
  `reliability_bin_count_per_sector` and
  `reliability_min_resolutions_per_bin_per_sector`. Sectors with
  many resolutions per window can use finer bins; sectors with
  few can keep them broader.
- T-RG-COMPAT-CHART-001 added: ASCII calibration-curve overlay
  inside the reliability section (terminal + markdown). Helps
  operators read calibration drift visually.

### Changes in spec v0.4.0 (LOOM v0.41.0)

- Two additional measurement kinds shipped under
  T-RG-COMPAT-MEAS-001's existing infrastructure:
  `single_venue_dominance_share` (max venue's share of combined
  24h volume per multi-venue class, sourced from the surfaced
  section) and `brier_per_sector` (per-sector rolling Brier
  score, sourced from the calibration section). The generator
  records all three shipped kinds on every cycle.
- T-RG-COMPAT-EXPL-001 added: new
  `razor-rooster report explain-thresholds` CLI subcommand
  that reports where each configured threshold sits in the
  most recent measurement's percentile distribution.
  Strictly descriptive — no imperative recommendations.
- T-RG-COMPAT-SUGG-001 added: new threshold-suggestion engine
  in `engines/suggestions.py` plus a
  `razor-rooster report suggest-thresholds` CLI subcommand.
  Reads the most recent `lookback_cycles` measurements,
  averages each percentile cut across cycles, and emits one
  suggested value per target percentile. Default targets are
  0.50, 0.70, 0.90; custom targets repeatable. Strictly
  descriptive.

### Changes in spec v0.5.0 (LOOM v0.42.0)

- T-RG-COMPAT-SUGG-002 added: opt-in **write path** for the
  suggest-thresholds engine. New `apply_threshold_suggestion`
  helper writes a suggested value back to
  `config/report.yaml` after operator confirmation, with a
  timestamped backup. New CLI flags `--apply`, `--yes`,
  `--config` on `suggest-thresholds`. Refuses postures that
  would silence guard rails (e.g. `target_pct >= 1.0` for
  the dominance threshold).
- T-RG-COMPAT-PRUNE-001 added: retention/pruning helper plus
  new `razor-rooster report prune-measurements` CLI
  subcommand. Two strategies (absolute cutoff via
  `--before` and per-kind newest-N retention via
  `--keep-last`) that stack. Confirm-required.
- T-RG-COMPAT-SUGG-003 added: stability metric on the
  suggestion engine. Computes a per-kind coefficient of
  variation across percentile cuts and flags
  `unstable=True` when the CV exceeds 0.5 (default). The
  `--apply` confirmation prompt prepends a short
  descriptive note when the underlying distribution is
  unstable so operators don't tune to noise.

### Changes in spec v0.6.0 (LOOM v0.43.0)

- T-RG-COMPAT-AUTOPRUNE-001 added: auto-prune of
  `report_threshold_measurements` after every successful
  cycle. New `auto_prune:` block in `config/report.yaml`
  with `enabled` (default False — opt-in),
  `older_than_days`, and `keep_last` knobs. Strategies
  stack. Generator wraps the prune in best-effort isolation
  so a prune failure never breaks report generation.
- T-RG-COMPAT-DIFF-001 added: new `--diff` flag on
  `suggest-thresholds --apply`. Prints a unified-diff-style
  preview of the YAML change before the confirmation
  prompt. New `compute_apply_diff` helper is a pure
  function that does not touch the live config.
- T-RG-COMPAT-TUNINGLOG-001 added: new
  `threshold_tuning_log` table (m7003 migration) records
  every successful `--apply` write. New persistence
  helpers `persist_tuning_log_entry` and
  `list_tuning_log_entries`. New `--note TEXT` flag on
  `suggest-thresholds` attaches operator commentary. New
  `razor-rooster report tuning-log` CLI subcommand lists
  the history. Failures of the log write are best-effort
  (don't undo the apply).

### Changes in spec v0.7.0 (LOOM v0.44.0)

- T-RG-COMPAT-UNDO-001 added: new
  `razor-rooster report tuning-log-undo <log_id>` CLI
  subcommand restores `config/report.yaml` from a
  tuning-log entry's recorded backup. Saves a fresh
  pre-undo backup so the undo is itself reversible.
  Records the undo as a new tuning-log entry with a `note`
  referencing the original `log_id`. Backup-file
  timestamps now use microsecond resolution so back-to-
  back applies/undos don't reuse filenames.
- T-RG-COMPAT-RECENT-001 added: new opt-in
  `recent_tuning` report section. Sits between
  `system_health` and `surfaced` when enabled. Reads
  recent `threshold_tuning_log` entries and emits a short
  per-entry summary so operators see threshold changes
  alongside the data the thresholds shape.
- T-RG-COMPAT-HTML-001 added: new self-contained HTML
  renderer in `renderer/html.py`. Inline CSS only, no
  external assets, no JavaScript. Supports
  `prefers-color-scheme`. New `--html PATH` flag on
  `report generate`. New schema columns
  `rendered_html_text` and `html_path` on `report_log`
  (m7004 migration; idempotent on fresh installs via
  `PRAGMA table_info`).

### Changes in spec v0.8.0 (LOOM v0.45.0)

- T-RG-COMPAT-COMPARE-001 added: new
  `razor-rooster report compare <a> <b>` CLI subcommand
  diffs two persisted reports by ID. Pure read; emits
  metadata, sections-added/removed, library/disclaimer
  drift, terminal-text length delta, and an optional
  unified-diff preview of the rendered terminal text.
- T-RG-COMPAT-WATCH-001 added: new
  `razor-rooster report watch` CLI subcommand runs
  `report generate` on a fixed cadence in a loop. Pure
  ergonomics; no new analytical surface. The same engine
  runs once per `--interval` (range [60, 86400] seconds).
  `--once` and `--max-cycles` for tests and single-shot
  use.
- T-RG-COMPAT-GLANCE-001 added: new opt-in `at_a_glance`
  body section at the top of the report. Lifts the top
  item from each major section's already-ordered list
  and emits structured key/value facts. The assembler
  does NOT independently rank or score — it pulls the
  first element out of each section's existing ordered
  list. Strict no-prose, no-synthesis framing. Section
  title is "AT A GLANCE", not "Executive Summary". Nine
  new editorial-flavor phrases added to
  `config/forbidden_phrases.yaml` to defend against
  editorial drift entering via this section.

---

## 1. Authorized scope

The operator approved the following items from the multi-venue
calibration directive:

- **§1 Cross-venue disagreement section** — describe disagreement
  between Polymarket and Kalshi prices on the same event class.
- **§3 Per-sector Brier score** — surface model calibration by
  domain sector over a rolling window.
- **§5 Single-venue dominance warning** — flag surfaced comparisons
  where one venue holds an outsized share of combined liquidity.

A fourth pass — liquidity-weighted consensus, derived from §1 — was
added during implementation as a natural extension of the cross-venue
section.

### Explicitly rejected (carried forward as standing rejections)

The following directive items were **not** implemented and remain
rejected. Future supplement requests must not silently re-introduce
them:

- **Webhook output / `recommended_action: ENTER|EXIT` action tier.**
  Would emit imperative directives in operator-facing output.
  Conflicts with REQ-RG-FRAME-001 (imperative-language linter)
  and the v0.2.0 educational framing.
- **Autonomous A↔B loop.** A self-feeding loop where
  `mispricing_detector` outputs become inputs without operator
  review. Conflicts with OT-004 v1 resolution
  (recommendation-only).
- **No-human-in-loop mode.** Operator review remains the only
  path from analysis to action. Conflicts with the standing
  user instruction "Educational framing only".
- **§6 conflict-resolution rule.** A directive that would have
  elevated the v0.1 system prompt over the LOOM. The LOOM
  remains the project's source of truth.
- **New connectors (SX Bet, Drift, Metaculus).** Cross-venue work
  in v1.x is limited to Polymarket + Kalshi. Adding venues is a
  separate spec triple.

---

## 2. Passes shipped in v0.38.0

### 2.1 Pass 1 — Cross-venue disagreement section

A new section between `surfaced` and `watched` in the fixed body
order. Reads recently-computed `comparisons` rows and surfaces
classes whose market-implied probabilities diverge by at least
`spread_threshold_bps` basis points across the venues mapped to
the class.

**Module:**
`src/razor_rooster/report_generator/engines/section_assemblers/cross_venue.py`

**Behavior:**

- Scans the cycle window's comparisons.
- Dedups to the most-recent comparison per `(class_id, venue)`
  pair so a class with multiple comparisons in the window doesn't
  trigger spurious self-disagreement.
- Drops classes mapped to fewer than two venues.
- Drops classes whose spread is below `spread_threshold_bps`.
- Looks up `pl_event_classes.title` and `domain_sector` for each
  class.
- Emits one item per qualifying class with:
  - `class_id`, `class_title`, `domain_sector`.
  - `venue_prices` — per-venue list, sorted alphabetically by
    venue name for deterministic output.
  - `spread_bps` — `round(abs(max - min) * 10000)`.
  - `max_market_p`, `min_market_p`.
  - `consensus_market_p`, `total_volume_24h` (Pass 4 — see §2.4).
- Items ordered by `spread_bps` descending.

**Defaults:**

- `DEFAULT_SPREAD_THRESHOLD_BPS = 500` (5 percentage points).
- Threshold is currently a module constant; v1.2 plan moves it
  to `config/report.yaml`.

**Renderers:**

- Terminal: per-class block with the consensus line, per-venue
  rows, and the model anchor.
- Markdown: summary table (Class | Sector | Spread (bps) |
  Consensus | Venue prices) plus per-class detail blocks
  underneath.

**Empty state:** `_No cross-venue disagreements this cycle._`
when no class meets the threshold.

**Tests:** `tests/report_generator/test_cross_venue.py` (14
tests).

### 2.2 Pass 2 — Single-venue dominance warning

An extension of the existing surfaced-comparisons assembler. When
a class is mapped on more than one venue but one venue holds
strictly greater than 80% of the combined 24h volume across them,
every surfaced comparison for that class gets a
`single_venue_dominance` warning appended to its warnings list.

**Module:**
`src/razor_rooster/report_generator/engines/section_assemblers/surfaced.py`

**New helpers:**

- `_compute_venue_volume_shares(conn, *, since_ts, until_ts) -> dict[str, dict[str, float]]`
- `_has_single_venue_dominance(*, class_id, shares, threshold_pct) -> bool`

**Behavior:**

- One window-bounded SQL query computes each class's per-venue
  share of 24h volume, deduped to the most-recent comparison per
  (class, venue) using the same dedup pattern as cross_venue.
- Classes mapped on a single venue cannot be "dominant" by
  definition — the check returns False without further work.
- Classes with all-NULL volume across all venues fall through to
  not-dominant; we don't manufacture a warning when there's no
  data to compare.
- Threshold is strict `>`: a 50/50 split is not dominance even
  at the boundary. Default 80% (0.80).

**Defaults:**

- Threshold = `0.80` as the keyword arg
  `single_venue_dominance_pct` on `surfaced.assemble`.
- v1.2 plan: move to `config/report.yaml`.

**Renderers:** No new code path. The existing warnings-block
rendering picks up the new warning string.

**Tests:** `tests/report_generator/test_single_venue_dominance.py`
(10 tests).

### 2.3 Pass 3 — Per-sector Brier score

An extension of the calibration-log assembler. Aggregates
resolutions over a rolling window by `pl_event_classes.domain_sector`
and computes a Brier score per sector.

**Module:**
`src/razor_rooster/report_generator/engines/section_assemblers/calibration.py`

**New helper:**

- `_compute_sector_brier_scores(conn, *, until_ts, window_days, miscalibration_threshold) -> list[dict[str, Any]]`

**Behavior:**

- Window: `until_ts - timedelta(days=window_days)`.
- Selects every `comparison_resolutions` row in the window joined
  to `comparisons` and `pl_event_classes`. Excludes
  `resolution_outcome = 'invalid'` rows since they have no
  defined outcome to score against.
- Per-resolution squared error:
  `(model_probability_at_comparison - outcome_observed) ** 2`.
- Per-sector mean of squared errors, rounded to four decimals.
- `miscalibrated = brier_score > miscalibration_threshold`.
- Sectors sorted alphabetically. Sectors with zero scoreable
  resolutions in the window are omitted.

**Defaults:**

- `DEFAULT_BRIER_WINDOW_DAYS = 90`.
- `DEFAULT_MISCALIBRATION_THRESHOLD = 0.25` — the random-guesser
  Brier at p=0.5.

**Render-when-empty rule:** The calibration section now renders
even when the report's `since`-`until` window contains no fresh
resolutions, as long as the Brier table has data from the rolling
window. This keeps the operator's view of long-running
calibration visible on quiet days.

**Renderers:**

- Terminal: per-sector lines listing sector, Brier score, n,
  window, and miscalibration status.
- Markdown: a per-sector table (Sector | Brier | n | Window |
  Status) below the per-resolution table. Status column reads
  `**miscalibrated** (weight outputs less)` for flagged sectors,
  `ok` otherwise.

**Tests:** `tests/report_generator/test_sector_brier.py`
(13 tests).

### 2.4 Pass 4 — Liquidity-weighted consensus

An extension of cross_venue.py. Each cross-venue item now reports
a consensus market probability that weights each venue's price by
its 24h volume.

**Module:**
`src/razor_rooster/report_generator/engines/section_assemblers/cross_venue.py`

**New helper:**

- `_liquidity_weighted_consensus(venue_entries) -> tuple[float | None, float | None]`

**Behavior:**

- Per-venue weight: `float(volume)` if `volume is not None and
  volume > 0`, else `0.0`.
- Weighted mean: `sum(market_p * weight) / sum(weight)` when
  total weight > 0.
- Fallback when total weight is zero: unweighted mean across
  venues with a non-NULL `market_probability`.
- Returns `(consensus_market_p, total_volume_24h)`.
- `total_volume_24h` is `None` on the unweighted-fallback path
  so the renderer can label the consensus accordingly.

**Renderers:**

- Markdown: a `Consensus` column in the cross-venue summary
  table; per-class detail blocks call out whether the consensus
  is liquidity-weighted (with the total volume) or
  unweighted-fallback.
- Terminal: a `consensus: <pct>%` line in each cross-venue item.

**Tests:** `tests/report_generator/test_cross_venue_consensus.py`
(9 tests).

---

## 3. New Requirements

These extend the requirements in `specs/REPORT_GENERATOR.md`.

### 3.1 Cross-venue disagreement (Pass 1)

**REQ-RG-COMPAT-CV-001: Cross-venue section presence**
The generator **shall** emit a `cross_venue` section between
`surfaced` and `watched` whenever at least one event class has
comparisons on more than one venue in the cycle window with
market-implied probabilities differing by at least
`spread_threshold_bps` basis points.
*Verification:* `tests/report_generator/test_cross_venue.py`.

**REQ-RG-COMPAT-CV-002: Spread threshold default**
The default `spread_threshold_bps` **shall** be 500 (5
percentage points). The constant lives at
`cross_venue.DEFAULT_SPREAD_THRESHOLD_BPS`.
*Verification:* unit test asserts the constant value.

**REQ-RG-COMPAT-CV-003: Most-recent-per-pair deduplication**
For each `(class_id, venue)` pair within the cycle window, the
assembler **shall** keep only the most recent comparison.
*Verification:* test feeds two comparisons per (class, venue)
pair and asserts only the newer one informs the spread.

**REQ-RG-COMPAT-CV-004: Spread-descending ordering**
Items in the `cross_venue.items` list **shall** be ordered by
`spread_bps` descending.
*Verification:* test asserts ordering.

**REQ-RG-COMPAT-CV-005: Per-venue alphabetical ordering**
Within each item, `venue_prices` **shall** be sorted
alphabetically by venue name for deterministic rendering.
*Verification:* test asserts ordering.

### 3.2 Single-venue dominance (Pass 2)

**REQ-RG-COMPAT-SV-001: Dominance warning emission**
When a surfaced comparison's class is mapped on more than one
venue and one venue holds strictly greater than
`single_venue_dominance_pct` of the combined 24h volume across
the venues, the comparison's warnings list **shall** include
`single_venue_dominance`.
*Verification:* `tests/report_generator/test_single_venue_dominance.py`.

**REQ-RG-COMPAT-SV-002: Dominance threshold default**
The default `single_venue_dominance_pct` **shall** be 0.80.
Threshold comparison is strict `>`.
*Verification:* test feeds a 80/20 boundary case (must trip)
and a 50/50 split (must not trip).

**REQ-RG-COMPAT-SV-003: No false-warn on single-venue classes**
Classes mapped on only one venue **shall not** trigger the
warning.
*Verification:* test asserts.

**REQ-RG-COMPAT-SV-004: NULL-volume robustness**
When no venue has a recorded 24h volume for the class, the
warning **shall not** be emitted.
*Verification:* test feeds NULL volumes and asserts no warning.

### 3.3 Per-sector Brier score (Pass 3)

**REQ-RG-COMPAT-BRIER-001: Per-sector Brier emission**
The calibration section's content dict **shall** include a
`sector_brier_scores` list with one entry per `domain_sector`
that has at least one scoreable resolution in the rolling
window.
*Verification:* `tests/report_generator/test_sector_brier.py`.

**REQ-RG-COMPAT-BRIER-002: Brier window default**
The default rolling window **shall** be 90 days (constant
`DEFAULT_BRIER_WINDOW_DAYS`).
*Verification:* unit test asserts the constant value.

**REQ-RG-COMPAT-BRIER-003: Miscalibration threshold default**
The default miscalibration threshold **shall** be 0.25
(constant `DEFAULT_MISCALIBRATION_THRESHOLD` — the random-guesser
Brier at p=0.5). Sectors above this **shall** be flagged
`miscalibrated = True`.
*Verification:* unit test asserts the constant and the
miscalibration boundary.

**REQ-RG-COMPAT-BRIER-004: Invalid resolutions excluded**
Resolutions with `resolution_outcome = 'invalid'` **shall not**
contribute to per-sector Brier scores.
*Verification:* test mixes an invalid resolution into a sector
and asserts it doesn't move the Brier.

**REQ-RG-COMPAT-BRIER-005: Alphabetical sector ordering**
Per-sector entries in `sector_brier_scores` **shall** be sorted
alphabetically by sector name.
*Verification:* test asserts ordering.

**REQ-RG-COMPAT-BRIER-006: Render-when-empty rule**
When `comparison_resolutions` is empty for the report window
but the rolling Brier window has data, the calibration section
**shall** still render with the per-sector table.
*Verification:* test asserts the section renders the per-sector
table even with no resolutions in the report window.

### 3.4 Liquidity-weighted consensus (Pass 4)

**REQ-RG-COMPAT-CONS-001: Consensus computation**
Each cross-venue item **shall** include `consensus_market_p`
computed as the volume-weighted mean of venue market
probabilities, with weights equal to per-venue 24h volumes.
*Verification:* `tests/report_generator/test_cross_venue_consensus.py`.

**REQ-RG-COMPAT-CONS-002: Unweighted-fallback path**
When all venues' 24h volumes are NULL or zero, the consensus
**shall** be computed as the unweighted mean across venues with
a non-NULL `market_probability`. `total_volume_24h` **shall**
be `None` on the fallback path.
*Verification:* test asserts both branches.

**REQ-RG-COMPAT-CONS-003: Markdown Consensus column**
The cross-venue markdown summary table **shall** include a
`Consensus` column populated from `consensus_market_p`.
*Verification:* renderer test asserts column presence.

### 3.5 Reliability diagram (v0.39.0)

**REQ-RG-COMPAT-REL-001: Reliability section emission**
The generator **shall** emit a `reliability` section between
`calibration` and `watchlist` when the section is enabled in
`config/report.yaml`. The section **shall** be opt-in: the
default workspace `enabled_sections` list omits it.
*Verification:* `tests/report_generator/test_reliability.py`.

**REQ-RG-COMPAT-REL-002: Equal-width bin construction**
The assembler **shall** build `bin_count` equal-width bins
covering [0.0, 1.0]. The top bin is fully closed (`[lo, hi]`)
so probabilities of exactly 1.0 fall into it; all other bins
are half-open (`[lo, hi)`).
*Verification:* test asserts bin endpoints and the 1.0 boundary.

**REQ-RG-COMPAT-REL-003: Per-bin aggregation**
For each bin with at least one observation, the assembler
**shall** compute: `n` (count), `mean_predicted` (mean of model
probabilities), `empirical_rate` (mean of observed binary
outcomes), and `calibration_gap` (`empirical_rate -
mean_predicted`).
*Verification:* test seeds known observations and asserts
exact values per bin.

**REQ-RG-COMPAT-REL-004: Sparse-bin flag**
Bins with at least one observation but fewer than
`min_resolutions_per_bin` observations **shall** be flagged
`sparse=True`. Default `min_resolutions_per_bin` = 5.
*Verification:* test asserts the flag at the boundary.

**REQ-RG-COMPAT-REL-005: Empty-bin sentinel**
Bins with zero observations **shall** be emitted with `n=0`
and `mean_predicted`, `empirical_rate`, `calibration_gap`
all `None`, plus `sparse=True`.
*Verification:* test asserts the empty-bin shape.

**REQ-RG-COMPAT-REL-006: Invalid resolutions excluded**
Resolutions with `resolution_outcome = 'invalid'` **shall not**
contribute to any bin.
*Verification:* test mixes an invalid resolution into the
window and asserts it doesn't change any bin's `n`.

**REQ-RG-COMPAT-REL-007: Per-sector window narrowing**
The reliability assembler **shall** honor the per-sector
window overrides from
`thresholds.brier_window_days_per_sector` so each sector's
bins reflect its own rolling window.
*Verification:* test seeds a sector with a 30-day override
and asserts older resolutions are excluded.

**REQ-RG-COMPAT-REL-008: Defaults**
The default `reliability_bin_count` **shall** be 10
(`DEFAULT_BIN_COUNT`). The default
`reliability_min_resolutions_per_bin` **shall** be 5
(`DEFAULT_MIN_RESOLUTIONS_PER_BIN`).
*Verification:* unit tests assert the constants.

### 3.6 Threshold-distribution measurements (v0.40.0)

**REQ-RG-COMPAT-MEAS-001: Per-cycle distribution recording**
On every report-generation cycle, the generator **shall**
record a `report_threshold_measurements` row per recorded
measurement kind. v0.40.0 records one kind:
`cross_venue_spread_bps`. The row **shall** carry: report_id,
measurement_kind, measured_at, n_observations,
n_above_threshold, configured_threshold, and a JSON
distribution payload.
*Verification:* `tests/report_generator/test_threshold_measurements.py`.

**REQ-RG-COMPAT-MEAS-002: Distribution payload shape**
The persisted distribution payload **shall** include `n`,
`n_above_threshold`, `configured_threshold`, `min`, `max`,
`mean`, `stddev`, and a `percentiles` mapping with keys
matching `DEFAULT_PERCENTILES` (default
`(0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)`). Empty input
**shall** produce all-None numeric fields with `n=0`.
*Verification:* unit tests assert each field on empty and
non-empty inputs.

**REQ-RG-COMPAT-MEAS-003: Strict greater-than threshold count**
A value exactly at the configured threshold **shall not**
contribute to `n_above_threshold` (strict `>` semantics
matching the cross-venue spread filter).
*Verification:* unit test asserts the boundary case.

**REQ-RG-COMPAT-MEAS-004: Best-effort isolation**
Measurement-side failures **shall not** break report
generation. The generator wraps measurement persistence in a
try/except, logs the exception, and proceeds.
*Verification:* generator integration test does not regress
on a malformed cross_venue content dict.

**REQ-RG-COMPAT-MEAS-005: CLI surface**
The CLI **shall** expose
`razor-rooster report measurements [--kind ...] [--since ...]
[--limit N] [--json]` for inspecting recorded measurements.
*Verification:* CLI smoke tests assert plain and JSON output
shapes.

### 3.7 Per-sector reliability overrides (v0.40.0)

**REQ-RG-COMPAT-REL-009: Per-sector bin count**
The reliability assembler **shall** accept
`bin_count_per_sector` and apply each sector's override
independently when constructing its bin range list.
*Verification:* test asserts a sector override produces the
override's bin count while other sectors use the global default.

**REQ-RG-COMPAT-REL-010: Per-sector sparse-bin floor**
The reliability assembler **shall** accept
`min_resolutions_per_bin_per_sector` and apply each sector's
override when flagging the `sparse` field.
*Verification:* test asserts an override flips the sparse
flag for the target sector.

### 3.8 ASCII calibration chart (v0.40.0)

**REQ-RG-COMPAT-CHART-001: Chart presence**
For each sector with at least one observation, the
reliability section **shall** include an ASCII calibration
chart after the per-bin table. The chart **shall** be
rendered identically by the terminal and markdown renderers
(the markdown renderer wraps it in a fenced code block to
preserve monospace alignment).
*Verification:* renderer tests assert the chart's legend
phrase appears in both outputs.

**REQ-RG-COMPAT-CHART-002: Chart glyphs**
The chart **shall** use:
- `.` for the perfect-calibration diagonal,
- `*` for non-sparse bin observations,
- `+` for sparse bin observations,
- `#` when an observation lands on the diagonal cell.
*Verification:* per-glyph tests assert each marker shows up
under the matching condition.

**REQ-RG-COMPAT-CHART-003: Chart dimensions**
The chart grid **shall** be 11 rows × 21 cols. Out-of-range
coordinate values **shall** clamp to the grid edge rather
than raise.
*Verification:* tests assert dimensions and clamping.

**REQ-RG-COMPAT-CHART-004: Linter compatibility**
The chart text **shall not** contain any phrase matched by
the shared `forbidden_phrases.yaml` linter.
*Verification:* a test passes representative chart text
through `position_engine.frame.linter.check_text` and
asserts it does not raise.

### 3.9 Additional measurement kinds (v0.41.0)

**REQ-RG-COMPAT-MEAS-006: single_venue_dominance_share**
The generator **shall** record a
``single_venue_dominance_share`` measurement on every cycle,
using the surfaced section's ``venue_shares`` mapping per
class. One observation per multi-venue class equal to the
maximum venue's share of combined 24h volume. Single-venue
classes contribute nothing.
*Verification:* unit + integration tests in
``test_threshold_measurements.py``.

**REQ-RG-COMPAT-MEAS-007: brier_per_sector**
The generator **shall** record a ``brier_per_sector``
measurement on every cycle, using the calibration section's
``sector_brier_scores`` list. One observation per sector.
*Verification:* unit + integration tests.

**REQ-RG-COMPAT-MEAS-008: SHIPPED_MEASUREMENT_KINDS contract**
The module-level tuple ``SHIPPED_MEASUREMENT_KINDS`` **shall**
list every measurement kind shipped by the project. Every
generator-recorded kind **shall** appear there.
*Verification:* test asserts every recorded kind during a
full generate() cycle is in the tuple.

### 3.10 Threshold percentile-rank inspection (v0.41.0)

**REQ-RG-COMPAT-EXPL-001: explain-thresholds CLI**
The CLI **shall** expose
``razor-rooster report explain-thresholds [--kind KIND]
[--db PATH]`` that prints, per shipped kind, the most-recent
cycle's measured_at + report_id, the configured threshold
value, n / n_above_threshold, and a percentile-rank line
showing where the threshold sits in this cycle's
distribution.
*Verification:* CLI tests in ``test_threshold_measurements.py``.

**REQ-RG-COMPAT-EXPL-002: Descriptive-only language**
The ``explain-thresholds`` output **shall not** contain any
phrase matched by the shared imperative-language linter.
*Verification:* a test runs the rendered output through
``position_engine.frame.linter.check_text``.

**REQ-RG-COMPAT-EXPL-003: threshold_percentile_rank helper**
A pure-function helper **shall** compute the percentile rank
of a threshold within a recorded distribution payload,
returning ``None`` when the payload has no observations or
no usable percentile values.
*Verification:* unit tests on the helper.

### 3.11 Threshold-suggestion engine (v0.41.0)

**REQ-RG-COMPAT-SUGG-001: suggest-thresholds engine**
A new engine ``engines/suggestions.py`` **shall** read the
most recent ``lookback_cycles`` measurements per kind,
average the recorded percentile cuts across cycles with
data, and emit one ``SuggestedThreshold`` per target
percentile. Cycles with zero observations are inspected but
do not contribute to averages.
*Verification:* engine tests in
``test_threshold_suggestions.py``.

**REQ-RG-COMPAT-SUGG-002: suggest-thresholds CLI**
The CLI **shall** expose
``razor-rooster report suggest-thresholds [--kind KIND]
[--lookback-cycles N] [--target-pct 0.70 [...]]
[--db PATH]``. Default targets: 0.50, 0.70, 0.90. Custom
targets are repeatable; values outside [0.0, 1.0] are
rejected.
*Verification:* CLI tests assert default + custom targets,
kind filter, and out-of-range rejection.

**REQ-RG-COMPAT-SUGG-003: Descriptive-only suggestions**
The ``suggest-thresholds`` output **shall not** contain any
phrase matched by the shared imperative-language linter. The
output reports what each percentile cut would mean if the
operator chose to use it; it never directs the operator to
apply a suggestion.
*Verification:* a test runs the rendered output through
``position_engine.frame.linter.check_text``.

### 3.12 Reversible config write path (v0.42.0)

**REQ-RG-COMPAT-APPLY-001: --apply requires explicit scoping**
The CLI ``--apply`` flag **shall** require ``--kind`` and
exactly one ``--target-pct``. Without both, the CLI exits
non-zero with a descriptive error.
*Verification:* CLI tests assert non-zero exit codes for
each missing-flag case.

**REQ-RG-COMPAT-APPLY-002: Timestamped backup before write**
On every successful ``--apply`` invocation, the helper
**shall** save a copy of the existing
``config/report.yaml`` to a sibling
``report.yaml.bak.<ISO timestamp>`` file before overwriting
the original. The backup **shall** be a byte-perfect copy.
*Verification:* a test asserts the backup file exists, has
the original bytes, and the live config has the updated
value.

**REQ-RG-COMPAT-APPLY-003: Refuse to silence guard rails**
The helper **shall** refuse to write
``target_percentile >= 1.0`` to
``single_venue_dominance_pct`` because that posture would
effectively silence the dominance warning. Operators who
genuinely want that posture edit the YAML by hand.
*Verification:* test asserts ``ApplyError`` is raised with a
descriptive message.

**REQ-RG-COMPAT-APPLY-004: Integer coercion for integer knobs**
Knobs flagged in ``INTEGER_VALUED_KNOBS`` (currently only
``cross_venue_spread_bps``) **shall** be rounded to the
nearest int before being written, so the YAML remains
clean.
*Verification:* test asserts the post-write YAML has an
int-typed value for an integer-valued knob.

**REQ-RG-COMPAT-APPLY-005: Operator confirmation gate**
Without ``--yes``, the CLI **shall** prompt the operator
for confirmation before writing. A "no" answer **shall**
preserve the original config unchanged. The prompt
language **shall** be descriptive, not imperative
(`"Proceed with this change?"`, not "Recommend applying").
*Verification:* CLI test feeds "n" to the prompt and
asserts the original config bytes are unchanged.

**REQ-RG-COMPAT-APPLY-006: Descriptive-only apply output**
The ``--apply`` confirmation prompt and the post-write
output **shall not** contain any phrase matched by the
shared imperative-language linter.
*Verification:* a test runs the rendered output through
``position_engine.frame.linter.check_text``.

### 3.13 Measurement retention (v0.42.0)

**REQ-RG-COMPAT-PRUNE-001: prune-measurements helper**
A helper ``prune_threshold_measurements`` **shall** delete
``report_threshold_measurements`` rows by absolute cutoff
(``before``) and/or by per-kind newest-N retention
(``keep_last``). The two strategies stack: rows are deleted
when *either* condition fires.
*Verification:* unit tests assert each strategy in
isolation and combined.

**REQ-RG-COMPAT-PRUNE-002: Confirm guard**
The helper **shall** raise
``PruneConfirmationError`` when called with
``confirm=False``. The CLI **shall** refuse to invoke the
helper unless ``--confirm`` is set.
*Verification:* unit and CLI tests assert the refusal.

**REQ-RG-COMPAT-PRUNE-003: At-least-one-strategy guard**
The helper **shall** raise ``ValueError`` when called
without either ``before`` or ``keep_last``. The CLI
**shall** echo a descriptive refusal and exit non-zero in
the same case.
*Verification:* unit and CLI tests assert the refusal.

**REQ-RG-COMPAT-PRUNE-004: Per-kind scoping**
The helper **shall** accept an optional ``measurement_kind``
filter to scope the prune to one kind. Without it, every
kind is considered.
*Verification:* test asserts a scoped prune deletes only
matching rows.

### 3.14 Stability metric (v0.42.0)

**REQ-RG-COMPAT-STAB-001: stability_cv field**
``ThresholdSuggestionReport.stability_cv`` **shall** be the
average coefficient of variation across percentile cuts:
per cut, take the standard deviation across cycles and
divide by the cut's mean; then average across cuts.
*Verification:* unit tests assert
``stability_cv == 0.0`` for identical-distribution cycles
and ``stability_cv > 0`` for divergent ones.

**REQ-RG-COMPAT-STAB-002: Two-cycle minimum**
``stability_cv`` **shall** be ``None`` when fewer than two
cycles of data are available (variation is undefined with
a single sample).
*Verification:* test asserts ``None`` for one cycle.

**REQ-RG-COMPAT-STAB-003: Unstable flag default threshold**
``ThresholdSuggestionReport.unstable`` **shall** be true
when ``stability_cv > DEFAULT_STABILITY_CV_THRESHOLD``.
The default threshold **shall** be 0.5 and **shall** be
overridable via the ``stability_cv_threshold`` parameter.
*Verification:* unit tests assert default and override
behavior.

**REQ-RG-COMPAT-STAB-004: CLI rendering**
The ``suggest-thresholds`` CLI **shall** print a
``stability:`` line per kind when ``stability_cv`` is
not ``None``. The line **shall** include the cv value and
a descriptive note (e.g. "stable; percentile cuts are
consistent across cycles" or "unstable; percentile cuts
vary widely cycle-to-cycle, suggestion is noisy").
*Verification:* CLI test asserts the line's presence and
phrasing across stable / unstable inputs.

**REQ-RG-COMPAT-STAB-005: Apply-prompt warning**
When the operator runs ``--apply`` and the underlying
distribution is flagged unstable, the confirmation prompt
**shall** prepend a short descriptive note so the operator
sees the instability before agreeing to the change. The
note **shall** be descriptive, not blocking — the operator
can still apply.
*Verification:* CLI test asserts the warning appears in
the prompt output when seeded with divergent cycles.

### 3.15 Auto-prune in cycle (v0.43.0)

**REQ-RG-COMPAT-AUTOPRUNE-001: AutoPruneConfig defaults**
``ReportConfig.auto_prune`` **shall** be a frozen
``AutoPruneConfig`` dataclass with three fields:
``enabled`` (default False), ``older_than_days`` (default
365), ``keep_last`` (default None).
*Verification:* unit tests assert defaults.

**REQ-RG-COMPAT-AUTOPRUNE-002: Opt-in by default**
The default ``enabled=False`` **shall** result in a no-op
prune at cycle close. Operators opt in by editing
``config/report.yaml``.
*Verification:* generator integration test asserts no rows
deleted when disabled.

**REQ-RG-COMPAT-AUTOPRUNE-003: Best-effort isolation**
A failure in the auto-prune hook **shall not** break
report generation. The exception **shall** be logged and
swallowed.
*Verification:* test patches ``prune_threshold_measurements``
to raise and asserts the report still succeeds.

**REQ-RG-COMPAT-AUTOPRUNE-004: No-op on missing strategy**
When ``enabled=True`` but neither ``older_than_days`` nor
``keep_last`` is set, the hook **shall** be a silent no-op.
*Verification:* test asserts no deletions in this case.

### 3.16 Apply-diff preview (v0.43.0)

**REQ-RG-COMPAT-DIFF-001: --diff requires --apply**
The CLI ``--diff`` flag **shall** be rejected when
``--apply`` is not also set. The CLI exits non-zero with a
descriptive error.
*Verification:* CLI test asserts non-zero exit.

**REQ-RG-COMPAT-DIFF-002: Unified-diff format**
The ``compute_apply_diff`` helper **shall** emit a
unified-diff-style string with ``---``, ``+++``, ``@@``,
``-``, and ``+`` markers showing the YAML line that would
change.
*Verification:* test asserts the markers and the
old/new/knob lines.

**REQ-RG-COMPAT-DIFF-003: Pure function**
``compute_apply_diff`` **shall not** modify the live
config file. It reads the YAML and returns a string.
*Verification:* test asserts file mtime is unchanged
across a diff invocation.

**REQ-RG-COMPAT-DIFF-004: Descriptive-only diff**
The diff output **shall not** contain any phrase matched
by the shared imperative-language linter.
*Verification:* CLI test runs ``check_text`` over the
rendered output.

### 3.17 Tuning log (v0.43.0)

**REQ-RG-COMPAT-TUNINGLOG-001: Table presence**
A new table ``threshold_tuning_log`` **shall** exist
under m7003. Columns: log_id, applied_at,
measurement_kind, knob, previous_value, new_value,
target_percentile, backup_path, note.
*Verification:* migration test asserts the table is
created with the expected columns.

**REQ-RG-COMPAT-TUNINGLOG-002: Apply path writes log entry**
On every successful ``suggest-thresholds --apply`` write,
the CLI **shall** persist a tuning-log row recording the
change. The log write **shall** be best-effort: a
log-side failure is logged but does not undo the config
write.
*Verification:* CLI integration test asserts the log
entry is created on apply, and a separate test patches
``persist_tuning_log_entry`` to raise and asserts the
config write still succeeds.

**REQ-RG-COMPAT-TUNINGLOG-003: --note flag**
A new ``--note TEXT`` flag on ``suggest-thresholds``
**shall** attach free-text operator commentary to the
tuning-log entry. Without the flag, the row's ``note``
field is NULL.
*Verification:* CLI test asserts the note round-trips.

**REQ-RG-COMPAT-TUNINGLOG-004: tuning-log CLI**
The CLI **shall** expose
``razor-rooster report tuning-log [--kind KIND]
[--since ISO] [--limit N] [--db PATH]`` listing
entries newest-first. Output **shall not** contain any
phrase matched by the shared imperative-language linter.
*Verification:* CLI tests assert filtering, ordering,
and linter compatibility.

**REQ-RG-COMPAT-TUNINGLOG-005: Skipped applies don't log**
Answering "n" at the apply confirmation prompt **shall**
both skip the config write and skip the tuning-log
write.
*Verification:* CLI test asserts the tuning-log table
is empty after an "n" response.

### 3.18 Tuning-log undo (v0.44.0)

**REQ-RG-COMPAT-UNDO-001: Undo CLI**
The CLI **shall** expose
``razor-rooster report tuning-log-undo <log_id> [--yes]
[--config PATH] [--db PATH]`` that restores
``config/report.yaml`` from the backup recorded in the
referenced tuning-log entry.
*Verification:* CLI integration test asserts an
end-to-end apply → undo restores the original config.

**REQ-RG-COMPAT-UNDO-002: Pre-undo backup**
Before restoring from the historical backup, the helper
**shall** save a fresh timestamped backup of the
current config so the undo is itself reversible. The
filename **shall** include microseconds so back-to-back
applies/undos don't collide.
*Verification:* test asserts the pre-undo backup exists
and contains the post-apply contents.

**REQ-RG-COMPAT-UNDO-003: Refusal paths**
The helper **shall** raise ``ApplyError`` when the
referenced tuning-log entry is missing
``backup_path``, when the backup file no longer exists,
or when the live config doesn't exist.
*Verification:* tests assert each refusal.

**REQ-RG-COMPAT-UNDO-004: Undo recorded as new entry**
A successful undo **shall** persist a new tuning-log
entry whose ``previous_value``/``new_value`` are
swapped relative to the original and whose ``note``
references the original ``log_id``.
*Verification:* CLI test asserts a new entry exists
after the undo with the expected swapped values and
note prefix.

**REQ-RG-COMPAT-UNDO-005: Descriptive-only language**
The CLI prompt and the post-undo output **shall not**
contain any phrase matched by the shared
imperative-language linter.
*Verification:* CLI test runs ``check_text`` over the
rendered output.

### 3.19 Recent-tuning report section (v0.44.0)

**REQ-RG-COMPAT-RECENT-001: Section presence**
A new ``recent_tuning`` body section **shall** be
available between ``system_health`` and ``surfaced`` in
``ALL_SECTIONS``. Opt-in via ``enabled_sections``;
default workspace config does not enable it.
*Verification:* test asserts ALL_SECTIONS ordering.

**REQ-RG-COMPAT-RECENT-002: Window filtering**
The assembler **shall** include only tuning-log entries
whose ``applied_at >= since_ts``. Older entries are
excluded.
*Verification:* test asserts.

**REQ-RG-COMPAT-RECENT-003: Newest-first ordering**
The assembler **shall** order entries newest-first.
*Verification:* test asserts.

**REQ-RG-COMPAT-RECENT-004: Missing-table robustness**
When the m7003 ``threshold_tuning_log`` table doesn't
exist (pre-migration store), the assembler **shall**
return an empty section instead of raising.
*Verification:* test asserts empty result on a fresh
DuckDB connection without migrations.

**REQ-RG-COMPAT-RECENT-005: Linter compatibility**
Both terminal and markdown renderings **shall** pass
the imperative-language linter.
*Verification:* tests run ``check_text`` over each.

### 3.20 HTML render mode (v0.44.0)

**REQ-RG-COMPAT-HTML-001: HTML renderer**
A new ``renderer/html.py`` module **shall** produce a
fully self-contained HTML document — inline CSS only,
no external fonts, no JavaScript, no images, no
network requests. Renders fine offline.
*Verification:* test asserts the output contains no
``http://`` or ``https://``, no ``<script>`` tags, and
no ``<link>`` tags.

**REQ-RG-COMPAT-HTML-002: HTML escape**
Operator-supplied text (notes, log_ids, condition_ids)
**shall** pass through ``html.escape()`` so special
characters don't break the document.
*Verification:* test seeds ``<>&`` in operator text and
asserts the output contains the escaped entities.

**REQ-RG-COMPAT-HTML-003: --html CLI flag**
``razor-rooster report generate`` **shall** accept a
``--html PATH`` option that writes the HTML to disk
parallel to the existing ``--markdown PATH``. The
parent directory **shall** be created if missing.
*Verification:* CLI test asserts the file is written.

**REQ-RG-COMPAT-HTML-004: Persistence**
``report_log`` **shall** carry two new columns
``rendered_html_text`` (TEXT NULL) and ``html_path``
(VARCHAR NULL) so the rendered HTML round-trips. The
m7004 migration **shall** be idempotent (uses
``PRAGMA table_info`` to detect existing columns
before issuing ``ALTER TABLE``).
*Verification:* tests assert round-trip through
``query_last_report``.

**REQ-RG-COMPAT-HTML-005: Linter compatibility**
The rendered HTML **shall** pass the
imperative-language linter.
*Verification:* test runs ``check_text`` over the
rendered output.

**REQ-RG-COMPAT-HTML-006: prefers-color-scheme support**
The inline CSS **shall** include
``@media (prefers-color-scheme: dark)`` rules so the
document renders in both light and dark modes
without configuration.
*Verification:* test asserts the rule is present in
the output.

### 3.21 Report-to-report compare (v0.45.0)

**REQ-RG-COMPAT-COMPARE-001: compare engine**
A new pure helper ``compare_reports(record_a, record_b)
-> ReportDiff`` **shall** produce a structured diff
covering metadata changes, sections added/removed,
section-failure delta, library-version drift,
disclaimer-hash drift, terminal-text length delta, and
a unified-diff preview of the rendered terminal text.
The helper **shall not** modify state.
*Verification:* unit tests assert each field of the
diff under representative inputs.

**REQ-RG-COMPAT-COMPARE-002: compare CLI**
The CLI **shall** expose
``razor-rooster report compare <report_id_a>
<report_id_b> [--diff/--no-diff] [--diff-lines N]
[--db PATH]`` that reads two reports by ID, runs the
compare helper, and emits a descriptive summary plus
optional bounded unified-diff preview.
*Verification:* CLI integration tests assert metadata
output and missing-report handling.

**REQ-RG-COMPAT-COMPARE-003: time-between absolute**
The diff's ``time_between`` field **shall** be the
absolute value of the duration between the two
``generated_at`` timestamps so passing reports in
either order gives the same delta.
*Verification:* test asserts symmetric output.

**REQ-RG-COMPAT-COMPARE-004: descriptive-only output**
The compare CLI output **shall** pass the shared
imperative-language linter.
*Verification:* CLI test runs ``check_text``.

### 3.22 Watch loop (v0.45.0)

**REQ-RG-COMPAT-WATCH-001: watch CLI**
The CLI **shall** expose
``razor-rooster report watch [--interval SEC]
[--html PATH] [--markdown PATH] [--once]
[--max-cycles N] [--db PATH]`` that runs
``report generate`` on a fixed cadence in a loop.
The same engine runs once per interval; the loop adds
no new analytical surface.
*Verification:* CLI integration tests with patched
``time.sleep``.

**REQ-RG-COMPAT-WATCH-002: interval bounds**
The ``--interval`` flag **shall** reject values
outside [60, 86400] seconds with a non-zero exit and
a descriptive error.
*Verification:* CLI test asserts both boundary
rejections.

**REQ-RG-COMPAT-WATCH-003: cycle-failure isolation**
A failure in one cycle **shall** be logged and the
loop **shall** continue. The error must not terminate
the watch.
*Verification:* CLI test patches ``generate`` to raise
on cycle 1 and asserts cycle 2 still runs.

**REQ-RG-COMPAT-WATCH-004: --once / --max-cycles**
The ``--once`` flag **shall** cause the watch to run
exactly one cycle and exit. The ``--max-cycles N``
flag **shall** cap the total cycle count.
*Verification:* CLI tests assert the exit summary
counts.

### 3.23 At-a-glance section (v0.45.0)

**REQ-RG-COMPAT-GLANCE-001: section presence**
A new ``at_a_glance`` body section **shall** be
available at index 0 of ``ALL_SECTIONS``. Opt-in via
``enabled_sections``; default workspace config does
not enable it.
*Verification:* test asserts ALL_SECTIONS ordering.

**REQ-RG-COMPAT-GLANCE-002: no independent ranking**
The assembler **shall** lift the top item from each
section's already-ordered list (cross_venue items
already sorted by spread_bps desc, surfaced
comparisons by confidence_weighted_score desc, etc.).
The assembler **shall not** introduce a new ranking,
score, or interpretation layer.
*Verification:* test seeds known-ordering content and
asserts the lifted items match.

**REQ-RG-COMPAT-GLANCE-003: structured key/value output**
The section's content dict **shall** be a list of
``{label, value, section}`` triples. No prose
synthesis. Renderers emit "label: value" lines
(terminal), bullet items with bold labels (markdown),
or `<dl>`/`<dt>`/`<dd>` definition lists (HTML).
*Verification:* tests assert the per-renderer output
shape.

**REQ-RG-COMPAT-GLANCE-004: section title**
The section title **shall** be "AT A GLANCE" (not
"Executive Summary" or any synthesizing header).
*Verification:* test asserts the rendered output
contains "AT A GLANCE".

**REQ-RG-COMPAT-GLANCE-005: extended editorial linter**
The shared ``forbidden_phrases.yaml`` catalog
**shall** include nine new editorial-flavor phrases
("particularly notable", "worth attention", "key
takeaway", "noteworthy", "you might want to",
"you'll want to", "worth a look", "worth looking at",
"the most important"). The shared imperative-language
linter picks them up automatically.
*Verification:* tests assert the phrases are present
in the catalog and that representative editorial
content trips the linter.

**REQ-RG-COMPAT-GLANCE-006: failed-section robustness**
When an upstream section's content is None (it
failed), the at-a-glance assembler **shall** skip
that section's fact extraction without raising.
*Verification:* test passes a content dict with
``None`` values and asserts the assembler returns
remaining facts.

**REQ-RG-COMPAT-GLANCE-007: opt-in by default**
The default workspace ``enabled_sections`` list
**shall not** include ``at_a_glance``. Operators opt
in by editing ``config/report.yaml``.
*Verification:* test reads the workspace config and
asserts the section is absent.

### 3.24 Watch --on-change skip (v0.46.0)

**REQ-RG-COMPAT-WATCH-CHANGE-001: --on-change flag**
The ``razor-rooster report watch`` subcommand
**shall** accept an optional ``--on-change`` flag.
When set, the loop **shall** compute an upstream
fingerprint at the start of each cycle and skip the
``generate()`` call when the fingerprint is identical
to the prior cycle's fingerprint.
*Verification:* CLI test asserts that with the flag
set and no upstream changes, no new ``report_log``
rows are persisted across multiple cycles.

**REQ-RG-COMPAT-WATCH-CHANGE-002: fingerprint composition**
The fingerprint **shall** consist of
``MAX(scan_id)`` from ``scan_summaries``,
``MAX(comparison_id)`` from ``comparisons``,
``MAX(follow_up_id)`` from ``follow_ups``, and
``MAX(log_id)`` from ``threshold_tuning_log``. Each
field defaults to ``None`` when the table is missing
or empty. The comparator treats two ``None`` values
as equal.
*Verification:* engine test asserts the four fields
appear in ``UpstreamFingerprint`` and that
``compute_upstream_fingerprint`` returns ``None``
for each missing-table case.

**REQ-RG-COMPAT-WATCH-CHANGE-003: first cycle always runs**
The first cycle of a watch loop **shall** always run,
regardless of the ``--on-change`` flag. The first
cycle's fingerprint becomes the baseline for
subsequent comparisons.
*Verification:* CLI test runs a single
``--once --on-change`` cycle on an empty store and
asserts a ``report_log`` row appears.

**REQ-RG-COMPAT-WATCH-CHANGE-004: skipped cycles count toward --max-cycles**
Skipped cycles **shall** count as cycles for
``--max-cycles N`` accounting. The exit summary
**shall** include the skip count when nonzero.
*Verification:* CLI test runs ``--max-cycles 5
--on-change`` against an unchanging store and
asserts "Watch exited after 1 cycle(s) (4 skipped)".

**REQ-RG-COMPAT-WATCH-CHANGE-005: read-only fingerprint**
The fingerprint computation **shall not** modify
state. It **shall** be safe to run against a
read-only DuckDB connection.
*Verification:* engine test asserts that
``compute_upstream_fingerprint`` does not issue any
INSERT/UPDATE/DELETE statements (audited via
``LinterCatalog`` of forbidden DML keywords if
needed; v0.46.0 satisfies via direct code inspection
plus the existing CLI test that runs against a
fresh fixture and asserts no row counts change).

### 3.25 Compare --html two-column view (v0.46.0)

**REQ-RG-COMPAT-COMPARE-HTML-001: --html flag**
The ``razor-rooster report compare`` subcommand
**shall** accept an optional ``--html PATH`` flag.
When set, the CLI **shall** write a self-contained
HTML document at ``PATH`` rendering the comparison
in a two-column side-by-side layout.
*Verification:* CLI test asserts ``PATH`` exists and
contains ``<!DOCTYPE html>``, ``<style>``, the two
report ids, and the rendered terminal text from
both reports.

**REQ-RG-COMPAT-COMPARE-HTML-002: self-contained**
The rendered HTML **shall not** reference external
resources. It **shall not** contain ``src=``
attributes, ``<script>`` tags (other than within
escaped report content), ``http://`` URLs, or
``https://`` URLs. The styling **shall** be inline
via a single ``<style>`` block.
*Verification:* CLI test asserts the rendered file
contains none of those substrings (with
``<script`` count ≤ 0).

**REQ-RG-COMPAT-COMPARE-HTML-003: HTML escape**
All operator-supplied content (report ids, terminal
text, sections) **shall** be HTML-escaped via
``html.escape(..., quote=True)`` before
interpolation. A ``<script>alert(1)</script>``
substring inside a report's terminal text **shall**
appear as ``&lt;script&gt;alert(1)&lt;/script&gt;``
in the output.
*Verification:* adversarial test feeds the
``<script>`` substring as terminal text and asserts
the escaped form appears while the literal form
does not.

**REQ-RG-COMPAT-COMPARE-HTML-004: imperative-language linter**
Before writing the HTML to disk, the CLI **shall**
pass the rendered content through
``position_engine.frame.linter.check_text``. A
linter rejection **shall** prevent the file from
being written and propagate the exception.
*Verification:* CLI test runs the linter directly
on the rendered output of a normal-content
comparison.

**REQ-RG-COMPAT-COMPARE-HTML-005: parent directory creation**
If the parent directory of ``--html PATH`` doesn't
exist, the CLI **shall** create it (recursively)
before writing.
*Verification:* CLI test passes a nested path and
asserts the file appears at that path.

**REQ-RG-COMPAT-COMPARE-HTML-006: metadata diff highlighting**
Changed metadata fields (library version,
disclaimer hash, terminal length) **shall** be
visually distinguished from unchanged fields. The
template **shall** apply ``class="changed"`` to the
table row when the value differs.
*Verification:* CLI test feeds two reports with
different library versions and asserts
``class="changed"`` appears in the metadata table.

**REQ-RG-COMPAT-COMPARE-HTML-007: section presence diff styling**
Sections added in the newer report **shall** carry
``class="added"`` styling; sections removed
**shall** carry ``class="removed"``. The semantic
distinction is rendered via background color
following the dark/light ``prefers-color-scheme``
palette.
*Verification:* CLI test feeds two reports with
disjoint section sets and asserts both classes
appear in the rendered HTML.

### 3.26 Recent-reports digest (v0.46.0)

**REQ-RG-COMPAT-DIGEST-001: digest CLI**
A new subcommand ``razor-rooster report digest
[--days N]`` **shall** print a one-line-per-report
digest of reports persisted within the last N days.
Default ``--days`` is 7.
*Verification:* CLI test seeds three
``report_log`` rows at varying ages, runs ``digest
--days 20``, and asserts only the rows within the
window appear.

**REQ-RG-COMPAT-DIGEST-002: --days range validation**
``--days`` **shall** be bounded to ``[1, 365]``.
Out-of-range values **shall** raise
``click.BadParameter`` with a non-zero exit code.
*Verification:* CLI tests pass ``--days 0`` and
``--days 366`` and assert the command exits with a
non-zero code and an "out of range" message.

**REQ-RG-COMPAT-DIGEST-003: digest line format**
Each row **shall** include the generated_at
timestamp (ISO 8601), report_id,
``sections=R/E`` (rendered/enabled), ``failed=F``,
``terminal_chars=L``, and bracketed
``[md]``/``[html]``/``[md, html]`` markers when the
underlying ``ReportRecord`` persisted those output
paths.
*Verification:* CLI test seeds a report with both
markdown_path and html_path and asserts both
markers appear in the row.

**REQ-RG-COMPAT-DIGEST-004: ordering**
Rows **shall** appear in newest-first order, the
same ordering ``list_reports`` already returns.
*Verification:* CLI test seeds three reports and
asserts the newest report_id substring appears
before the older ones in the output.

**REQ-RG-COMPAT-DIGEST-005: empty-window message**
When no reports fall within the window, the CLI
**shall** print a benign "No reports in the last
N day(s)." message and exit successfully.
*Verification:* CLI test runs ``digest`` against
an empty store and asserts the message appears
with exit code 0.

**REQ-RG-COMPAT-DIGEST-006: imperative-language linter**
The digest output **shall** pass the shared
imperative-language linter
``position_engine.frame.linter.check_text``.
*Verification:* CLI test runs ``check_text`` on a
populated digest output.

### 3.27 Compare unified-diff HTML panel (v0.47.0)

**REQ-RG-COMPAT-COMPARE-HTML-DIFF-001: panel presence**
The compare-HTML page **shall** include a fourth
``<section>`` rendering the unified terminal-text diff
between report A and report B as a color-highlighted
list of lines.
*Verification:* CLI test asserts ``<h2>Unified diff</h2>``
and ``unified-diff`` (CSS class) appear in the rendered
page.

**REQ-RG-COMPAT-COMPARE-HTML-DIFF-002: line classification**
Each diff line **shall** be wrapped in a
``<div class="diff-line ...">`` with a semantic class:
``diff-add`` for additions, ``diff-del`` for deletions,
``diff-hunk`` for ``@@`` lines, ``diff-meta`` for
``---``/``+++`` file headers, ``diff-context``
otherwise.
*Verification:* CLI test asserts each class name
appears at least once in the rendered HTML for an
input that exercises every category.

**REQ-RG-COMPAT-COMPARE-HTML-DIFF-003: --diff-lines truncation**
The ``--diff-lines`` flag **shall** apply uniformly to
both the terminal output and the HTML unified-diff
panel. When the diff exceeds the cap, a
``<div class="diff-truncated">`` footer **shall** name
the count of truncated lines.
*Verification:* CLI test runs with ``--diff-lines 5``
against a diff with many more lines and asserts the
truncation footer appears.

**REQ-RG-COMPAT-COMPARE-HTML-DIFF-004: empty diff message**
When the unified-diff payload is empty (the rendered
terminal text of both reports is identical), the panel
**shall** emit a benign ``"No textual differences..."``
message rather than an empty container.
*Verification:* CLI test feeds two reports with
identical terminal text and asserts the message
appears.

### 3.28 Digest aggregation header (v0.47.0)

**REQ-RG-COMPAT-DIGEST-AGG-001: aggregate stats line**
When at least one report is in the window, the digest
output **shall** include a header section above the
per-row listing reporting: total report count, count of
cycles with at least one failed section, count with
persisted markdown_path, count with persisted html_path,
average sections rendered per cycle (one decimal place),
and average terminal-text length per cycle (rounded to
the nearest character).
*Verification:* CLI test seeds three reports with
varied metadata and asserts each metric value appears
verbatim in the output.

**REQ-RG-COMPAT-DIGEST-AGG-002: zero-failure clean rendering**
The header **shall** render correctly when every
report has zero failed sections / no markdown / no
HTML (zero counts emitted as ``0``, not omitted).
*Verification:* CLI test seeds clean reports and
asserts ``cycles with failures: 0`` appears.

**REQ-RG-COMPAT-DIGEST-AGG-003: linter compatibility**
The aggregate header **shall** pass the shared
imperative-language linter.
*Verification:* CLI test runs ``check_text`` on a
populated digest output that includes the header.

### 3.29 Watch on-change resume summary (v0.47.0)

**REQ-RG-COMPAT-WATCH-CHANGE-RESUME-001: resume note**
When ``--on-change`` is set and the loop transitions
from skipping to running after one or more skipped
cycles, the next non-skipped cycle's log line **shall**
include a parenthesized note of the form ``(resume
after N skipped: <fields> changed)`` naming the
fingerprint field(s) whose value differs from the prior
fingerprint.
*Verification:* CLI test seeds an upstream change mid-
loop and asserts ``resume after`` and the changed-field
label appear in the watch output.

**REQ-RG-COMPAT-WATCH-CHANGE-RESUME-002: no resume note on first cycle**
The first cycle of a watch loop **shall not** emit a
resume note (it has no prior fingerprint to compare
against).
*Verification:* CLI test runs ``--once --on-change``
and asserts ``"resume after"`` does not appear in the
output.

**REQ-RG-COMPAT-WATCH-CHANGE-RESUME-003: field-label vocabulary**
The ``_diff_fingerprint_fields`` helper **shall** emit
short labels matching the four upstream tables:
``scan``, ``comparison``, ``follow_up``,
``tuning_log``. The output ordering **shall** follow
the declaration order, not field-content order.
*Verification:* unit test compares two
``UpstreamFingerprint`` instances differing in
multiple fields and asserts the label list.

### 3.30 ANSI-to-HTML translator (v0.47.0)

**REQ-RG-COMPAT-ANSI-001: strip_ansi helper**
A pure helper ``strip_ansi(text) -> str`` **shall**
remove every ANSI CSI sequence (SGR, cursor moves,
screen clears) from a string, returning plain text.
*Verification:* unit tests cover SGR sequences,
non-SGR CSI sequences, plain-text passthrough, and
empty string.

**REQ-RG-COMPAT-ANSI-002: ansi_to_html helper**
A pure helper ``ansi_to_html(text) -> str`` **shall**
translate ANSI SGR sequences into inline ``<span>``
elements with semantic CSS class names. The eight
standard foreground colors (codes 30–37), the eight
bright foreground colors (codes 90–97), bold (1),
dim (2), italic (3), and underline (4) **shall** be
supported. Reset codes 0 (full) and 39 (foreground
only) **shall** close open spans correctly.
*Verification:* unit tests assert the class name
appears for each supported SGR code and that resets
close spans.

**REQ-RG-COMPAT-ANSI-003: HTML escape**
``ansi_to_html`` **shall** HTML-escape the underlying
text content (``html.escape(..., quote=True)``)
before splicing in spans. Class names **shall** be
fixed strings, not derived from input, so no
user-controlled CSS can be injected.
*Verification:* unit test feeds ``<script>alert(1)</script>``
and asserts the escaped form appears while the
literal form does not.

**REQ-RG-COMPAT-ANSI-004: well-nested output**
The output of ``ansi_to_html`` **shall** be
well-nested: every ``<span`` substring **shall** have
a matching ``</span>``.
*Verification:* unit test asserts equal counts.

**REQ-RG-COMPAT-ANSI-005: defensive use in compare-HTML**
The compare-HTML side-by-side panel **shall** route
each report's ``rendered_terminal_text`` through
``ansi_to_html`` before splicing, and **shall** embed
the ``ANSI_INLINE_CSS`` palette in the page's inline
``<style>`` block. Today's terminal renderer doesn't
emit ANSI, so the translator is currently a no-op;
this requirement is defensive against future renderer
changes and externally-pasted terminal text.
*Verification:* CLI test feeds an ANSI-tagged
``rendered_terminal_text`` value, runs ``compare
--html``, and asserts (a) the raw escape character
``\x1b`` does not appear in the output, (b) the
expected semantic classes (``ansi-fg-red``,
``ansi-bold``) appear, and (c) the inline CSS
includes the palette declarations.

**REQ-RG-COMPAT-ANSI-006: silent drop of unsupported codes**
SGR codes outside the supported subset (background
colors, 256-color, RGB, intensity codes other than
1/2, etc.) **shall** be silently dropped. The
underlying text content **shall** be preserved.
*Verification:* unit tests feed background-color
(SGR 41) and 256-color (SGR ``38;5;n``) sequences
and assert the surrounding text appears while no
class is added.

### 3.31 Compare-HTML word-level diff (v0.48.0)

**REQ-RG-COMPAT-COMPARE-HTML-WORD-001: paired
del/add runs**
When the unified-diff input contains a run of one or
more consecutive deletion lines (``-`` prefix)
immediately followed by a run of the same length of
addition lines (``+`` prefix), the renderer **shall**
pair them element-wise (the i-th del with the i-th
add) and emit word-level highlight spans inside the
existing line-level styling.
*Verification:* CLI test feeds two reports differing
only in the trailing token of a single line and
asserts the ``word-del`` span wraps the old token
and the ``word-add`` span wraps the new token.

**REQ-RG-COMPAT-COMPARE-HTML-WORD-002: tokenization**
Lines **shall** be split into alternating word-character
and non-word-character runs via
``re.findall(r"\\w+|\\W+", body)``. Empty matches
**shall** be filtered out. ``SequenceMatcher`` **shall**
operate on the resulting token list.
*Verification:* helper unit test asserts the helper
returns paired (del_html, add_html) HTML strings
where unchanged tokens appear unwrapped and changed
tokens carry the word-add/word-del classes.

**REQ-RG-COMPAT-COMPARE-HTML-WORD-003: unequal-run
fallback**
When the deletion-run length differs from the
following addition-run length (e.g. two deletions vs
one insertion), the renderer **shall** fall back to
whole-line styling (the existing diff-del / diff-add
classes) and **shall not** emit word-level spans for
the unbalanced run.
*Verification:* CLI test feeds a 4-line removal +
2-line insertion and asserts diff-del and diff-add
appear in the output without any word-add / word-del
spans inside the unbalanced segment.

**REQ-RG-COMPAT-COMPARE-HTML-WORD-004: marker
preservation**
The leading ``-`` / ``+`` line marker **shall** stay
unwrapped (no span around it) so the line
classification remains visible.
*Verification:* unit test asserts the helper
prepends the marker outside any word-* span.

**REQ-RG-COMPAT-COMPARE-HTML-WORD-005: HTML escape**
HTML-special characters inside changed words **shall**
be escaped via ``html.escape(..., quote=True)`` before
splicing into the spans.
*Verification:* adversarial CLI test introduces
``<script>x</script>`` as the changed word and
asserts the literal form does not appear; the
escaped form does.

### 3.32 Digest --json output (v0.48.0)

**REQ-RG-COMPAT-DIGEST-JSON-001: --json flag**
The ``digest`` subcommand **shall** accept an optional
``--json`` flag. When set, the command **shall** emit
JSON Lines: one ``{"kind": "report", ...}`` object
per report (newest first) followed by a single
``{"kind": "aggregate", ...}`` object.
*Verification:* CLI test runs the JSON path and
parses each output line as standalone JSON.

**REQ-RG-COMPAT-DIGEST-JSON-002: report object shape**
Each report object **shall** include the keys:
``report_id``, ``generated_at`` (ISO 8601 string),
``sections_rendered`` (int), ``sections_enabled``
(int), ``sections_failed`` (int), ``terminal_chars``
(int), ``markdown_path`` (string or null), and
``html_path`` (string or null).
*Verification:* CLI test asserts every key is
present in the parsed object.

**REQ-RG-COMPAT-DIGEST-JSON-003: aggregate object shape**
The aggregate object **shall** include the keys:
``window`` (string label), ``since`` (ISO 8601
string), ``report_count`` (int),
``cycles_with_failures`` (int),
``cycles_with_markdown`` (int),
``cycles_with_html`` (int),
``avg_sections_rendered`` (float or null when no
reports), ``avg_terminal_chars`` (float or null when
no reports).
*Verification:* CLI tests cover both populated and
empty windows.

**REQ-RG-COMPAT-DIGEST-JSON-004: empty window**
When no reports fall in the window, ``--json``
**shall** emit only the aggregate object (one line),
with ``report_count: 0`` and ``avg_*: null``.
*Verification:* CLI test runs against an empty store
and asserts a single line with the aggregate.

### 3.33 Watch-loop exit summary block (v0.48.0)

**REQ-RG-COMPAT-WATCH-SUMMARY-001: avg cycle duration**
When at least one cycle ran (success or failure), the
watch-loop exit output **shall** include a line
reporting the average cycle duration in seconds with
millisecond precision and the count of failed cycles.
*Verification:* CLI test asserts ``avg cycle
duration:`` and ``cycles failed:`` appear after a
single ``--once`` run.

**REQ-RG-COMPAT-WATCH-SUMMARY-002: failure counting**
A cycle that raised an exception during ``generate()``
**shall** count toward the ``cycles failed`` total in
the summary even though the loop continued.
*Verification:* CLI test patches ``generate`` to
raise and asserts ``cycles failed: 1``.

**REQ-RG-COMPAT-WATCH-SUMMARY-003: distinct field
listing**
When ``--on-change`` is set and one or more
fingerprint-field changes drove a non-skipped cycle,
the summary **shall** include a line listing the
distinct fingerprint field labels encountered across
the loop, in alphabetical order.
*Verification:* CLI test seeds a tuning-log change
mid-loop and asserts ``fingerprint fields changed
during loop:`` appears with ``tuning_log`` in the
list.

**REQ-RG-COMPAT-WATCH-SUMMARY-004: total skip time**
When at least one cycle was skipped, the summary
**shall** include a line of the form
``total skip time: ~S s (M cycle(s) x I s interval)``
where S = M × I.
*Verification:* CLI test runs ``--max-cycles 5
--on-change`` against an unchanging store and
asserts the matching string appears in the output.

**REQ-RG-COMPAT-WATCH-SUMMARY-005: short happy path**
When no cycles were skipped, the ``total skip time``
line **shall not** appear. When no fingerprint field
ever changed, the ``fingerprint fields changed``
line **shall not** appear.
*Verification:* CLI test runs ``--once`` and
asserts neither line appears.

### 3.34 Digest --since window override (v0.48.0)

**REQ-RG-COMPAT-DIGEST-SINCE-001: --since flag**
The ``digest`` subcommand **shall** accept an optional
``--since ISO`` flag mutually exclusive with
``--days``. When set, the window starts at the
specified ISO 8601 timestamp.
*Verification:* CLI test seeds a report inside the
window and one outside and asserts only the inside
report appears.

**REQ-RG-COMPAT-DIGEST-SINCE-002: mutual exclusivity**
Passing both ``--days`` and ``--since`` **shall** raise
``click.BadParameter`` with a "mutually exclusive"
message.
*Verification:* CLI test asserts the failure mode
and message.

**REQ-RG-COMPAT-DIGEST-SINCE-003: ISO validation**
A non-ISO ``--since`` value **shall** raise
``click.BadParameter`` with an "invalid --since"
message before any DB access.
*Verification:* CLI test passes ``--since not-a-date``
and asserts the failure mode and message.

**REQ-RG-COMPAT-DIGEST-SINCE-004: naive ISO is UTC**
A naive ISO timestamp (no tzinfo) **shall** be
interpreted as UTC. The window label in both the
terminal output and the JSON aggregate **shall**
use the resolved tz-aware form.
*Verification:* CLI test passes a naive ISO and
asserts the resolved timezone-aware form appears in
the output.

**REQ-RG-COMPAT-DIGEST-SINCE-005: combines with --json**
The ``--since`` flag **shall** be compatible with
``--json``. The aggregate object's ``window`` field
**shall** start with ``"since "`` when ``--since``
is set.
*Verification:* CLI test runs both flags and parses
the aggregate; asserts the ``window`` value.

### 3.35 Compare --no-word-diff toggle (v0.49.0)

**REQ-RG-COMPAT-COMPARE-HTML-NO-WORD-001: flag**
The ``compare`` subcommand **shall** accept a
``--word-diff/--no-word-diff`` flag (default
``--word-diff``). When ``--no-word-diff`` is set,
the HTML unified-diff panel **shall** apply only
line-level coloring; word-level highlight spans
(``word-add``/``word-del``) **shall not** be emitted.
*Verification:* CLI test asserts the ``<span class=
"word-del">``/``"word-add">`` markup is absent in
the rendered HTML when the flag is set, while the
line-level ``diff-del``/``diff-add`` classes still
appear.

**REQ-RG-COMPAT-COMPARE-HTML-NO-WORD-002: helper
parameter**
The ``_render_diff_rows_with_word_highlights`` helper
**shall** accept a ``word_diff: bool = True``
keyword argument. When False, paired del/add lines
**shall** fall back to whole-line styling regardless
of run length.
*Verification:* unit test feeds an equal-length
del/add pair with ``word_diff=False`` and asserts
no word-level spans appear in the output.

### 3.36 Watch --summary-file (v0.49.0)

**REQ-RG-COMPAT-WATCH-SUMMARY-FILE-001: flag**
The ``watch`` subcommand **shall** accept an
optional ``--summary-file PATH`` flag. When set,
after the loop exits, the exit-summary block
**shall** be written to ``PATH`` in addition to
being echoed to stdout.
*Verification:* CLI test asserts the file exists
and contains the same lines emitted on stdout.

**REQ-RG-COMPAT-WATCH-SUMMARY-FILE-002: JSON
dispatch on suffix**
When ``PATH`` ends in ``.json``, the file **shall**
contain a single JSON object with keys: ``kind``
(``"watch_summary"``), ``cycles_run``,
``cycles_skipped``, ``cycles_failed``,
``avg_cycle_duration_seconds`` (float or null when
no cycles ran), ``fingerprint_fields_changed``
(sorted list), ``total_skip_seconds``,
``interval_seconds``.
*Verification:* CLI test parses the JSON output
and asserts every key is present with correct
types.

**REQ-RG-COMPAT-WATCH-SUMMARY-FILE-003: parent
directory creation**
If the parent directory of ``PATH`` doesn't exist,
the CLI **shall** create it (recursively) before
writing.
*Verification:* CLI test passes a nested path and
asserts the file appears at that path.

### 3.37 Digest --report-id PREFIX filter (v0.49.0)

**REQ-RG-COMPAT-DIGEST-PREFIX-001: flag**
The ``digest`` subcommand **shall** accept an
optional ``--report-id PREFIX`` flag. When set,
only reports whose ``report_id`` starts with
``PREFIX`` (using ``str.startswith``) **shall**
appear in the output.
*Verification:* CLI test seeds reports with
varied ids and asserts only the matching ones
appear.

**REQ-RG-COMPAT-DIGEST-PREFIX-002: composability**
``--report-id`` **shall** combine cleanly with
``--days``, ``--since``, and ``--json``. The
filter applies after the time-window filter.
*Verification:* CLI tests for each pairwise
combination assert correct intersection
semantics.

**REQ-RG-COMPAT-DIGEST-PREFIX-003: empty-match
message**
When the prefix matches no reports, the empty
message **shall** mention the prefix:
``"No reports {window_label} (filtered by
report-id prefix '<PREFIX>')."``.
*Verification:* CLI test asserts the message.

**REQ-RG-COMPAT-DIGEST-PREFIX-004: aggregate
field**
The ``--json`` aggregate object **shall** include
a ``report_id_prefix`` field carrying the prefix
string when set, or ``null`` when not set.
*Verification:* CLI tests parse the aggregate
and assert the field's value in both modes.

### 3.38 Compare --no-side-by-side toggle (v0.49.0)

**REQ-RG-COMPAT-COMPARE-HTML-NO-SBS-001: flag**
The ``compare`` subcommand **shall** accept a
``--side-by-side/--no-side-by-side`` flag
(default ``--side-by-side``). When
``--no-side-by-side`` is set, the HTML page
**shall not** include the two-column terminal-
text panel (the ``"Side-by-side terminal text"``
``<h2>`` heading and the ``side-by-side``
container).
*Verification:* CLI test asserts the heading
and container class are absent in the rendered
HTML.

**REQ-RG-COMPAT-COMPARE-HTML-NO-SBS-002: other
sections preserved**
When ``--no-side-by-side`` is set, the metadata
table, sections-changed list, unified-diff
panel, and disclaimer footer **shall** still
appear. The compare page remains a valid
self-contained HTML document.
*Verification:* CLI test asserts the
``<h2>Metadata</h2>``, ``<h2>Sections</h2>``,
``<h2>Unified diff</h2>`` headings appear and
the closing ``</html>`` tag is present.

**REQ-RG-COMPAT-COMPARE-HTML-NO-SBS-003: composes
with --no-word-diff**
``--no-side-by-side`` **shall** be combinable
with ``--no-word-diff`` to produce the most
compact compare-HTML view.
*Verification:* CLI test sets both flags and
asserts neither the side-by-side panel nor any
``<span class="word-del">``/``"word-add">``
markup appears, while the line-level
``diff-del``/``diff-add`` styling does.

### 3.39 Watch --summary-file rotation (v0.50.0)

**REQ-RG-COMPAT-WATCH-SUMMARY-ROTATE-001:
{timestamp} placeholder**
The ``--summary-file`` path **shall** support a
literal ``{timestamp}`` placeholder. When present,
the placeholder **shall** be replaced with the UTC
ISO 8601 timestamp at exit (seconds precision)
with colons replaced by hyphens for filesystem
safety.
*Verification:* CLI test passes
``summary-{timestamp}.txt`` and asserts the
written file's name matches
``summary-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\+\d{2}-\d{2}\.txt``.

**REQ-RG-COMPAT-WATCH-SUMMARY-ROTATE-002:
no-placeholder backward compat**
A ``--summary-file`` path without the
``{timestamp}`` placeholder **shall** be used
unchanged. No "summary written to" announcement
**shall** appear in stdout in this case.
*Verification:* CLI test passes a literal path
and asserts the file appears at exactly that
path with no extra announcement.

**REQ-RG-COMPAT-WATCH-SUMMARY-ROTATE-003:
suffix-driven dispatch preserved**
The substitution **shall** preserve the suffix
so JSON dispatch (paths ending in ``.json``)
still produces a single
``{"kind": "watch_summary", ...}`` object.
*Verification:* CLI test passes
``summary-{timestamp}.json`` and parses the
written file as JSON.

### 3.40 Digest --sort-by (v0.50.0)

**REQ-RG-COMPAT-DIGEST-SORT-001: --sort-by flag**
The ``digest`` subcommand **shall** accept a
``--sort-by FIELD`` option restricted to
``generated_at`` / ``sections_failed`` /
``terminal_chars`` (case-insensitive via
``click.Choice``). The default **shall** be
``generated_at``.
*Verification:* CLI tests for each supported
value assert the per-row listing comes back in
the requested order.

**REQ-RG-COMPAT-DIGEST-SORT-002: --sort-direction
flag**
The ``digest`` subcommand **shall** accept a
``--sort-direction {asc,desc}`` option
(case-insensitive via ``click.Choice``). The
default **shall** be ``desc`` (largest /
newest / highest first).
*Verification:* CLI test asserts ``--sort-by
terminal_chars --sort-direction asc`` puts the
shortest report first.

**REQ-RG-COMPAT-DIGEST-SORT-003: secondary sort
on tie**
When two reports tie on the primary sort key,
the secondary sort **shall** be ``generated_at
desc`` so newer reports still appear first.
*Verification:* unit test on
``_sort_digest_reports`` with two reports
sharing ``terminal_chars`` value asserts the
newer report comes first.

**REQ-RG-COMPAT-DIGEST-SORT-004: sort applies
to --json output**
The ``--sort-by``/``--sort-direction`` settings
**shall** apply to the per-report objects in
``--json`` output too.
*Verification:* CLI test runs
``--sort-by sections_failed --json`` and
asserts the first per-report object is the
most-failed cycle.

**REQ-RG-COMPAT-DIGEST-SORT-005: invalid
field rejection**
Unknown ``--sort-by`` values **shall** be
rejected with a non-zero exit code (delegated
to ``click.Choice``).
*Verification:* CLI test asserts a non-zero
exit when ``--sort-by library_version`` is
passed.

### 3.41 Compare-HTML deep-link anchors (v0.50.0)

**REQ-RG-COMPAT-COMPARE-HTML-ANCHORS-001:
section ids**
Each rendered ``<section>`` in the compare-HTML
output **shall** carry a stable id attribute:
``id="metadata"``, ``id="sections"``,
``id="side-by-side"`` (when present), and
``id="unified-diff"``.
*Verification:* CLI test asserts each id appears
in the output.

**REQ-RG-COMPAT-COMPARE-HTML-ANCHORS-002:
quick-jump nav**
The header **shall** include a
``<nav class="quick-jump muted">`` block listing
``<a href="#...">`` links to each present
section in the order metadata, sections, side-
by-side (when present), unified diff.
*Verification:* CLI test asserts the nav block
and four ``href="#..."`` links appear.

**REQ-RG-COMPAT-COMPARE-HTML-ANCHORS-003:
omit suppressed sections**
When ``--no-side-by-side`` is set, the nav
**shall not** include the ``href="#side-by-
side"`` link, and the corresponding section
**shall not** be emitted.
*Verification:* CLI test asserts neither the
nav link nor the section is present in that
mode.

### 3.42 `report compare-latest` shortcut (v0.50.0)

**REQ-RG-COMPAT-COMPARE-LATEST-001: subcommand**
The CLI **shall** expose a new subcommand
``razor-rooster report compare-latest``. It
**shall** resolve the two newest persisted
reports via ``list_reports(conn, limit=2)``
(newer is ``b``, older is ``a``) and forward
the rendering flags (``--diff/--no-diff``,
``--diff-lines``, ``--html``,
``--word-diff/--no-word-diff``,
``--side-by-side/--no-side-by-side``, ``--db``)
to ``report compare``.
*Verification:* CLI test seeds three reports and
asserts the announcement names the two newest
ids in ``a=<older>  b=<newer>`` form.

**REQ-RG-COMPAT-COMPARE-LATEST-002: refuses on
fewer than two reports**
When fewer than two reports are persisted, the
subcommand **shall** print ``Need at least 2
reports for compare-latest; found N.`` to
stderr and exit with a non-zero code.
*Verification:* CLI tests assert the message
on a 1-report store and an empty store.

**REQ-RG-COMPAT-COMPARE-LATEST-003: HTML
output forwarding**
``compare-latest --html PATH`` **shall** write
the same self-contained HTML page that
``report compare A B --html PATH`` would
produce for the resolved pair.
*Verification:* CLI test passes ``--html`` and
asserts the file exists with both report ids
referenced.

**REQ-RG-COMPAT-COMPARE-LATEST-004: rendering
flag forwarding**
``compare-latest`` **shall** forward
``--no-word-diff`` and ``--no-side-by-side`` to
the same renderer used by ``compare``.
*Verification:* CLI test sets both flags and
asserts the rendered HTML lacks the suppressed
panel and the word-level spans.

### 3.43 Compare --no-quick-jump (v0.51.0)

**REQ-RG-COMPAT-COMPARE-HTML-NO-QJ-001: flag**
The ``compare`` and ``compare-latest`` subcommands
**shall** accept a
``--quick-jump/--no-quick-jump`` flag (default
``--quick-jump``). When ``--no-quick-jump`` is
set, the rendered HTML page's header **shall not**
include the ``<nav class="quick-jump muted">``
block.
*Verification:* CLI test asserts the nav class
is absent in that mode.

**REQ-RG-COMPAT-COMPARE-HTML-NO-QJ-002: section
ids preserved**
``--no-quick-jump`` **shall** only affect the nav
block. The ``<section id="...">`` attributes
**shall** still be emitted so deep linking works
for operators who construct URLs by hand.
*Verification:* CLI test asserts the ids appear
even when the nav is suppressed.

**REQ-RG-COMPAT-COMPARE-HTML-NO-QJ-003: composes
with other compactness flags**
``--no-quick-jump`` **shall** combine with
``--no-side-by-side`` and ``--no-word-diff`` to
produce a maximally-stripped page.
*Verification:* CLI test sets all three flags
and asserts the corresponding markup is absent
while the line-level diff classes remain.

### 3.44 compare-latest --offset (v0.51.0)

**REQ-RG-COMPAT-COMPARE-LATEST-OFFSET-001: flag**
The ``compare-latest`` subcommand **shall** accept
an optional ``--offset N`` flag (default 0). When
set, the diff **shall** target reports
``[N]`` (newer ``b``) and ``[N + 1]`` (older
``a``) from the newest-first list.
*Verification:* CLI test seeds 4 reports and
asserts each ``--offset 0/1/2`` resolves the
expected pair.

**REQ-RG-COMPAT-COMPARE-LATEST-OFFSET-002:
non-negative**
``--offset`` **shall** reject negative values via
``click.BadParameter``.
*Verification:* CLI test asserts a non-zero
exit on ``--offset -1``.

**REQ-RG-COMPAT-COMPARE-LATEST-OFFSET-003:
sufficient reports**
``compare-latest --offset N`` **shall** require
at least ``N + 2`` reports. The refusal message
**shall** name the required count and the
actual count.
*Verification:* CLI test seeds 2 reports and
asserts ``Need at least 4 reports for
compare-latest --offset 2; found 2.`` appears.

**REQ-RG-COMPAT-COMPARE-LATEST-OFFSET-004: flag
forwarding**
``--offset`` **shall** combine with all the other
``compare-latest`` flags
(``--diff``, ``--diff-lines``, ``--html``,
``--word-diff``, ``--side-by-side``,
``--quick-jump``).
*Verification:* CLI test sets ``--offset 1
--html`` and asserts the offset pair appears in
the HTML output.

### 3.45 Watch --summary-retention (v0.51.0)

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-001: flag**
The ``watch`` subcommand **shall** accept an
optional ``--summary-retention DAYS`` flag.
When set, after writing the new summary file,
older files in the same directory matching the
``--summary-file`` template (with
``{timestamp}`` substituted as ``*``) **shall**
be deleted if their mtime is older than DAYS
days.
*Verification:* CLI test pre-creates two old
matching files plus one non-matching old file,
runs watch with ``--summary-retention 30``, and
asserts the matching files are gone while the
non-matching one is kept.

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-002:
template requirement**
``--summary-retention`` **shall** require
``--summary-file`` with the ``{timestamp}``
placeholder. Without the placeholder, the CLI
**shall** raise ``click.BadParameter``.
*Verification:* CLI test passes a literal path
+ retention and asserts the failure.

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-003:
range validation**
``--summary-retention`` **shall** require a
value in [1, 365]. Out-of-range values **shall**
raise ``click.BadParameter``.
*Verification:* CLI test asserts ``--summary-
retention 0`` fails.

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-004: keeps
just-written file**
The currently-just-written summary file
**shall not** be pruned regardless of its
mtime.
*Verification:* CLI test runs with
``--summary-retention 1`` and asserts the
new file is present after exit.

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-005:
strict ownership**
Pruning **shall** only remove files matching
the same filename glob as the template (with
``{timestamp}`` substituted as ``*``). Files
in the same directory with different filenames
**shall not** be touched.
*Verification:* CLI test pre-creates an old
file matching a different glob and asserts it
survives.

**REQ-RG-COMPAT-WATCH-SUMMARY-RETAIN-006:
announcement**
When at least one file is pruned, the watch
exit output **shall** include a line of the
form ``summary retention: pruned N file(s)
older than D day(s)``.
*Verification:* CLI test asserts the line
appears.

### 3.46 Digest --top N (v0.51.0)

**REQ-RG-COMPAT-DIGEST-TOP-001: flag**
The ``digest`` subcommand **shall** accept an
optional ``--top N`` flag. When set, the
per-row listing in both terminal and JSON
output **shall** be limited to the first N
reports (after sorting and filtering).
*Verification:* CLI test asserts the listing
contains exactly N rows.

**REQ-RG-COMPAT-DIGEST-TOP-002: aggregate
unaffected**
The aggregate header (in both terminal and
JSON output) **shall** report totals over the
unsliced window.
*Verification:* CLI test seeds 3 reports with
varied properties, runs ``--top 1``, and
asserts the aggregate report_count is 3.

**REQ-RG-COMPAT-DIGEST-TOP-003: range
validation**
``--top`` **shall** require a value in
[1, 1000]. Out-of-range values **shall**
raise ``click.BadParameter``.
*Verification:* CLI tests assert ``--top 0``
and ``--top 1001`` both fail.

**REQ-RG-COMPAT-DIGEST-TOP-004: terminal
slice indicator**
When the slice is in effect (i.e. the slice
is shorter than the full window), the
terminal output **shall** include a line of
the form ``showing top {len} of {full}
(--top N, sorted by {field} {direction})``.
*Verification:* CLI test asserts the
indicator line appears.

**REQ-RG-COMPAT-DIGEST-TOP-005: JSON
aggregate fields**
The JSON aggregate object **shall** include a
``top_n`` field carrying the integer cap (or
null when unset) and a ``top_n_emitted`` field
carrying the number of report objects actually
emitted (or null when unset).
*Verification:* CLI tests assert both fields
in both modes.

---

## 4. Framing Rules (Carry-forward)

The base-spec framing constraints (REQ-RG-FRAME-001 through
REQ-RG-FRAME-004) apply unchanged to every new section and
warning:

- **REQ-RG-FRAME-001 carry-forward.** The cross-venue section
  text, the dominance warning string, and the Brier-status
  labels all run through the existing imperative-language
  linter. Forbidden phrases like "you should buy" or "go long"
  do not appear; the renderer emits descriptive text only.
- **REQ-RG-FRAME-002 carry-forward.** The cross-venue section
  describes both venues' prices at equal prominence; no venue
  is rendered as "correct" or "the answer". The disagreement
  itself is the information.
- **REQ-RG-FRAME-003 carry-forward.** Sizing analyses embedded
  in the surfaced section continue to include the position-engine
  disclaimer block. The new `single_venue_dominance` warning
  appears alongside other warnings inside the surfaced block,
  not in the sizing block.
- **REQ-RG-FRAME-004 carry-forward.** The Brier `miscalibrated`
  label is qualified with "weight outputs less"; the renderer
  does not state any sector is "wrong" or "broken".

---

## 5. Operator Action Cues

The supplement adds four pieces of information the operator can
use without instructing the operator on what to do:

| Section | Cue | What it tells the operator |
| - | - | - |
| `cross_venue` items | spread_bps, per-venue prices | The two venues disagree by N bps on the same question. |
| `cross_venue` items | consensus_market_p | The volume-weighted average price across venues. |
| `surfaced.warnings` | `single_venue_dominance` | The cross-venue spread is real but the smaller-volume side may not be informative. |
| `calibration.sector_brier_scores` | `miscalibrated` flag | Outputs in this sector should be weighted lower in the operator's own thinking. |

The operator's response to any of these cues is not the system's
to direct.

---

## 6. Open Questions and Forward Work

### 6.1 Open questions

- **OQ-RG-COMPAT-001:** Should the cross-venue threshold be
  per-sector? Some sectors (regulatory, public health) routinely
  show wider spreads than others (macroeconomic). Default
  disposition: keep one global threshold for v1.x; revisit
  after the first month of measurements (T-RG-082 follow-on).

- **OQ-RG-COMPAT-002:** Should single-venue-dominance trigger
  on absolute volume rather than relative share? A class with
  $50/$10 volume splits 83% / 17% but neither side is
  meaningful. Default disposition: keep relative share for v1.x;
  add an absolute-volume floor in v1.2 if measurements show
  noise.

- **OQ-RG-COMPAT-003:** Should the Brier window be variable per
  sector (e.g., shorter for high-cadence sectors like
  macroeconomic)? Default disposition: 90 days global for v1.x;
  revisit if some sectors swamp the report with stale data.

- **OQ-RG-COMPAT-004:** Should we add a per-sector
  reliability-diagram section? More detail than the Brier number
  alone. Default disposition: defer to v1.2.

### 6.2 Deferred items

- **DEFER-RG-COMPAT-001:** Move all four module-level constants
  (`DEFAULT_SPREAD_THRESHOLD_BPS`, the `0.80` dominance
  threshold, `DEFAULT_BRIER_WINDOW_DAYS`,
  `DEFAULT_MISCALIBRATION_THRESHOLD`) into
  `config/report.yaml`. **RESOLVED v0.39.0** — landed under
  `thresholds:` block; defaults match v0.38.0 module
  constants; out-of-range values fall back with a logged
  warning.

- **DEFER-RG-COMPAT-002:** Per-sector overrides for spread
  threshold, dominance threshold, Brier threshold. Targeted
  for v1.2; depends on operator-driven measurements first.
  **RESOLVED v0.39.0** — landed as `<knob>_per_sector` keys
  alongside each global knob; sectors without an override
  use the global value via lookup helpers on
  `ReportThresholds`.

- **DEFER-RG-COMPAT-003:** A separate `reliability` section
  rendering per-sector reliability diagrams (calibration plot
  bins). Targeted for v1.2 if/when enough resolutions
  accumulate per sector. **RESOLVED v0.39.0** — new
  `reliability` section sits between `calibration` and
  `watchlist` in the body order. Opt-in via
  `enabled_sections` since v1 sectors typically lack enough
  resolutions to populate every bin. Default 10 equal-width
  bins, 90-day window, sparse-bin floor of 5 resolutions.

---

## 7. Acceptance Criteria

The supplement is considered shipped when:

- All four passes' tests pass (14 + 10 + 13 + 9 = 46 tests added).
- mypy strict clean across the new module and the extended
  modules.
- ruff lint + format clean.
- The `cross_venue` section is wired into the generator dispatch,
  the renderer, and the empty-message catalog for both the
  terminal and markdown renderers.
- The `enabled_sections` config recognizes `cross_venue` as a
  valid section name.
- All four framing-rule carry-forwards (REQ-RG-FRAME-001 through
  REQ-RG-FRAME-004) hold against adversarial test inputs to the
  new code paths.
- No imperative-language linter regressions across the existing
  v1.0 acceptance suite.

All of the above were satisfied at the LOOM v0.38.0 close (1905
tests pass; 239 source files mypy-strict-clean; 408 files
ruff-clean).

---

## 8. References

- LOOM v0.38.0 — `razorrooster.md`, evolution-log entry
  "Multi-venue calibration supplement — compatible Passes 1–4
  landed".
- `specs/REPORT_GENERATOR.md` — base requirements (v0.1.0).
- `specs/REPORT_GENERATOR_DESIGN.md` — base design (v0.1.0).
- `specs/REPORT_GENERATOR_TASKS.md` — task tracking; T-RG-082
  measurement guidance now records cross-venue threshold +
  single-venue dominance threshold + Brier window/threshold
  follow-up data.
- `docs/reports.md` — engine-internals reference; updated for
  v0.38.0.
- `docs/user_guide.md` — operator-facing CLI reference; §11 +
  §13 updated for v0.38.0.
