# Razor-Rooster v1 Mispricing Detector

Operator-facing reference for the model-vs-market comparison layer.
The `mispricing_detector` subsystem reads `signal_scanner` posteriors
and Polymarket prices, computes deltas with credible-interval-overlap
analysis, and emits structured comparisons that present the case for
both the model view and the market view at equal prominence.

For requirements, design, and task tracking, see
`specs/MISPRICING_DETECTOR.md`,
`specs/MISPRICING_DETECTOR_DESIGN.md`, and
`specs/MISPRICING_DETECTOR_TASKS.md`.

## Design principles

- **Default-to-market disposition.** When the model and market
  disagree, the trace presents possible reasons the market may be
  right with at least equal weight as possible reasons the model
  may be right (REQ-MD-TRACE-005). Treating the market as
  default-correct is one of the system's stated principles. The
  trace builder pads the shorter case section so the renderer can't
  accidentally favour one view.
- **No directives.** The subsystem describes disagreements; it
  never recommends action. Position sizing is `position_engine`'s
  job (deferred to its own subsystem); order placement is out of v1
  scope per OT-004.
- **Failure isolation.** A bad mapping or a missing market produces
  an error record but the cycle keeps going for the rest.
- **Versioned everything.** Every comparison carries the
  `pattern_library_version` (via the embedded scanner trace), the
  class `definition_version`, the mapping `polarity` and confidence,
  and the `data_as_of` timestamp.
- **Lazy resolution linkage.** Comparisons link to Polymarket
  resolutions only after the resolution lands. The linkage pass
  runs at the end of every cycle and is idempotent.

## The five surfacing gates

A comparison is `surfaced=True` only when all five gates pass:

1. **Magnitude.** `|log_odds_delta|` exceeds the per-sector
   threshold from `config/mispricing.yaml` (default 0.5).
2. **CI overlap.** The model's credible interval and the market's
   bid-ask spread do NOT overlap. Overlap means "model and market
   are not in material disagreement at current uncertainty levels."
3. **No critical warnings.** None of the following are set:
   `stale_market_price`, `no_market_price`, `low_signature_confidence`,
   `library_stale_warning`, `low_liquidity`.
4. **Mapping confidence.** `mapping_confidence` is `'exact'` or
   `'inferred'`, not `'low'`. Auto-derived mappings without a
   temporal qualifier or strong keyword overlap default to `'low'`.

Suppressed comparisons are still persisted; they just don't bubble
up to the operator's attention. The `suppression_reasons` JSON list
explains why each suppressed comparison was held back.

## Class-to-market mappings

A *mapping* is the link between one `pattern_library` event class
and one Polymarket market. Mappings have:

- `mapping_type` — `direct` (1:1), `proxy` (same domain, different
  question), or `aggregate` (composite of multiple markets).
- `mapping_confidence` — `exact` (operator-asserted), `inferred`
  (auto-derived sector match + 3+ keyword overlaps + temporal
  qualifier), or `low` (sector match alone).
- `polarity` — `aligned` (default; YES outcome means event happens)
  or `inverted` (YES means event does NOT happen). Operators must
  explicitly mark inverted mappings; auto-derivation never sets
  inverted polarity.

Operator-curated mappings always take precedence; auto-derived
mappings are computed fresh per cycle and not persisted.

### Adding a mapping

```bash
razor-rooster mispricing map pheic_declaration_12mo 0xPHEIC_CONDITION_ID \
    --type direct --notes "PHEIC declaration in 2026 Polymarket question"
```

For inverted markets ("Will X NOT happen?"):

```bash
razor-rooster mispricing map final_rule_within_12mo 0xRULE_NOT_PASSING_ID \
    --type direct --polarity inverted \
    --notes "Inverted: market YES means rule does NOT pass within 12mo"
```

### Removing a mapping

```bash
razor-rooster mispricing list-mappings --class pheic_declaration_12mo
razor-rooster mispricing unmap <mapping_id>
```

Removed mappings are soft-deleted (`removed_at` timestamp set) so
the audit trail is preserved. Auto-derivation respects the
tombstone — a (class, market) pair previously removed by the
operator is not auto-resurrected.

## The reasoning trace

Every comparison has an associated trace JSON with this stable
shape:

```json
{
  "class_id": "pheic_declaration_12mo",
  "condition_id": "0xPHEIC...",
  "polarity": "aligned",
  "mapping": { "type": "direct", "confidence": "exact", "mapped_by": "operator" },
  "model_probability": 0.18,
  "model_ci": [0.07, 0.34],
  "market_probability": 0.05,
  "market_best_bid": 0.04,
  "market_best_ask": 0.06,
  "market_volume_24h": 25000.0,
  "market_spread_bps": 200,
  "delta": 0.13,
  "log_odds_delta": 1.43,
  "ci_overlap": false,
  "expected_value": 0.13,
  "confidence_weighted_score": 1.18,
  "embedded_scanner_trace": { ... full scanner trace verbatim ... },
  "case_for_model": [
    "Precursor 'who_don_publication_frequency' fired with hit rate 0.65, FPR 0.20, applied LR 3.25.",
    ...
  ],
  "case_for_market": [
    "Market price reflects aggregate trader belief at 0.0500; participants have priced in information not necessarily captured by the model's precursors.",
    "24h volume of 25000 (liquidity floor 10000) suggests active price discovery from at least some informed participants.",
    "Bid-ask spread of 200 bps indicates the level of agreement among market makers on the current price.",
    ...
  ],
  "ambiguity_factors": [...],
  "warnings": [...],
  "suppression_reasons": [],
  "surfaced": true
}
```

Render the trace to text with `razor-rooster mispricing show
<comparison_id>`. Pass `--json` for the raw payload.

The renderer always emits `case_for_model` and `case_for_market` as
two adjacent equal-prominence blocks with identical headers and
identical formatting. The trace builder pads the shorter section
with explicit "(no specific items identified for the X side)"
entries so neither view is implicitly diminished.

## Daily workflow

```bash
# After data_ingest cycle, pattern_library refresh, and signal_scanner run:
razor-rooster mispricing run

# Review surfaced comparisons.
razor-rooster mispricing list-comparisons --surfaced-only --since 2026-05-01

# Read the full trace for a specific comparison.
razor-rooster mispricing show <comparison_id>
```

## Calibration scaffolding

When a Polymarket market resolves, the linkage pass writes a
`comparison_resolutions` row connecting any prior comparisons on
that market to the resolution outcome. This populates the data the
calibration backtest reads (OT-006).

The linkage pass runs at the end of every `mispricing run` cycle.
Operators can trigger it on demand:

```bash
razor-rooster mispricing relink
```

Linkage is idempotent — running it twice produces no duplicates.

Polarity-aware mapping: an inverted mapping with a `'no'` market
resolution produces `outcome_observed = 1` (the model-event-as-
defined did happen). Aligned mappings with `'yes'` produce the same.

## Storage layout

Six tables under the mispricing namespace live alongside the four
data_ingest canonical tables, the seven `polymarket_*` tables, the
eight `pl_*` tables, and the three `scan_*` tables in the same
DuckDB store at `data/trough.duckdb`:

- `class_market_mappings` — operator-curated mappings.
- `comparison_cycles` — one row per cycle execution.
- `comparisons` — one row per (cycle, mapping) pair.
- `comparison_traces` — full reasoning trace JSON.
- `comparison_resolutions` — calibration scaffolding.
- `mispricing_detector_state` — single-row KV (currently only
  `last_linkage_ts`).

Schema migration version namespacing: `mispricing_detector` uses
4001+, namespaced clear of `data_ingest` (1–999),
`polymarket_connector` (1001–1999), `pattern_library`
(2001–2999), and `signal_scanner` (3001–3999).

## Configuration knobs

`config/mispricing.yaml` controls:

- `surfacing_thresholds.log_odds_delta_min` — global default 0.5.
- `surfacing_thresholds.per_sector` — per-sector overrides.
- `market_price_freshness_seconds` — default 12h. Snapshots older
  trigger `stale_market_price`.
- `liquidity_floors.default` — default $10k 24h volume. Sectors
  with shallow Polymarket liquidity (regulatory, climate) have
  lower defaults.
- `auto_mapping.min_keyword_overlap_for_inferred` — default 3.
- `auto_mapping.require_temporal_qualifier_for_inferred` — default
  true.

The defaults are deliberately conservative. Operators tune them
after the first real-hardware cycle (T-MD-081) once the empirical
distribution of deltas, volumes, and mapping confidence levels is
known.

## Disk and performance

- v1 disk budget: **200 MB** out of the 100 GB global cap given
  v1 scale (≤8 seed classes × ≤5 mapped markets each = ≤40
  comparisons per cycle) and daily cadence over the first year
  (NFR-MD-DISK-001).
- Daily cycle target: **under 2 minutes** on EliteBook G8 hardware
  for the v1 scale (NFR-MD-PERF-001).
- Per-comparison: ~5 SQL queries plus in-process math, sub-second.

## After T-MD-081 measurements

The first real-hardware cycle against the live Polymarket corpus
produces the empirical distribution of comparison deltas, market
volumes, and mapping confidence levels. Update DEFER-MD-001 and
DEFER-MD-002 in `specs/MISPRICING_DETECTOR_TASKS.md` with measured
numbers:

- Per-sector empirical distribution of `|log_odds_delta|`.
- Per-sector market volume distribution to validate the liquidity
  floor defaults.
- Distribution of mapping confidence levels (`'exact'` vs
  `'inferred'` vs `'low'`).

If a sector's surfacing rate is anomalous, revise the per-sector
threshold in `config/mispricing.yaml` and document the revision in
the task tracking. Healthy surfacing rates are roughly 5–15% of
evaluated comparisons.

## See also

- `razorrooster.md` — LOOM (project state of truth)
- `specs/MISPRICING_DETECTOR.md`,
  `specs/MISPRICING_DETECTOR_DESIGN.md`,
  `specs/MISPRICING_DETECTOR_TASKS.md` — full requirements / design
  / tasks
- `docs/scanner.md` — the upstream scanner that produces the model
  posteriors
- `docs/pattern_library.md` — the upstream class registry
- `src/razor_rooster/mispricing_detector/engines/` — the
  comparator, surfacing, delta, ci_overlap, trace, and linkage
  modules
