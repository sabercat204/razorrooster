# Reports

The Crow — operator-facing report renderer. v1 implementation per
`specs/REPORT_GENERATOR.md` (Requirements v0.1.0) and
`specs/REPORT_GENERATOR_DESIGN.md` (Design v0.1.0).

## Purpose

The reports CLI assembles outputs from every analytical subsystem
(`data_ingest`, `polymarket_connector`, `pattern_library`,
`signal_scanner`, `mispricing_detector`, `position_engine`,
`monitor`) into a single structured document the operator reads on
each cycle. The document is decision-support analysis. It does not
trade, recommend, or forecast outcomes with certainty. It is a
structured second opinion the operator reads alongside their own
thinking.

## Generation pipeline

```bash
razor-rooster report generate
```

The pipeline:

1. **Resolve cycle window** — `[since, until]`. Default `since`
   is the prior report's `generated_at`; first run defaults to
   `now - 24h`. `until` is wall-clock now.
2. **Load disclaimer text** and compute its SHA-256 hash so the
   `report_log` entry can detect drift across reports.
3. **Assemble header + footer** — the header records cycle date,
   library version, stale-source count, disabled-section note.
4. **Assemble body sections** in the order:
   `system_health → surfaced → cross_venue → watched → calibration → reliability → watchlist`.
   Each section is wrapped in per-section try/except; failures
   render as `section error: <reason>` placeholders. ``reliability``
   is opt-in via ``enabled_sections`` (DEFER-RG-COMPAT-003).
5. **Render terminal text** via `renderer/terminal.py` with ASCII
   section dividers.
6. **Apply imperative-language linter** to terminal text. If the
   linter raises, the report is not persisted (next run
   re-attempts). The linter is shared with the position engine —
   same `config/forbidden_phrases.yaml`, same `check_text()`.
7. **Optionally render markdown** via `renderer/markdown.py` (GFM
   with `##`/`###` headers, code blocks, tables) and run the
   linter on it too. Write to disk.
8. **Optionally print** terminal text unless `--quiet`.
9. **Persist** to `report_log` with full rendered text(s),
   metadata, and the disclaimer hash.

## Section structure

### Header

Cycle date, report ID, library version, source freshness summary
(`stale_source_count`), library refresh age, prior-report `since`
timestamp, and a note listing disabled sections.

### System health

Driven by the `data_ingest` `freshness` view and the upstream
subsystem cycle tables. Surfaces:

- **Stale sources** with their days-stale and last-successful-fetch
  timestamps.
- **Errored subsystems** since the prior report (per-cycle error
  count for each of the seven analytical subsystems).
- **Suppressed-comparison breakdown** — count of unsurfaced
  comparisons grouped by `suppression_reasons` so the operator can
  see whether sub-edge, low-mapping-confidence, or stale-market
  conditions dominated this cycle.

### Surfaced comparisons

Every `mispricing_detector` comparison with `surfaced = TRUE` since
the prior report, ordered by `confidence_weighted_score`
descending. For each, the section embeds:

- Class title + sector.
- Model probability + CI, market probability + spread, delta, EV.
- Warnings block (low_signature_confidence, source_stale_warning,
  library_stale_warning, low_mapping_confidence, low_liquidity,
  stale_market_price, no_market_price, **single_venue_dominance**).
- **Equal-prominence case sections** — `case_for_model` and
  `case_for_market` rendered as adjacent bullet blocks with
  identical headers, identical bullet prefixes, and the shorter
  side padded with `(no specific items identified)` so neither
  side gets visual prominence (REQ-RG-FRAME-002).
- Ambiguity factors.
- Position-engine analysis — when the position engine has run on
  this comparison, the rendered analysis (already including its own
  disclaimer block per REQ-PE-FRAME-001) is embedded verbatim. If
  no rendered text is available, a summary of suggested fraction,
  dollar size, EV, and clamps is rendered instead.

The `single_venue_dominance` warning fires when this class is
mapped on more than one venue and one venue holds strictly greater
than 80% of the combined 24h volume across them. It says: the
cross-venue spread you'll see in the next section is real, but the
smaller-venue side may be too thin to be informative. The check is
implemented in `surfaced.py::_compute_venue_volume_shares` and
`_has_single_venue_dominance`. The 80% threshold is the value
spec'd in supplement §2 and is currently a hard-coded module
default; a config-file override is planned for v1.2.

### Cross-venue disagreements

New in v0.38.0. Sits between `surfaced` and `watched`. Reads
recently-computed `comparisons` rows from the cycle window, dedups
to the most-recent comparison per (class_id, venue) pair, and
emits one item per class whose venue prices spread by at least
`spread_threshold_bps` basis points (default 500 — five
percentage points). Items ordered by spread_bps descending.

For each item, the assembler emits:

- Class title + sector.
- Per-venue breakdown: `comparison_id`, `condition_id`, market
  probability, 24h volume, market spread, snapshot timestamp,
  model probability + CI.
- `spread_bps` — gap between max and min market-implied
  probabilities across the venues.
- `consensus_market_p` — liquidity-weighted mean of the venue
  market probabilities, weighted by 24h volume. When all venues
  have NULL or zero volume, falls back to an unweighted mean and
  the renderer flags this with "no per-venue volume available".
  Implementation: `cross_venue.py::_liquidity_weighted_consensus`.
- `total_volume_24h` — sum of per-venue 24h volumes (NULL when
  the consensus fell back to unweighted).

Rendering:

- **Terminal**: per-class block with the consensus line, per-venue
  rows, and the model anchor.
- **Markdown**: a summary table with columns Class | Sector |
  Spread (bps) | Consensus | Venue prices, plus per-class detail
  blocks underneath the table.

Empty-message: `_No cross-venue disagreements this cycle._` when no
class meets the threshold. Disabled when no class is mapped on more
than one venue.

The framing rule (REQ-RG-FRAME-002 carry-forward): the section
*describes* the disagreement and the volume distribution; it does
not direct the operator to act on either venue's price. The
disclaimer block in the footer does the rest.

### Active watched

Every `monitor` follow-up with `recommended_review = TRUE` since
the prior report, ranked by alert tier:

1. `resolution`
2. `invalidation_triggered`
3. `material_shift`
4. `precursor_shift`
5. `time_decay`

For each, the section embeds the analysis-time-vs-current
probabilities, days since analysis, days to resolution, and the
follow-up's reasoning text.

### Calibration log

Every `comparison_resolutions` row created since the prior report.
The verdict text is template-driven per `(predicted_band, outcome)`
pairs (OQ-RG-001 resolution):

| Predicted band | Outcome | Verdict |
| - | - | - |
| high (≥0.7) | yes | "Model said {p} → resolved YES; in line with predicted likelihood." |
| high (≥0.7) | no | "Model said {p} → resolved NO; this counts against the model's calibration." |
| mid (0.3–0.7) | yes | "Model said {p} → resolved YES; consistent with mid-confidence prediction." |
| mid (0.3–0.7) | no | "Model said {p} → resolved NO; consistent with mid-confidence prediction." |
| low (<0.3) | yes | "Model said {p} → resolved YES; the model assigned low probability, but tail outcomes happen." |
| low (<0.3) | no | "Model said {p} → resolved NO; in line with predicted likelihood." |
| any | invalid | "Model said {p}; market was invalidated, outcome is undefined for calibration." |

The catalog lives at
`src/razor_rooster/report_generator/templates/calibration_verdicts.yaml`
and is operator-extensible.

In markdown mode the calibration log renders as a GFM table:

```markdown
| Class | Venue | Outcome | Predicted p | Days to Resolution | Verdict |
|-------|-------|---------|-------------|---------------------|---------|
```

#### Per-sector Brier scores

New in v0.38.0. The calibration section also computes per-sector
Brier scores over a rolling 90-day window:

- **Window**: `until_ts - timedelta(days=brier_window_days)` where
  `brier_window_days` defaults to 90 (`DEFAULT_BRIER_WINDOW_DAYS`
  in `calibration.py`).
- **Aggregation**: groups all `comparison_resolutions` whose
  `resolution_ts` falls in the window by
  `pl_event_classes.domain_sector`. Invalidated resolutions
  (`resolution_outcome = 'invalid'`) are excluded.
- **Formula**: per-resolution squared error
  `(model_probability - outcome_observed) ** 2`, averaged within
  the sector. Result rounded to four decimals.
- **Miscalibration flag**: `miscalibrated = True` when the rolling
  Brier exceeds `miscalibration_threshold` (default 0.25 per
  supplement §3 — the random-guesser baseline at p=0.5). Sectors
  with a miscalibration flag are labeled `miscalibrated (weight
  outputs less)` in the markdown status column; the terminal
  renderer surfaces the flag inline.
- **Order**: sectors sorted alphabetically. Sectors with zero
  scoreable resolutions in the window are omitted entirely.

The calibration section now renders even when the report's
`since`-`until` window contains no fresh resolutions, as long as
the per-sector Brier table has data from the rolling 90-day
window. This keeps the operator's view of long-running calibration
visible on quiet days.

In markdown the per-sector table follows the per-resolution table:

```markdown
**Per-sector Brier scores (rolling window):**

| Sector | Brier | n | Window | Status |
|--------|-------|---|--------|--------|
```

### Reliability diagram

New in v0.39.0. **Opt-in** — not in the workspace
``config/report.yaml`` ``enabled_sections`` by default. Sits
between ``calibration`` and ``watchlist`` when enabled. Renders
per-sector calibration bins so the operator can see, for any
sector, how the model's predicted probabilities map onto observed
hit rates.

Module:
``src/razor_rooster/report_generator/engines/section_assemblers/reliability.py``

Behavior:

- Reads ``comparison_resolutions`` over a rolling window
  (``thresholds.brier_window_days``; defaults to 90).
- Per-sector window narrowing applies via
  ``thresholds.brier_window_days_per_sector`` — the same per-sector
  window used by the Brier score.
- Bins model probabilities into ``thresholds.reliability_bin_count``
  equal-width bins (default 10) covering [0.0, 1.0]. The top bin is
  fully closed so a probability of exactly 1.0 lands in it.
- v0.40.0: per-sector overrides. Two new threshold knobs
  ``reliability_bin_count_per_sector`` and
  ``reliability_min_resolutions_per_bin_per_sector`` let operators
  give finer bins to sectors with many resolutions per window and
  broader bins to sectors with few. Per-sector entries echo the
  applicable values back via ``bin_count`` and
  ``min_resolutions_per_bin`` so renderers know which values
  actually applied.
- Per-bin: ``n``, ``mean_predicted``, ``empirical_rate``,
  ``calibration_gap = empirical_rate - mean_predicted``.
- Bins with at least one observation but fewer than
  ``thresholds.reliability_min_resolutions_per_bin`` (default 5) get
  a ``sparse: True`` flag.
- Bins with zero observations get ``n=0`` and ``None`` for the
  three numeric fields.
- Excludes invalidated resolutions
  (``resolution_outcome = 'invalid'``).
- Sectors sorted alphabetically. Sectors with zero observations
  across all bins are omitted entirely.

Reading the diagram:

- **Positive ``calibration_gap``**: the model was *under-confident*
  in this bin — events happened more often than predicted. Below
  about 0.3 this is the more common direction; the model is being
  cautious.
- **Negative ``calibration_gap``**: the model was *over-confident*
  in this bin — events happened less often than predicted. The
  more interesting direction; suggests the model is too sure of
  itself in that band.
- **Sparse bins**: numbers are noisy. Treat them as visual cues,
  not signal.

Renderers:

- Terminal: per-sector ASCII table with mean predicted, empirical,
  gap, and sparse indicator. v0.40.0 adds an ASCII
  calibration-curve overlay below the table showing the
  perfect-calibration diagonal and per-bin observations as a
  visual aid (`render_chart` from
  `report_generator/renderer/calibration_chart.py`).
- Markdown: per-sector GFM table with the same columns plus a
  Notes column flagging sparse and empty bins, plus the same
  calibration chart wrapped in a fenced code block to preserve
  monospace alignment.

Empty state: ``_No reliability bins populated this cycle._``
when no sector has any observations in the rolling window.

Framing rule (REQ-RG-FRAME-002 carry-forward): the section
*describes* per-bin calibration; it does not direct the operator to
trust or distrust any specific class output. The disclaimer block
in the footer does the rest.

### Threshold-distribution measurements

New in v0.40.0. Each cycle records a snapshot of the cross-venue
spread distribution into a new ``report_threshold_measurements``
table (m7002 migration, primary key
``(report_id, measurement_kind)``). Operators inspect the
historical distribution via the
``razor-rooster report measurements`` CLI subcommand to decide
whether to re-tune the configured thresholds for their corpus.

v0.41.0 adds two more measurement kinds. The generator now
records all three on every cycle:

- ``cross_venue_spread_bps`` — gap (in bps) between max and min
  market-implied probabilities across the venues mapped to a
  class. Source: cross_venue section's ``items[*].spread_bps``.
- ``single_venue_dominance_share`` — for each multi-venue class,
  the maximum venue's share of combined 24h volume. Source:
  surfaced section's ``comparisons[*].venue_shares``. Single-
  venue classes contribute nothing.
- ``brier_per_sector`` — per-sector rolling Brier score. Source:
  calibration section's ``sector_brier_scores``.

Module: ``src/razor_rooster/report_generator/engines/measurements.py``

Behavior:

- ``compute_distribution(values, *, threshold)`` returns
  ``n``, ``n_above_threshold`` (strict ``>``),
  ``configured_threshold``, ``min``, ``max``, ``mean``,
  ``stddev``, and a ``percentiles`` mapping over the default cut
  set ``(0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)``. Empty
  input emits all-None numeric fields and ``n=0``.
- Three extractor helpers — ``cross_venue_spread_observations``,
  ``single_venue_dominance_observations``,
  ``brier_per_sector_observations`` — pull the relevant series
  out of each section's content dict so the generator doesn't
  re-run any SQL.
- ``threshold_percentile_rank(distribution, *, threshold=None)``
  reports where a configured threshold sits in a recorded
  distribution's percentile cuts (used by the
  ``explain-thresholds`` CLI).
- The generator's ``_persist_threshold_measurements`` hook
  records all three kinds per cycle. Wrapped in try/except so
  a measurement bug never breaks report generation.

Persisted columns:

- ``report_id`` — joins back to ``report_log`` 1:1.
- ``measurement_kind`` — bounded enum;
  ``SHIPPED_MEASUREMENT_KINDS`` lists every kind shipped by the
  project.
- ``measured_at`` — TIMESTAMPTZ.
- ``n_observations``, ``n_above_threshold``,
  ``configured_threshold`` — flat columns for cheap querying.
- ``distribution_json`` — full payload from
  ``compute_distribution`` so future percentile or stat
  additions don't require a schema change.

CLI surface (v0.40.0 + v0.41.0 + v0.42.0 + v0.43.0 + v0.44.0 + v0.45.0):

- ``razor-rooster report measurements [--kind ...] [--since ISO]
  [--limit N] [--json]`` — historical distribution view.
- ``razor-rooster report explain-thresholds [--kind ...]
  [--db PATH]`` — for the latest cycle per kind, prints where the
  configured threshold sits in the distribution's percentile cuts.
- ``razor-rooster report suggest-thresholds [--kind ...]
  [--lookback-cycles N] [--target-pct 0.70 [...]] [--db PATH]
  [--apply [--yes] [--diff] [--config PATH] [--note TEXT]]``
  — averages the most recent ``lookback_cycles`` measurements'
  percentile cuts across cycles with data, and emits one
  suggested threshold per target percentile.
- ``razor-rooster report prune-measurements [--before ISO]
  [--keep-last N] [--kind KIND] [--confirm]`` — delete
  ``report_threshold_measurements`` rows.
- ``razor-rooster report tuning-log [--kind ...] [--since ISO]
  [--limit N] [--db PATH]`` — list tuning-log entries.
- ``razor-rooster report tuning-log-undo <log_id> [--yes]
  [--config PATH]`` — restore config from a tuning-log entry's
  recorded backup.
- ``razor-rooster report compare <report_id_a> <report_id_b>
  [--diff/--no-diff] [--diff-lines N]`` — diff two reports by ID.
  New in v0.45.0.
- ``razor-rooster report watch [--interval SEC] [--html PATH]
  [--markdown PATH] [--once] [--max-cycles N]`` — run
  ``report generate`` on a fixed cadence in a loop. New in
  v0.45.0.

Body sections (v0.40.0 → v0.45.0): the body has nine slots
arranged in fixed top-to-bottom order; the operator-tunable
``enabled_sections`` list controls which actually render.

```
at_a_glance       (opt-in, v0.45.0)
system_health
recent_tuning     (opt-in, v0.44.0)
surfaced
cross_venue
watched
calibration
reliability       (opt-in, v0.39.0)
watchlist
```

The four opt-in sections (at_a_glance, recent_tuning,
reliability) default to disabled in the workspace config. Add
them to ``enabled_sections`` to render. Disabling a default-on
section produces a one-line note in the header.

Configuration (v0.43.0): ``config/report.yaml`` gained an
optional ``auto_prune:`` block. v0.45.0 added nine new
editorial-flavor phrases to ``config/forbidden_phrases.yaml``
to defend against drift in the at-a-glance section.

Output formats: terminal (default), markdown (``--markdown``),
HTML (``--html``, new in v0.44.0). All three pass through the
shared imperative-language linter before persistence.

### Watchlist

Lists `signal_scanner` candidates that did not surface in
`mispricing_detector` this cycle for one of three reasons documented
in REQ-RG-SEC-006:

- `no_active_mapping` — no `class_market_mappings` row for the class
  (or all mappings are soft-deleted).
- `all_low_confidence` — comparisons exist but every one is flagged
  `low_mapping_confidence`.
- `all_stale_market_price` — comparisons exist but every one is
  flagged `stale_market_price`.

For each candidate, a suggested action is rendered (e.g.,
"Consider mapping this class to a Polymarket market"). Suggestions
are framed as suggestions, never directives.

### Footer

Verbatim disclaimer block (loaded from `templates/disclaimer.txt`),
system version stamp, report ID, completion timestamp. The markdown
renderer wraps the disclaimer in a blockquote so it stays visually
distinct.

## Framing constraints

The renderer enforces four framing constraints:

1. **REQ-RG-FRAME-001: imperative-language linter.** Both terminal
   and markdown outputs run through `check_text()` before
   persistence. Forbidden phrases (e.g., "i recommend", "go
   long", "guaranteed to") trigger
   `ImperativeLanguageDetected` and the report is not persisted.
2. **REQ-RG-FRAME-002: equal-prominence "case for market".**
   Implemented via the `equal_prominence_blocks` helper (terminal)
   and `_balanced_cases_md` helper (markdown). Both pad the shorter
   list with explicit "no specific items" entries so the layouts
   are visually balanced.
3. **REQ-RG-FRAME-003: sizing-disclaimer block presence.** When a
   surfaced comparison has a position-engine analysis, the
   embedded `rendered_text` already contains the position-engine
   disclaimer block (the position-engine renderer asserts it
   appears verbatim per REQ-PE-FRAME-001). If a fallback summary
   is rendered without it, the renderer's tests guarantee
   downstream operators see a clear `_(below min-edge threshold;
   sizing not surfaced)_` note instead.
4. **REQ-RG-FRAME-004: no certainty claims.** "Definitely",
   "guaranteed to", and similar phrases are in the shared
   `forbidden_phrases.yaml` catalog and trigger the linter.

## Configuration

`config/report.yaml`:

- `enabled_sections` — body-section toggle. Disabling a section
  omits it from the body and adds a one-line note to the header.
  Includes `cross_venue` (new in v0.38.0); set `enabled_sections`
  without it on installations that map to a single venue only.
  Includes `reliability` (new in v0.39.0) but **opt-in by
  default** — operators with enough resolutions per sector add it
  to their list.
- `verbosity.watchlist` — `full` includes the posterior + base
  rate + log-odds shift line per candidate; `compact` omits it.
- `verbosity.calibration` — reserved for future tuning; v1 always
  shows verdicts.
- `calibration_first_run_lookback_days` — `since` window when no
  prior report exists (default 30). Currently the resolver uses
  `now - 24h` for first-run; the 30-day knob will be wired in if
  T-RG-082 measurement shows it useful.

The multi-venue and reliability defaults live under
``config/report.yaml.thresholds:`` (DEFER-RG-COMPAT-001..003
resolved in v0.39.0):

- ``cross_venue_spread_bps`` (default 500). Per-sector overrides
  in ``cross_venue_spread_bps_per_sector``.
- ``single_venue_dominance_pct`` (default 0.80, strict ``>``).
  Per-sector overrides in ``single_venue_dominance_pct_per_sector``.
- ``brier_window_days`` (default 90). Per-sector overrides in
  ``brier_window_days_per_sector`` (also used by the reliability
  section).
- ``brier_miscalibration`` (default 0.25). Per-sector overrides in
  ``brier_miscalibration_per_sector``.
- ``reliability_bin_count`` (default 10; range [2, 50]).
  Per-sector overrides via
  ``reliability_bin_count_per_sector`` (v0.40.0).
- ``reliability_min_resolutions_per_bin`` (default 5; range
  [1, 1000]). Per-sector overrides via
  ``reliability_min_resolutions_per_bin_per_sector`` (v0.40.0).

Out-of-range or non-coercible values fall back to the default with
a warning logged. Per-sector overrides for sectors not listed use
the global value transparently via lookup helpers on
``ReportThresholds``.

`config/forbidden_phrases.yaml` — shared with position engine.
Operator-extensible.

## Tables

- `report_log` — one row per generated report. Columns:
  `report_id`, `generated_at`, `since_ts`, `until_ts`,
  `sections_enabled`, `sections_rendered`, `sections_failed`,
  `library_version`, `disclaimer_version_hash`,
  `rendered_terminal_text`, `rendered_markdown_text`,
  `markdown_path`, `duration_seconds`. Indexed on
  `(generated_at DESC)` for "latest report" queries.

Schema-migration version space: 7001+.

## CLI summary

```bash
razor-rooster report generate                         # daily cadence
razor-rooster report generate --markdown <path>       # also write markdown
razor-rooster report generate --html <path>           # also write HTML
razor-rooster report generate --since <iso>           # explicit window start
razor-rooster report generate --quiet                 # skip terminal output
razor-rooster report list                             # most recent 20
razor-rooster report list --since 2026-05-01 --limit 5
razor-rooster report show <report_id>                 # rendered terminal text
razor-rooster report latest                           # most recent
razor-rooster report compare <a> <b>                  # diff two reports
razor-rooster report compare <a> <b> --html <path>    # two-column HTML view
razor-rooster report compare-latest                   # diff the two most recent reports
razor-rooster report compare-latest --html <path>     # ...with HTML output
razor-rooster report compare-latest --offset 1        # step backward to the prior pair
razor-rooster report watch                            # rerun on a fixed cadence
razor-rooster report watch --on-change                # skip cycles when nothing changed upstream
razor-rooster report watch --summary-file <path>      # write exit summary to a file
razor-rooster report watch --summary-file 'logs/watch-{timestamp}.json' --summary-retention 30
razor-rooster report digest                           # one-line-per-report list, default 7-day window
razor-rooster report digest --days 30
razor-rooster report digest --sort-by sections_failed # surface most-failed cycles first
razor-rooster report digest --sort-by terminal_chars --top 5
razor-rooster report version                          # schema namespace
```

### `report watch --on-change`

Skips the per-cycle `generate()` call when upstream tables
haven't changed since the prior cycle. The fingerprint covers
the latest IDs in `scan_summaries`, `comparisons`,
`follow_ups`, and `threshold_tuning_log`. The first cycle
always runs (it seeds the baseline). Skipped cycles count
toward `--max-cycles` for deterministic test harnesses; the
exit summary includes the skip count when nonzero.

When the loop transitions skip→run after one or more skipped
cycles (added v0.47.0), the next non-skipped cycle's log line
includes a parenthesized note like `(resume after 3 skipped:
tuning_log changed)` naming which fingerprint field(s)
drove the resume.

When the watch loop exits (Ctrl+C / `--max-cycles` / `--once`),
the summary block (extended v0.48.0) includes:
- average cycle duration (with millisecond precision)
- count of failed cycles
- distinct fingerprint fields encountered as changed across
  the loop (only when at least one change occurred)
- total skip time (only when at least one cycle was skipped)

Pass `--summary-file PATH` (added v0.49.0) to also write the
summary to disk. Suffix-driven dispatch: paths ending in
`.json` get a single `{"kind": "watch_summary", ...}` JSON
object; other paths get plain text matching the stdout
format. Useful for cron-driven watch invocations where the
operator wants to harvest the summary without parsing log
files.

The summary path may include a `{timestamp}` placeholder
(added v0.50.0) that expands to a UTC ISO 8601 timestamp
(filesystem-safe — colons replaced with hyphens) so
successive cron invocations produce discrete files instead
of overwriting one another. The CLI prints
`summary written to: <resolved>` when the path was rewritten.

Pass `--summary-retention DAYS` (added v0.51.0) to delete
older summaries from the same directory after writing the
new file. Range `[1, 365]`. Requires `--summary-file` with
the `{timestamp}` placeholder. Pruning is strict on
filename pattern: only files whose names match the same
template glob (with `{timestamp}` substituted as `*`) are
candidates. The just-written file is always kept regardless
of mtime.

### `report compare --html PATH`

Renders the two-report comparison as a self-contained HTML
page with a two-column side-by-side terminal-text panel, a
metadata table that highlights changed fields, a sections
list with `added`/`removed` styling, and a fourth panel
(added v0.47.0) that renders the unified terminal-text diff
with line-level color highlighting. Inline CSS only; no
external assets, no JavaScript. The dark/light
`prefers-color-scheme` palette mirrors the daily-report HTML
renderer. The output passes the imperative-language linter
before being written to disk.

The header now includes a quick-jump nav (added v0.50.0) with
inline `href="#..."` anchor links to each panel: metadata,
sections, side-by-side (when present), unified diff. Each
section carries a stable `id="..."` attribute so URL
fragments deep-link to the matching panel. Pass
`--no-quick-jump` (added v0.51.0) to suppress the nav block;
the section ids stay in place so manual deep-linking still
works.

The unified-diff panel honors `--diff-lines N` — the same
flag that bounds the terminal preview also caps the HTML
panel. When the diff exceeds the cap, a "more line(s)
truncated" footer indicates the drop count. When the
terminal text is identical, the panel emits a benign "No
textual differences" message rather than an empty container.

The unified-diff panel applies word-level highlighting (added
v0.48.0) to paired deletion/addition runs of equal length:
unchanged tokens render as plain text inside the line; replaced
tokens get an inline `<span class="word-del">` (red tint with
strike-through) or `<span class="word-add">` (green tint), so
the operator sees exactly which substring within each touched
line changed. Unequal-length runs fall back to whole-line
styling. Pass `--no-word-diff` (added v0.49.0) to suppress
word-level highlighting for narrow viewports.

Pass `--no-side-by-side` (added v0.49.0) to suppress the
two-column terminal-text panel for a more compact page focused
on the structural diff. The two flags compose: `--no-side-by-side
--no-word-diff` produces the most compact view (metadata table,
sections list, and line-level unified diff only).

The side-by-side panel routes each report's terminal text
through an ANSI-to-HTML translator (added v0.47.0). Today's
terminal renderer doesn't emit ANSI escape sequences, so
the translator is currently a no-op. It activates if a
future renderer change emits ANSI or if external content
with ANSI is pasted in. Eight standard + eight bright
foreground colors plus bold / dim / italic / underline are
supported via inline `<span>` elements with semantic class
names. Background colors and 256-color/RGB sequences are
silently dropped to keep the surface small.

### `report digest [--days N | --since ISO] [--report-id PREFIX] [--json]`

Prints one line per report in newest-first order: the
generated_at timestamp, the report_id, the
sections-rendered/sections-enabled count, the failed-section
count, the terminal-text length, and bracketed
`[md]`/`[html]`/`[md, html]` markers when the underlying
ReportRecord persisted those output paths. Default window is
7 days; range `[1, 365]`. The window can also be specified as
`--since ISO 8601` (added v0.48.0); the two flags are mutually
exclusive. Strictly descriptive — it reports observed activity
over the window without ranking or recommending.

A small aggregate header sits above the per-row listing
(added v0.47.0): total report count, cycles with at least
one failed section, cycles with persisted markdown / HTML
output, average sections rendered per cycle, and average
terminal-text length per cycle.

`--json` (added v0.48.0) emits JSON Lines: one
`{"kind": "report", ...}` object per line in newest-first
order, followed by a single `{"kind": "aggregate", ...}`
line carrying the same totals. Each line parses standalone,
so `jq`, `head`, and other unix tooling work without
preprocessing.

`--report-id PREFIX` (added v0.49.0) further filters the
listing to reports whose `report_id` starts with `PREFIX`
(e.g. `--report-id rpt-2026-05` to scope to May 2026
cycles). Combines cleanly with `--days`/`--since`/`--json`.
The JSON aggregate object carries the prefix in its
`report_id_prefix` field (or `null` when the flag isn't set).

`--sort-by FIELD` (added v0.50.0) selects the ordering field
(`generated_at` / `sections_failed` / `terminal_chars`) and
`--sort-direction {asc,desc}` controls direction. Defaults
preserve the existing newest-first listing. Useful for
finding the longest reports (`--sort-by terminal_chars`) or
the most-failed cycles (`--sort-by sections_failed`).

`--top N` (added v0.51.0) caps the per-row listing to the
first N reports after sorting (range `[1, 1000]`). The
aggregate header still reports totals over the full unsliced
window so the operator's selection remains accurate. Pairs
naturally with `--sort-by` to surface only the top few
most-failed or longest cycles.

### `report compare-latest`

Convenience wrapper over `report compare`. Resolves the two
newest persisted reports' ids (newer is `b`, older is `a`)
and forwards the rendering flags to the compare path. Same
flag set as `compare`: `--diff/--no-diff`, `--diff-lines`,
`--html`, `--word-diff/--no-word-diff`,
`--side-by-side/--no-side-by-side`, `--quick-jump/--no-quick-jump`,
`--db`. Refuses with a clear message when fewer than two
reports are persisted.

`--offset N` (added v0.51.0) steps backward through history.
Offset 0 (default) diffs reports `[0]` and `[1]`; offset 1
diffs `[1]` and `[2]`; and so on. Refuses when fewer than
`N + 2` reports are persisted.

Useful when the operator wants to diff the last two cycles
without first running `report list` to find the ids.

## No-network guarantee

The renderer makes no network calls. `tests/report_generator/test_end_to_end_cycle.py::test_no_network_calls_during_generate`
patches `socket.socket` to raise on instantiation and runs a full
generate cycle; if any code path opened a socket the test would fail
(NFR-RG-LOCAL-001).

## Post-T-RG-082 measurement guidance

The first month of operator-driven cycles tells us whether the
defaults are right for the v1 corpus. T-RG-082 records:

- **Cycle duration** — should stay sub-minute for v1 scale per
  NFR-RG-PERF-001. The generator issues SQL only against tables
  the operator's hardware already has indexed; profile per
  assembler if it slips.
- **Report length** — v1 expects 1–3 pages of terminal output for
  a typical day. Reports with many active situations may be
  longer; we render in full per OQ-RG-003 resolution.
- **"Case for market" balance** — DEFER-RG-001. Visually inspect
  surfaced comparisons over a few cycles. If the case-for-market
  side consistently looks thinner, refine the upstream
  `case_for_market_from_context` factory (in
  `mispricing_detector/engines/trace.py`) or pad more aggressively
  in the report renderer.
- **Calibration verdict catalog** — DEFER-RG-002. As resolutions
  accumulate, refine the verdict text per band/outcome. The
  template file is operator-extensible.
- **Cross-venue threshold** (v0.38.0). Default 5 percentage points
  (500 bps). Track over the first month: is the section quiet on
  most days (good — disagreement is rare) or noisy (lower the
  threshold via the constant in `cross_venue.py`, or bump the
  default once enough data justifies it)?
- **Single-venue dominance threshold** (v0.38.0). Default 80% of
  combined 24h volume, strict `>`. If most cross-venue mappings
  trip the warning despite both venues looking healthy on the
  surface, the threshold may be too conservative; if the warning
  never fires when one venue is clearly thinner, it may be too
  permissive.
- **Per-sector Brier window + threshold** (v0.38.0). Default
  90-day window, 0.25 miscalibration threshold. With small-n
  sectors (1–3 resolutions per window), the score is noisy by
  construction — don't act on a single window's number for those
  sectors; let the sample build first. The threshold is set at
  the random-guesser Brier at p=0.5, so any sector above 0.25 is
  doing worse than coin-flip on the sector's resolutions in the
  window. Worth investigating before relying on outputs.
- **Section frequency** — does the watchlist explode in size? If
  many classes have no active mapping, the operator probably wants
  to either build mappings or accept that the section is
  noise-heavy. Switch the verbosity to `compact` if needed.

Threshold revisions and measurements are recorded in
`specs/REPORT_GENERATOR_TASKS.md` under T-RG-082.

## See also

- `specs/REPORT_GENERATOR.md` — Requirements v0.1.0
- `specs/REPORT_GENERATOR_DESIGN.md` — Design v0.1.0 (engine
  internals, threat model, test strategy)
- `specs/REPORT_GENERATOR_TASKS.md` — Task tracking and acceptance
  measurements
- `specs/REPORT_GENERATOR_SUPPLEMENT_MULTIVENUE.md` — Multi-venue
  calibration supplement (v0.38.0): cross-venue disagreement
  section, single-venue dominance warning, per-sector Brier,
  liquidity-weighted consensus.
- `razorrooster.md` — LOOM
