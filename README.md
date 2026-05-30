# Razor-Rooster

Geopolitical event forecasting and calibration engine. Educational decision-support for an individual operator. **Recommendation-only — no automated execution, no order placement, no real capital handling.**

The system pulls public data, computes historical base rates over configurable retrospective windows, scans current conditions for matches against precursor signatures, surfaces deltas between model probabilities and Polymarket-implied probabilities, and writes a structured report. The operator forms the view; the operator decides whether to act.

## Status

v1 implementation in progress. Subsystem coverage:

| Subsystem | Spec | Implementation |
| - | - | - |
| `data_ingest` | DONE | Phases 0-6 DONE, Phase 7 partial (T-070, T-071, T-074 done; T-072, T-073 are operator-driven) |
| `polymarket_connector` | DONE | Phases 0-6 DONE, Phase 7 partial (T-PMC-070, T-PMC-071, T-PMC-074 done; T-PMC-072, T-PMC-073 are operator-driven) |
| `pattern_library` | DONE | Phases 0-8 DONE; T-PL-081 first-run-on-real-hardware is operator-driven |
| `signal_scanner` | DONE | Phases 0-5 DONE; T-SCAN-081 first-scan-on-real-hardware is operator-driven |
| `mispricing_detector` | DONE | Phases 0-6 DONE; T-MD-081 first-cycle-on-real-hardware is operator-driven |
| `position_engine` | DONE | Phases 0-8 DONE; T-PE-081 first-cycle-on-real-hardware is operator-driven |
| `monitor` | DONE | Phases 0-5 DONE; T-MON-081 first-cycle-on-real-hardware is operator-driven |
| `report_generator` | DONE | Phases 0-6 DONE; T-RG-082 first-report-on-real-hardware is operator-driven |
| `kalshi_connector` | DONE | Phases 0-6 + Phase 7 T-KSI-070 + T-KSI-074 DONE; T-KSI-071, T-KSI-072, T-KSI-073 are operator-driven |

For the full spec set, see `specs/`. The living manifest is `razorrooster.md`.

## Prerequisites

- macOS or Linux (Python 3.11+).
- Python 3.12 recommended (`/opt/homebrew/bin/python3.12` on the development machine).
- ~150 GB free disk for the v1 corpus (100 GB hard cap with 80% warn / 95% pause).
- Optional API credentials for authenticated sources (see "Credentials" below).

## Setup

```bash
make install            # creates .venv, installs the package + dev deps
make test               # runs unit + integration tests (skips smoke marker)
make lint               # ruff lint + format check
make typecheck          # mypy strict on data_ingest + polymarket_connector
```

The `Makefile` is the single source of truth for tooling commands. `make help` lists every target.

## Credentials

Authenticated sources read credentials from `.env` at the workspace root. **Never commit `.env`.** The `.gitignore` already excludes it.

| Source | Required env vars | How to obtain |
| - | - | - |
| FRED | `FRED_API_KEY` | https://fred.stlouisfed.org/ — free key |
| ACLED | `ACLED_USERNAME`, `ACLED_PASSWORD` | https://acleddata.com/ — register; OAuth 2.0 password grant |
| EIA | `EIA_API_KEY` | https://www.eia.gov/opendata/ — free key |
| NRC ADAMS | `NRC_ADAMS_API_KEY` | NRC Public Search API subscription key |
| regulations.gov | `REGULATIONS_GOV_API_KEY` | https://api.data.gov/signup — free key |
| NOAA CDO | `NOAA_CDO_TOKEN` | https://www.ncdc.noaa.gov/cdo-web/token — free token |

Unauthenticated sources (no credentials needed): FRED's BDI proxy series (uses the FRED key), World Bank, GDELT events, USGS Mineral Commodity Summaries, WHO Disease Outbreak News, Federal Register.

If a credential is absent for an authenticated source, that source is skipped cleanly — other sources still run.

### ACLED license gate

ACLED data is published under terms that default to non-commercial use. Razor-Rooster's ingest layer enforces a conservative posture:

- `commercial_use_recorded_grant = FALSE` by default. Operators must explicitly review ACLED's then-current Terms and record any commercial-use grant.
- The Terms are hash-versioned at first use. A change in the Terms text triggers a re-acknowledgement before the next ACLED fetch.
- Refuses to start the ACLED connector without a recorded acknowledgement.

The acknowledgement gate is implemented in code; see `src/razor_rooster/data_ingest/connectors/acled.py`.

## First run

From a clean checkout:

```bash
make install
cp .env.example .env                # if you have one; otherwise create .env yourself
# edit .env with credentials for the sources you want to enable

razor-rooster ingest init             # apply schema migrations to data/trough.duckdb
razor-rooster ingest cycle            # run one incremental cycle (writes JSONL log)
razor-rooster ingest status           # show per-source freshness
```

The DuckDB store lives at `data/trough.duckdb` by default. Override with `--db PATH` or the `RAZOR_ROOSTER_DB` env var.

### Backfill

Backfill is per-source and resumable. To pull historical data for one source:

```bash
razor-rooster ingest backfill --source fred
razor-rooster ingest backfill --source worldbank --batch-size 5000
razor-rooster ingest backfill --source gdelt_events       # capped at 5 years per design
```

If a backfill is interrupted, re-running it picks up from the last committed resume token. Pass `--restart` to ignore prior state and start over from the connector's earliest data.

Backfill respects per-source byte caps and the global 100 GB corpus cap from `config/source_caps.yaml`. When a cap is reached, the backfill exits cleanly with status `CAP_REACHED` and a structured warning. Operators raise the cap by editing the config and re-running.

## Daily cycle

Razor-Rooster does not run itself. Schedule the daily cycle via the host's scheduler.

### macOS — `launchd`

Create `~/Library/LaunchAgents/com.razorrooster.cycle.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.razorrooster.cycle</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/YOUR_USERNAME/Sloptropy/razorrooster/.venv/bin/razor-rooster</string>
      <string>ingest</string>
      <string>cycle</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOUR_USERNAME/Sloptropy/razorrooster</string>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/Sloptropy/razorrooster/logs/cycle.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/Sloptropy/razorrooster/logs/cycle.stderr.log</string>
  </dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.razorrooster.cycle.plist
launchctl start com.razorrooster.cycle      # immediate test run
```

### Linux — `cron`

```cron
30 8 * * *  cd /home/YOUR_USER/razorrooster && .venv/bin/razor-rooster ingest cycle >> logs/cycle.stdout.log 2>> logs/cycle.stderr.log
```

The schedule honors NFR-PERF-001 (full cycle under 30 minutes on EliteBook G8 hardware). If `make smoke` runs over budget, investigate before scheduling daily.

## Polymarket connector

The Polymarket subsystem (`razor-rooster polymarket`) reads public market data: market metadata, prices, resolutions, and opt-in trade history for watched markets. It is read-only — no wallet, no signing, no order placement. Two non-bypassable startup gates protect every Polymarket subcommand.

### First run (Polymarket)

```bash
# 1. Declare your jurisdiction (env var wins, config/operator.yaml is the fallback).
export OPERATOR_JURISDICTION=DE   # ISO 3166-1 alpha-2; must NOT match config/restricted_jurisdictions.yaml.

# 2. Acknowledge the current Polymarket Terms of Service.
razor-rooster polymarket ack-tos
# Reads the live ToS, hashes it, prompts for confirmation, records the ack.

# 3. Run a full sync.
razor-rooster polymarket sync
# Markets → prices → resolutions → watched trades, with sector heuristic mapping.

# 4. Inspect freshness.
razor-rooster polymarket status
```

### Geo-restriction gate

`OPERATOR_JURISDICTION` is required. The gate compares against `config/restricted_jurisdictions.yaml` and refuses with a typed `StartupRefusal` if the value matches. The list reflects Polymarket's publicly-published geofence at time of authoring; **operators are responsible for keeping it current**. There is no proxy or VPN hook anywhere in the codebase — the connector exclusively uses direct HTTPS to Polymarket's documented API hosts.

### ToS acknowledgement gate

`razor-rooster polymarket ack-tos` records the operator's acknowledgement of the current Polymarket Terms of Service, stored as a SHA-256 hash on the `polymarket` source row. On every subsequent invocation, the gate fetches the live ToS, re-hashes, and compares. A mismatch refuses the connector and re-prompts. If the live URL is briefly unreachable, the gate falls back to the most recent hash in `polymarket_tos_version_history`.

### Watched markets

Configure markets that warrant higher-frequency snapshots and full trade-history pulls:

```bash
razor-rooster polymarket watch <condition_id>
razor-rooster polymarket unwatch <condition_id>
razor-rooster polymarket list-watched
razor-rooster polymarket fetch-orderbook <condition_id> --token-id <id>
razor-rooster polymarket snapshot --watched
```

Watched-market state lives in `config/polymarket.yaml` under `sync.prices.watched_markets`.

### Sector mapping triage

The Polymarket markets sync runs a keyword-based heuristic that classifies each market into one of the six Razor sectors. Markets the heuristic can't classify surface for operator review:

```bash
razor-rooster polymarket needs-review        # list pending reviews
razor-rooster polymarket map <id> <sector>   # record a manual override
razor-rooster polymarket map <id> none       # explicitly mark as "no Razor sector"
razor-rooster polymarket mapping-stats       # counts by sector and confidence
```

A manual override is never overwritten by the heuristic on subsequent cycles. The keyword catalogue lives in `config/sector_keywords.yaml` and can be edited without code changes; the connector re-reads it on each cycle.

## Kalshi connector

The Kalshi subsystem (`razor-rooster kalshi`) reads public market data from Kalshi: series, events, markets, prices, settlements, and optionally orderbook depth + trade history for watched markets. It is read-only — no API key, no RSA-PSS signing, no order placement. Two non-bypassable startup gates protect every Kalshi subcommand.

### First run (Kalshi)

Kalshi is a CFTC-regulated US designated contract market. The connector enforces an **allow-list** posture (the inverse of Polymarket's deny-list); the seed list is `["US"]`. The same `OPERATOR_JURISDICTION` env var that Polymarket consults drives the Kalshi gate too — one operator declaration, two opposite outcomes.

```bash
# 1. Declare your jurisdiction. Must be on Kalshi's allow-list.
export OPERATOR_JURISDICTION=US   # ISO 3166-1 alpha-2; default allow-list is just US.

# 2. Acknowledge the current Kalshi Terms of Service. Records the
# acknowledgement under the v1 read_only posture.
razor-rooster kalshi ack-tos

# 3. Run a full sync (cutoff → series → events → markets → prices
# → settlements; trades skipped if no markets are watched).
razor-rooster kalshi sync

# 4. (Optional) Backfill historical settlements for the calibration log.
razor-rooster kalshi backfill-settlements

# 5. Inspect freshness, ToS posture, cutoff snapshot, and mapping counts.
razor-rooster kalshi status
```

### Eligibility gate

`OPERATOR_JURISDICTION` is required. The gate compares against `config/kalshi_allowed_jurisdictions.yaml` and refuses with `EligibilityRefusal` when the value is **not** on the list. Kalshi may extend or restrict access without notice; **operators are responsible for keeping the allow-list current**.

### ToS acknowledgement gate (with posture)

`razor-rooster kalshi ack-tos` records the operator's acknowledgement of the current Kalshi Terms of Service as a SHA-256 hash plus an explicit `acknowledged_posture='read_only'`. The gate refuses the connector if the recorded posture is anything other than `read_only` (a future v2 trading posture is reserved). On every subsequent invocation, the gate fetches the live ToS, re-hashes, and compares; a mismatch refuses and re-prompts. If the live URL is briefly unreachable, the gate falls back to the most recent hash in `kalshi_tos_version_history`.

### Watched markets

Configure tickers that warrant tighter-cadence price snapshots and the trade-history pull:

```bash
razor-rooster kalshi watch <ticker>
razor-rooster kalshi unwatch <ticker>
razor-rooster kalshi list-watched
razor-rooster kalshi fetch-orderbook <ticker> [--depth N] [--persist|--no-persist]
razor-rooster kalshi snapshot-prices --watched
```

Watched-market state lives in `config/kalshi.yaml` under `sync.prices.watched_markets`.

### Sector mapping triage

The Kalshi markets sync runs a heuristic that classifies each market into one of the eight Razor sectors plus a Kalshi-specific `out_of_scope` bucket for sports / entertainment / daily-life markets. Markets the heuristic can't classify surface for operator review:

```bash
razor-rooster kalshi needs-review              # list pending reviews
razor-rooster kalshi map <ticker> <sector>     # record a manual override
razor-rooster kalshi map <ticker> out_of_scope # Kalshi-specific value
razor-rooster kalshi map <ticker> none         # explicit-null operator decision
razor-rooster kalshi mapping-stats             # counts by sector and confidence
```

A manual override is never overwritten by the heuristic on subsequent cycles. The keyword catalogue lives in `config/kalshi_sector_keywords.yaml`.

### Cross-venue mapping

A single event class can map to both Polymarket and Kalshi simultaneously. The `mispricing run` cycle then writes one comparison row per (class, venue) pair; downstream subsystems carry the discriminator end-to-end and render `(<venue>)` after each market identifier in operator-facing output.

```bash
razor-rooster mispricing map cpi_above_target 0xPOLY_CONDITION_ID
razor-rooster mispricing map cpi_above_target KX-CPI --venue kalshi
```

Non-binary Kalshi markets (scalar / categorical) are deferred to v1.2; `mispricing map --venue kalshi` refuses with a clear error if the ticker resolves to a non-binary market.

For engine internals (cutoff routing, per-endpoint cost map, post-T-KSI-072 measurement guidance), see `docs/kalshi_connector.md`.

## Pattern library

The pattern library (`razor-rooster pattern-library`) is the historical event-pattern catalogue. It computes per-class base rates with credible intervals, empirically-derived precursor signatures, k-NN analogue feature spaces, and per-class calibration outputs against an in-memory `data/library/` workspace. Read-only consumers (signal_scanner, mispricing_detector, etc.) call into the public `library` facade rather than touching `pl_*` tables directly.

### First refresh

```bash
razor-rooster pattern-library list                    # show the registered classes
razor-rooster pattern-library validate <class_id>     # sanity-check a class without persisting
razor-rooster pattern-library refresh                 # full refresh; populates pl_* tables
razor-rooster pattern-library refresh --class <id>    # refresh one class only (operator triage)
razor-rooster pattern-library show <class_id>         # current outputs + calibration summary
razor-rooster pattern-library eval <class_id>         # ad-hoc evaluation; does NOT persist
```

The first `refresh` call applies the pattern_library schema migrations
(versions 2001+) and populates outputs against whatever `data_ingest`
data exists at that point. Classes whose predicates find zero
occurrences still complete: their base rates are zero with a
`low_sample_warning`, and their calibration record is the
`insufficient_data` sentinel. This is by design — the v1 seed library
includes scaffolding classes (e.g. `eia_grid_reliability_event`,
`polymarket_resolution_calibration`) that produce empty outputs until
the operator's data corpus or downstream subsystems land.

Expected duration on EliteBook G8 hardware: under 15 minutes for the
v1 seed library against a populated `data_ingest` corpus
(NFR-PL-PERF-001). Disk budget: 1 GB out of the 100 GB global cap.

### Adding a class

The library is operator-extensible. Each class is a `.py` module under
`src/razor_rooster/pattern_library/classes/` exposing a module-level
`CLASS = EventClass(...)`. To add a new class:

1. Copy one of the existing seed-class modules as a template.
2. Edit the class id, title, description, sector, occurrence query,
   precursors, analogue features, base-rate window, and refractory
   period.
3. Run `razor-rooster pattern-library validate <class_id>` to confirm
   the class registers cleanly.
4. Run `razor-rooster pattern-library refresh --class <class_id>` to
   compute the first round of outputs.
5. (Optionally) write a per-class documentation file under
   `specs/seed_event_classes/<class_id>.md` recording the rationale,
   data sources, predicate, precursors, analogue features, and known
   limitations.

Library version auto-bumps when the registry changes (class added,
class definition modified, class removed). Per-class
`definition_version` bumps are the operator's responsibility when the
class definition changes — the next refresh stamps outputs with the
new definition_version so consumers can detect mismatches.

### Modifying a class

Edit the class module, bump its `definition_version`, and run
`razor-rooster pattern-library refresh --class <class_id>`. Prior
outputs at the older definition_version remain in the store but are
no longer "latest" — the public facade returns outputs from the most
recent refresh by default.

### Reading library outputs from downstream code

```python
from razor_rooster.pattern_library import library
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

store = DuckDBStore("data/trough.duckdb", read_only=True)

# Latest base rate for a class.
br = library.base_rate(store, "pheic_declaration_12mo")

# All signatures for a class, latest version.
sigs = library.signature(store, "gdelt_conflict_intensification")

# Top-k analogues given the operator's current feature vector.
matches = library.find_analogues_by_class_id(
    store,
    class_id="multi_signal_geopolitical_alert",
    current_features={"acled_density_7d": 42.0, "gdelt_volume_7d": 800.0},
    k=10,
)

# Calibration result, when present.
cal = library.calibration(store, "final_rule_within_12mo")

# Library version coherence — consumers compare what they got against
# the live version to detect a stale cache.
assert br is None or br.library_version == library.current_version()
```

The `library` facade is the only sanctioned read interface. Direct
queries against `pl_*` tables are discouraged and may break across
library versions.

For the eight seed classes, predicate details, and the per-class
documentation convention, see `docs/pattern_library.md`.

## Signal scanner

The signal scanner (`razor-rooster scan`) is the live-evaluation
bridge between historical patterns and current conditions. On each
cycle it evaluates every registered `pattern_library` event class
against current `data_ingest` data, computes a per-class
current-conditions probability estimate (Bayesian update with Monte
Carlo CI), and surfaces classes whose estimate has materially diverged
from the base rate as candidate situations.

### Daily cadence

```bash
# Run the daily scan after data_ingest cycle and pattern_library refresh.
razor-rooster scan run
```

The default invocation evaluates every registered class with the
candidate-identification thresholds from `config/scanner.yaml`. Per
NFR-SCAN-PERF-001, a full scan completes within 5 minutes on
EliteBook G8 hardware against the v1 seed library.

### Investigating candidates

```bash
razor-rooster scan list-candidates --since 2026-05-01
razor-rooster scan show-trace <scan_id> <class_id>
```

The trace explains why the system flagged the candidate: the prior,
each precursor's current value vs. its threshold, the applied
likelihood ratios, the optional co-occurrence correction, the
posterior, the log-odds shift, and any warnings (low signature
confidence, source stale, library stale, definition drift). The
operator decides whether to escalate to a deeper analysis.

```bash
# View the full structured trace JSON.
razor-rooster scan show-trace <scan_id> <class_id> --json
```

### Scan retention

All historical scan records are retained indefinitely
(REQ-SCAN-PERSIST-002) so calibration backtests have full
coverage. The store grows slowly — ~500 MB out of the 100 GB cap
after a year of daily scans against the v1 seed library
(NFR-SCAN-DISK-001). To prune older scans:

```bash
razor-rooster scan prune --before 2025-01-01T00:00:00+00:00 --confirm
```

The `--confirm` flag is mandatory; pruning without it refuses.

### Configuration

`config/scanner.yaml` controls candidate-identification thresholds
(per-sector log-odds shift minimums), the confidence floor, and the
Monte Carlo sample count. The defaults match SIGNAL_SCANNER_DESIGN.md
§3.7. Operators tune thresholds after the first real-hardware scan
(T-SCAN-081) once the empirical divergence distribution is known.

For the candidate-identification math, the trace schema, and details
on Bayesian update with co-occurrence correction, see
`docs/scanner.md`.

## Mispricing detector

The mispricing detector (`razor-rooster mispricing`) is the
model-vs-market comparison layer. For each `signal_scanner` posterior,
it finds Polymarket markets in the same event class, computes the
delta between model and market probabilities with credible-interval-
overlap analysis, and emits a structured comparison record with a
reasoning trace that presents the case for both views at equal
prominence.

The phrase "mispricing" is read in the educational-framing sense: the
subsystem detects *disagreements between model and market*, not "the
market is wrong here." Treating the market as default-correct is one
of the system's stated principles. Comparisons surface evidence; the
operator decides what to do with it.

### Class-to-market mappings

Each comparison requires a mapping between an event class and a
Polymarket market. Operator-curated mappings are precise and
authoritative; auto-derived mappings (sector match plus keyword
overlap plus temporal qualifier) produce comparisons flagged with
`mapping_confidence = 'inferred'` or `'low'`.

```bash
razor-rooster mispricing map pheic_declaration_12mo 0xCONDITION_ID \
    --type direct --notes "PHEIC declaration in 2026 question"
razor-rooster mispricing list-mappings --class pheic_declaration_12mo
razor-rooster mispricing unmap <mapping_id>
```

For markets framed inverted ("Will X NOT happen?"), use
`--polarity inverted` so the comparison flips the YES probability
before computing the delta.

### Daily cadence

```bash
# After data_ingest cycle + pattern_library refresh + signal_scanner run.
razor-rooster mispricing run
```

The default invocation evaluates every active mapping plus
auto-derived mappings against the latest scanner posteriors. The
linkage pass runs at the end of the cycle; resolved markets get
linked to prior comparisons for the calibration backtest.

### Reviewing surfaced comparisons

```bash
razor-rooster mispricing list-comparisons --surfaced-only --since 2026-05-01
razor-rooster mispricing show <comparison_id>
razor-rooster mispricing show <comparison_id> --json
```

The trace explains the comparison with two adjacent equal-prominence
sections — `case_for_model` and `case_for_market` — plus
`ambiguity_factors`, the embedded scanner trace, and any warnings
(stale market price, low liquidity, low mapping confidence,
definition drift).

### Calibration linkage

The linkage pass writes a `comparison_resolutions` row whenever a
Polymarket market resolves, polarity-aware so an inverted-mapping
comparison with a `'no'` resolution still produces
`outcome_observed = 1` for the model's event.

```bash
razor-rooster mispricing relink
```

Linkage runs automatically at the end of each cycle and is
idempotent.

### Configuration

`config/mispricing.yaml` controls per-sector surfacing thresholds,
the market-price freshness threshold (default 12h), per-sector
liquidity floors (default $10k 24h volume), and the auto-mapping
keyword/temporal heuristic knobs. Defaults match
MISPRICING_DETECTOR_DESIGN.md §3.6. Operators tune thresholds after
the first real-hardware cycle (T-MD-081) once the empirical
distribution of deltas and volumes is known.

For the surfacing-gate logic, the trace schema, and details on
the equal-prominence rendering rule, see `docs/mispricing.md`.

## Position engine

The position engine (`razor-rooster position-engine`) is the
paper-analysis sizing layer. For each surfaced
`mispricing_detector` comparison, it produces a *sizing analysis* —
a structured document with Kelly fractions, half-Kelly bounds,
expected-value figures, bankroll-survival diagnostics, and
invalidation criteria.

The subsystem produces analyses, not directives. Output uses
conditional language ("if the operator chose to act"); the
renderer linter rejects forbidden imperative phrases. The system
never places orders, manages real capital, or interacts with
Polymarket beyond reading. v1 is recommendation-only by design
(OT-004 v1 resolution).

### Bankroll setup

```bash
razor-rooster position-engine config --bankroll 1000
# prompts for confirmation of the analytical-bankroll disclaimer
```

For non-interactive use:

```bash
razor-rooster position-engine config \
    --bankroll 1000 --max-pct 0.05 --kelly-fraction 0.5 --min-edge 0.03 \
    --no-prompt --acknowledge-analytical
```

The bankroll figure is **analytical only**. The system does not
track real capital, hold positions, or interact with Polymarket
trading APIs. Bounds:

- `--kelly-fraction` is bounded `[0, 0.5]` — half-Kelly is the
  conservative ceiling.
- `--max-pct` is bounded `[0, 0.25]` — the engine refuses higher.

### Daily cadence

```bash
# After data_ingest cycle + pattern_library refresh + signal_scanner
# run + mispricing_detector run.
razor-rooster position-engine run
```

The default invocation analyzes every surfaced comparison from the
latest mispricing cycle. The auto-expiration pass runs at the end of
each cycle, transitioning watch states to `'expired'` when their
underlying markets have resolved.

### Reviewing analyses

```bash
razor-rooster position-engine list --watched
razor-rooster position-engine show <analysis_id>
razor-rooster position-engine show <analysis_id> --verbose   # includes sensitivity
```

The trace renders with conditional language throughout, warnings
before sizing math, and a standard disclaimer block. The
imperative-language linter at `frame/linter.py` refuses any output
that would contain forbidden phrases like "you should buy" or
"i recommend."

### Watch state workflow

```bash
razor-rooster position-engine watch <analysis_id> --note "interesting"
razor-rooster position-engine acted-on <analysis_id> --note "took action"
razor-rooster position-engine dismiss <analysis_id> --reason "..."
razor-rooster position-engine list --acted-on
razor-rooster position-engine list --expired
```

`'watching'` and `'acted_on'` auto-expire to `'expired'` when the
underlying Polymarket market resolves. `'dismissed'` and `'expired'`
are terminal — the operator can re-set state post-resolution if
they want to track retrospectively.

### Configuration

`config/position_engine.yaml` controls the per-sector liquidity
feasibility threshold (default 5% of 24h volume), the
long-time-to-resolution threshold (default 365 days), the
sensitivity-analysis perturbations (default ±10%, ±20%), and the
bankroll-validation bounds.

`config/forbidden_phrases.yaml` is the operator-extensible
imperative-language catalog used by the renderer linter.

For the full Kelly pipeline, the trace schema, the watch-state
lifecycle, and post-T-PE-081 measurement guidance, see
`docs/position_engine.md`.

## Monitor

The monitor (`razor-rooster monitor`) is the active-observation layer
for watched analyses. It does not recompute analyses; that is the
position engine's job. Each monitor cycle reads every analysis whose
latest watch state is `'watching'` or `'acted_on'`, snapshots
current upstream state (latest scan record, latest market price,
resolution status), classifies the change since the analysis was
produced, and writes one *follow-up* per analysis with a ranked
alert and a deterministic reasoning text.

### Daily cadence

```bash
# After the position_engine cycle.
razor-rooster monitor run
```

One cycle takes seconds for v1 scale (5–30 watched analyses). The
cycle output prints aggregate counts (total follow-ups, follow-ups
with alerts, alerts by tier, resolutions detected, expirations
written) and persists one row per follow-up plus one row in
`monitor_cycles`.

### Reading alerts

```bash
razor-rooster monitor list-alerts
razor-rooster monitor list-alerts --tier material_shift
razor-rooster monitor list-alerts --since 2026-05-01
razor-rooster monitor show <follow_up_id>
```

Alerts are ranked across five tiers (highest priority first):

1. `resolution` — the underlying market has resolved. Triggers
   automatic watch-state expiration.
2. `invalidation_triggered` — at least one of the analysis's
   stated invalidation criteria has fired.
3. `material_shift` — model or market probability has moved by a
   material or major band relative to the analysis-time value.
4. `precursor_shift` — at least one underlying precursor variable
   has crossed its threshold since the analysis.
5. `time_decay` — the days-to-resolution window is at or below the
   alert threshold (default 7 days).

A follow-up may match multiple tiers; `primary_alert_tier` is the
highest-priority match. `list-alerts` orders by tier priority then
recency.

### Trajectory views

```bash
razor-rooster monitor trajectory <analysis_id>
```

Prints every follow-up for an analysis chronologically, showing how
the model probability, market probability, resolution status, and
alert tier have evolved across cycles.

### Note-taking

```bash
razor-rooster monitor note <follow_up_id> "Reviewed; deciding to hold."
```

Notes are append-only retrospectives. They appear in the `show`
output below the reasoning text.

### Configuration

`config/monitor.yaml` controls:

- `shift_bands.default` — global magnitude classification thresholds
  (default: minor 0.01, material 0.05, major 0.15).
- `shift_bands.per_sector` — optional per-sector overrides (e.g.,
  monetary-policy markets often warrant tighter thresholds).
- `time_decay_alert_days` — global default for the time-decay
  alert window (default 7).
- `material_shift_alert_threshold` — overall magnitude that flags a
  follow-up as recommended-review (default 0.10).

### Resolution interlock

When the monitor cycle detects a resolution, it triggers
`position_engine.run_expiration_pass`, which transitions any active
`'watching'` or `'acted_on'` watch states for the resolved
comparison to `'expired'`. This keeps the two subsystems' views of
state consistent; both can detect resolution independently and
converge on the same outcome.

### Post-first-run measurement

After the first month of operator-driven cycles, the empirical
distribution of model + market shifts on watched analyses tells us
whether the default magnitude bands (0.01 / 0.05 / 0.15) are
calibrated. If too many follow-ups land in `material` for ordinary
day-to-day movement, raise the bands; if `none` and `minor`
dominate while obvious moves slip past, lower them. Threshold
revisions are recorded in `specs/MONITOR_TASKS.md` under T-MON-081.

For the engine internals, the trace schema, and the cycle
orchestration details, see `docs/monitor.md`.

## Reports

The reports CLI (`razor-rooster report`) is the operator-facing
surface — the document the operator actually reads on each cycle.
It assembles outputs from every analytical subsystem into a single
structured report covering newly surfaced comparisons, watched
analyses, recently resolved comparisons (calibration log), unmapped
candidates (watchlist), and system health.

The report is decision-support analysis. It does not place trades,
recommend specific actions, or claim certainty. The renderer
applies the same imperative-language linter as the position engine
(shared `config/forbidden_phrases.yaml`) and refuses to ship output
containing forbidden phrases.

### Daily cadence

```bash
# After the monitor cycle.
razor-rooster report generate
razor-rooster report generate --markdown ~/Documents/razor-rooster-2026-05-15.md
razor-rooster report generate --quiet --markdown ~/Documents/today.md
```

The default invocation reads from the last report's `generated_at`
through now. Override with `--since 2026-05-01T00:00:00`.

`--quiet` suppresses terminal output (useful when only the markdown
export is needed). `--markdown <path>` writes a parallel markdown
file; the parent directory is created if missing.

### Reading historical reports

```bash
razor-rooster report latest
razor-rooster report list                  # most recent 20
razor-rooster report list --since 2026-05-01
razor-rooster report show <report_id>      # rendered terminal text
```

Every generated report is persisted in the `report_log` table with
its full rendered terminal text, optional rendered markdown text,
sections rendered, sections failed, library version stamp, and a
SHA-256 hash of the disclaimer text used (so retrospective review
can verify the disclaimer hasn't drifted).

### Section structure

Every report has the same top-to-bottom layout:

1. **Header** — cycle date, library version, source freshness
   summary, prior-report-since timestamp, disabled-section note.
2. **System health** — stale sources, errored cycles per upstream
   subsystem, suppressed-comparison breakdown.
3. **Surfaced comparisons** — every `mispricing_detector`
   comparison with `surfaced = TRUE` since the prior report,
   ordered by confidence-weighted score. Each block includes the
   case-for-model and case-for-market sections at equal prominence
   (REQ-RG-FRAME-002), warnings before sizing math, and the
   position-engine analysis (with disclaimer block) when one
   exists.
4. **Active watched** — every `monitor` follow-up with
   `recommended_review = TRUE`, ranked by alert tier
   (resolution > invalidation_triggered > material_shift >
   precursor_shift > time_decay).
5. **Calibration log** — recently resolved comparisons with a
   template-driven verdict per (predicted-band, outcome) pair.
6. **Watchlist** — `signal_scanner` candidates that did not
   produce a `mispricing_detector` comparison this cycle (no active
   mapping, all mappings low-confidence, or all mapped markets had
   stale prices). Suggestions framed as suggestions, not directives.
7. **Footer** — standard disclaimer block, system version stamp,
   report ID, completion timestamp.

Empty sections render a "nothing to report this cycle" note rather
than disappearing silently.

### Failure isolation

If a section assembler fails, that section renders as
`section error: <reason>` and the rest of the report still renders.
The `sections_failed` field on the report log records the failure
for retrospective review. If the renderer's imperative-language
linter rejects the output, the report is **not** persisted; the
next run re-attempts.

### Configuration

`config/report.yaml`:

```yaml
enabled_sections:
  - system_health
  - surfaced
  - watched
  - calibration
  - watchlist

verbosity:
  watchlist: full     # 'full' includes scan reasoning; 'compact' omits
  calibration: full

calibration_first_run_lookback_days: 30
```

Disabled sections are omitted from the body and noted in the header
(REQ-RG-CONFIG-001).

`config/forbidden_phrases.yaml` is shared with the position engine
and is the operator-extensible imperative-language catalog.

### Local-only by design

The report renderer makes no network calls. The acceptance test
runs with `socket.socket` patched to raise on instantiation; if any
code path opened a network socket the test would fail
(NFR-RG-LOCAL-001).

For the renderer schema, the calibration verdict catalog, the
section-assembler contracts, and post-T-RG-082 measurement
guidance, see `docs/reports.md`.

## Smoke testing

Smoke tests exercise live API endpoints. They are operator-initiated, **not** part of `make test`:

```bash
make smoke
```

This writes to a separate `data/trough_smoke.duckdb` so the production store stays untouched. Sources without credentials are skipped cleanly.

## Recovery procedure

If a cycle leaves the system in a broken state:

1. Check the latest cycle log: `ls -lt logs/cycles/ | head`.
2. Read the JSONL line for the failing connector. The `errors[]` field has the full traceback summary.
3. For data corruption suspicion: take a backup of `data/trough.duckdb`, then run `razor-rooster ingest status` to see freshness state.
4. The schema is migration-versioned. To reset, delete `data/trough.duckdb` and `data/trough.duckdb.wal`, then `razor-rooster ingest init` and re-run backfills.
5. Per-source state lives in the `backfill_state` table. To force a single source to restart from scratch: `razor-rooster ingest backfill --source SOURCE_ID --restart`.

## Threat context

Per the LOOM (`razorrooster.md` §1):

- `data_ingest`: STANDARD. Public APIs, key-secured authentication, no PII collection beyond what the source publishes (e.g., named conflict actors in ACLED data).
- `polymarket_connector`: STANDARD for v1 (read-only public data). Returns to FULL when v2+ adds trading.
- `kalshi_connector`: STANDARD for v1 (read-only public data; no API key, no RSA-PSS signing). Returns to FULL when v2+ adds trading.
- `position_engine`: STANDARD for v1 (paper-analysis only). Returns to FULL if v2+ adds order placement.

The system never sends data to third parties. Outbound network traffic is exclusively to the configured upstream sources (FRED, World Bank, ACLED, etc.) and the public endpoints of Polymarket and Kalshi. There is no telemetry.

## License and disclaimers

This is an educational tool for understanding model-vs-market disagreement. It does not constitute investment advice, trading recommendations, or an offer to trade. Markets are correct more often than not; when the model and market disagree, the trace presents the case for both views at equal prominence.

ACLED data is subject to ACLED's terms; Razor-Rooster enforces the conservative non-commercial posture by default. See "Credentials" above.

## See also

- `docs/user_guide.md` — operator-facing reference: every CLI command, every option, every config knob, common workflows, troubleshooting
- `razorrooster.md` — LOOM (project state of truth)
- `specs/` — full requirements / design / tasks for each subsystem
- `docs/sources.md` — per-source reference (license, ToS, free-tier limits, backfill depth, expected disk footprint)
- `docs/pattern_library.md` — the eight seed classes and the per-class documentation convention
- `docs/scanner.md` — candidate-identification math, trace schema, configuration knobs
- `docs/mispricing.md` — class-to-market mapping rules, surfacing gates, default-to-market trace framing
- `docs/position_engine.md` — Kelly pipeline, watch-state lifecycle, sensitivity analysis, post-first-run measurement guidance
- `docs/monitor.md` — engine internals, follow-up trace schema, cycle orchestration, post-first-run measurement guidance
- `docs/reports.md` — section structure, calibration verdict catalog, section-assembler contracts, framing constraints, post-first-run measurement guidance
- `docs/kalshi_connector.md` — Kalshi engine reference: cutoff routing, per-endpoint cost map, cross-venue mapping, post-T-KSI-072 measurement guidance
