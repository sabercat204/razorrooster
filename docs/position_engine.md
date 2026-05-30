# Razor-Rooster v1 Position Engine

Operator-facing reference for the paper-analysis sizing layer. The
`position_engine` subsystem reads `mispricing_detector` comparisons
and produces *sizing analyses* — structured documents with Kelly
fractions, half-Kelly bounds, expected-value figures, bankroll-survival
diagnostics, and invalidation criteria. The subsystem produces
analyses, not directives.

For requirements, design, and task tracking, see
`specs/POSITION_ENGINE.md`,
`specs/POSITION_ENGINE_DESIGN.md`, and
`specs/POSITION_ENGINE_TASKS.md`.

## Design principles

- **Paper-analysis only.** No order placement, no real-capital
  tracking, no wallet integration. The subsystem assumes the operator
  understands this and uses the bankroll figure as an analytical
  baseline rather than a tracked balance. Confirmed by OT-004.
- **Conditional language only.** Every output uses "if the operator
  chose to act" framing. The renderer linter refuses to ship any
  output containing forbidden imperative phrases (see
  `config/forbidden_phrases.yaml`).
- **Half-Kelly is the conservative ceiling.** The `kelly_fraction_default`
  config knob is bounded `[0, 0.5]`. Setting full Kelly is rejected
  with a clear error. Fuller Kelly variants are deliberately not
  exposed (OQ-PE-001 resolution).
- **Warnings before sizing math.** Confidence indicators (low signature
  confidence, source stale, library stale, definition drift,
  low mapping confidence) display at the top of every analysis,
  before any Kelly figures.
- **Standard disclaimer in every analysis.** The disclaimer block
  appears verbatim in every rendered output (REQ-PE-FRAME-001).
- **Failure isolation.** A bad comparison input produces an analysis
  with `error` set rather than crashing the cycle.
- **Watch state auto-expires on resolution.** When the underlying
  Polymarket market resolves, watch states (`'watching'`, `'acted_on'`)
  transition to `'expired'` automatically (OQ-PE-005 resolution).

## Bankroll configuration

Before running an analysis cycle, declare a bankroll configuration:

```bash
razor-rooster position-engine config --bankroll 1000
```

The CLI prints the analytical-bankroll disclaimer and prompts for
confirmation. For non-interactive use:

```bash
razor-rooster position-engine config \
    --bankroll 1000 \
    --max-pct 0.05 \
    --kelly-fraction 0.5 \
    --min-edge 0.03 \
    --no-prompt --acknowledge-analytical
```

Bounds:
- `--bankroll` must be > 0.
- `--max-pct` (max single position fraction) is bounded `[0, 0.25]`.
- `--kelly-fraction` (default conservative multiplier) is bounded
  `[0, 0.5]`.
- `--min-edge` (sub-noise filter) is bounded `[0, 0.5]`.

Each `config` call appends a fresh row to `bankroll_config`. The
analyzer always reads the latest by `effective_at`. Old configs are
preserved for audit; nothing is overwritten.

## The Kelly pipeline

For each surfaced comparison whose `|delta|` clears the
`min_edge_threshold` floor, the engine runs:

1. **Unclamped Kelly.** `f* = (model_p - market_p) / (1 - market_p)`
   for a binary YES position priced at `market_p`. Negative values
   are kept for transparency but flagged `kelly_negative`.
2. **Half-Kelly multiplier.** `suggested = kelly_fraction_default * max(0, f*)`.
   Default is 0.5 (half-Kelly).
3. **Max-cap clamp.** `suggested = min(suggested, max_single_position_pct)`.
   Default cap is 5%. Triggers `kelly_clamped_by_max_cap`.
4. **Liquidity clamp.** If
   `suggested * bankroll > liquidity_threshold * volume_24h` (default
   5% of 24h volume), the suggested dollar size is clamped down to
   `liquidity_threshold * volume_24h`. Triggers
   `kelly_clamped_by_liquidity` and `low_liquidity`.

The unclamped Kelly is always preserved on the analysis row so the
operator can see how aggressive the math wanted to go.

## Bankroll survival

Every non-sub-threshold analysis includes survival fractions for
1, 3, and 5 consecutive adverse outcomes assuming the suggested
fraction is reapplied each round:

- After 1 loss: `(1 - f)^1`
- After 3 losses: `(1 - f)^3`
- After 5 losses: `(1 - f)^5`

These are decision-support figures, not predictions. They answer
"how much of the analytical bankroll would survive a sequence of
losses if the suggested fraction were applied each time."

## Invalidation criteria

Each analysis carries a structured list of "if observable X moves
to Y, revisit" criteria, generated automatically from the embedded
scanner trace plus the comparison state:

- **Precursor-shift criteria** — for each precursor that fired,
  "if it drops back below threshold X, the model signal weakens";
  for each non-fired precursor, "if it crosses above threshold X,
  the model signal would change."
- **Market-move criteria** — the market price at which the
  surfacing threshold no longer holds (two-sided).
- **General caveats** — when low-confidence or stale-data conditions
  hold, an explicit caveat criterion is recorded.

## The trace renderer

Every analysis renders to text matching this layout:

```
==============================================
ANALYSIS: <class title>
SECTOR: <sector>
==============================================

WARNINGS:
  - <flagged warnings, or "(no warnings)">

SOURCE COMPARISON:
  Model probability: 0.3000  (CI: [0.2000, 0.4000])
  Market-implied probability: 0.1000  (spread: 200 bps)
  Delta: +0.2000  (log-odds: +1.400)

SIZING ANALYSIS (if the operator chose to act):
  Kelly fraction (theoretical maximum, before clamping): 0.2222
  Suggested fraction (after half-Kelly + caps, conservative): 0.0500
  Suggested dollar size: $50.00 of $1000.00 analytical bankroll
  This represents 0.0020 of the market's 24h volume.
  Suggested fraction was clamped down by the max_single_position_pct cap.

BANKROLL-SURVIVAL SCENARIOS:
  After 1 adverse outcome: bankroll at 0.9500 of starting
  After 3 adverse outcomes: bankroll at 0.8574
  After 5 adverse outcomes: bankroll at 0.7738

EXPECTED VALUE (analytical metric, not a recommendation):
  EV per dollar (if held to resolution): 0.2000

INVALIDATION CRITERIA:
  - if <variable> drops back below 5.000, the model signal weakens
  - if market_p moves to 0.1234 the surfacing threshold no longer holds

TIME TO RESOLUTION:
  180 days remaining

DISCLAIMER:
  This is decision-support analysis. Kelly figures are theoretical
  optima before accounting for model error, transaction costs,
  slippage, and the possibility that the model is wrong. Half-Kelly
  is the conservative default and should still be considered an
  upper bound. The system does not place orders; the operator
  decides whether and how to act, and is responsible for any
  real-world outcomes.

==============================================
```

The `--verbose` flag adds a sensitivity-analysis section showing
how the suggested fraction changes when `model_p` is perturbed by
±10% and ±20%.

## The imperative-language linter

Every render passes through `frame/linter.py` before the analysis
is persisted. The linter refuses any output containing a phrase
from `config/forbidden_phrases.yaml` — the seed catalog covers
phrases like "you should buy", "go long", "i recommend", "the
trade is", "take this position", "guaranteed to", etc.

Operators extend the catalog by editing the YAML. The check is
case-insensitive substring match.

When a render fails the linter, the analysis cycle records the
failure on the cycle's `errors` field, marks the per-comparison
analysis with the offending phrase, and continues with the next
comparison. No partially-imperative output ever reaches the
operator's terminal.

## Watch state lifecycle

```
            +-------------------+
            |  (no state set)   |
            +---------+---------+
                      |
                      | razor-rooster position-engine watch
                      v
            +---------+---------+
            |    'watching'     |<---+
            +---------+---------+    |
                      |              |
       acted-on / dismiss            | (operator can re-set)
                      |              |
                      v              |
        +-------------+-----------+  |
        |   'acted_on' /          |  |
        |   'dismissed'           |  |
        +-------------+-----------+  |
                      |              |
        market resolution            |
                      |              |
                      v              |
            +---------+---------+    |
            |    'expired'      |    |
            +-------------------+    |
                                     |
                  (operator may re-set state post-resolution)
```

States append to `watch_states`; the latest row by `set_at` wins.
`set_by` is `'operator'` for CLI-driven transitions and
`'system'` for the auto-expiration pass.

The auto-expiration pass runs at the end of each
`razor-rooster position-engine run` cycle. It transitions any
active `'watching'` or `'acted_on'` states to `'expired'` when the
underlying market has a `comparison_resolutions` row.

## Storage layout

Five tables under the position_engine namespace live alongside
the prior subsystems' tables:

- `bankroll_config` — append-only history of bankroll declarations.
- `analysis_cycles` — one row per cycle execution.
- `analyses` — one row per (cycle, comparison) pair.
- `analysis_traces` — rendered text + structured JSON per analysis.
- `watch_states` — append-only log of operator watch-state
  transitions plus auto-expirations.

Schema migration version namespacing: `position_engine` uses 5001+,
namespaced clear of `data_ingest` (1–999),
`polymarket_connector` (1001–1999), `pattern_library`
(2001–2999), `signal_scanner` (3001–3999), and
`mispricing_detector` (4001–4999).

## Disk and performance

- v1 disk budget: **100 MB** out of the 100 GB global cap given
  v1 scale (≤20 surfaced comparisons per cycle) and daily cadence
  over the first year (NFR-PE-DISK-001).
- Daily cycle target: **under 1 minute** on EliteBook G8 hardware
  for the v1 scale (NFR-PE-PERF-001).
- Per-analysis: sub-second math.

## After T-PE-081 measurements

The first real-hardware cycle against the live mispricing_detector
output produces the empirical distribution of Kelly fractions,
clamping rates (max-cap and liquidity), and analysis durations.
Update DEFER-PE-001..003 in
`specs/POSITION_ENGINE_TASKS.md` with measured numbers:

- Per-sector empirical distribution of Kelly fractions.
- Per-sector clamping rates.
- Per-sector liquidity-feasibility threshold validation.
- Long-resolution threshold per-class tuning if the 365-day default
  is too aggressive or too lenient.

If a sector's clamping rate is anomalous (~0% or ~100%), revise the
per-sector liquidity threshold in `config/position_engine.yaml`.

## See also

- `razorrooster.md` — LOOM (project state of truth)
- `specs/POSITION_ENGINE.md`,
  `specs/POSITION_ENGINE_DESIGN.md`,
  `specs/POSITION_ENGINE_TASKS.md` — full requirements / design
  / tasks
- `docs/mispricing.md` — the upstream comparison layer
- `docs/scanner.md` — the upstream scanner that produces the model
  posteriors
- `docs/pattern_library.md` — the upstream class registry
- `src/razor_rooster/position_engine/engines/` — the Kelly,
  bankroll, liquidity, sensitivity, time-to-resolution,
  invalidation, and analyzer modules
- `src/razor_rooster/position_engine/frame/` — the renderer +
  imperative-language linter
- `config/forbidden_phrases.yaml` — operator-extensible linter
  catalog
