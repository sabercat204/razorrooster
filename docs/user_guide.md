# Razor-Rooster User Guide

This is the operator-facing reference for the whole platform. It explains
what each subsystem does, what each CLI command does, and what every
parameter and config knob means.

It does not replace the per-subsystem docs (`docs/scanner.md`,
`docs/mispricing.md`, `docs/position_engine.md`, `docs/monitor.md`,
`docs/reports.md`, `docs/sources.md`, `docs/pattern_library.md`); those
go deeper on math and trace schemas. This guide is the index — what to
run, when, and why, with every option called out.

> **Framing reminder.** Razor-Rooster is decision-support analysis, not
> automated trading. The system surfaces patterns, comparisons, and
> analyses; it does not place trades, recommend specific actions, or
> claim certainty about outcomes. Every operator-facing rendering uses
> conditional language ("if the operator chose to act") and presents
> the case for the model alongside the case for the market at equal
> prominence. The renderer linter refuses any output containing
> imperative phrases. The operator owns every decision.

---

## Contents

0. [Getting started](#0-getting-started)
1. [Architecture at a glance](#1-architecture-at-a-glance)
2. [Daily operating loop](#2-daily-operating-loop)
3. [Top-level CLI](#3-top-level-cli)
4. [`razor-rooster ingest` — data acquisition](#4-razor-rooster-ingest--data-acquisition)
5. [`razor-rooster polymarket` — market data](#5-razor-rooster-polymarket--market-data)
6. [`razor-rooster pattern-library` — historical patterns](#6-razor-rooster-pattern-library--historical-patterns)
7. [`razor-rooster scan` — current-conditions scoring](#7-razor-rooster-scan--current-conditions-scoring)
8. [`razor-rooster mispricing` — model-vs-market comparison](#8-razor-rooster-mispricing--model-vs-market-comparison)
9. [`razor-rooster position-engine` — sizing analysis](#9-razor-rooster-position-engine--sizing-analysis)
10. [`razor-rooster monitor` — watched-analysis follow-ups](#10-razor-rooster-monitor--watched-analysis-follow-ups)
11. [`razor-rooster report` — operator-facing document](#11-razor-rooster-report--operator-facing-document)
12. [Configuration reference](#12-configuration-reference)
13. [Common workflows](#13-common-workflows)
14. [Troubleshooting](#14-troubleshooting)

---

## 0. Getting started

This section walks a fresh clone all the way to a daily-cadence pipeline
producing reports against live data. Read it linearly the first time;
return to it as a checklist on subsequent installs.

### What Razor-Rooster is and is not

Razor-Rooster is a **decision-support analysis platform**. It does
the following:

- Pulls public macroeconomic, geopolitical, and event data on a
  schedule.
- Computes historical base rates and analogue matches per event
  class.
- Scores current conditions against those base rates to produce
  per-class probability estimates with reasoning traces.
- Pulls live prices from Polymarket and Kalshi (read-only,
  public-data endpoints).
- Surfaces *comparisons* between the model's estimated
  probabilities and the market-implied probabilities, with an
  equal-prominence "case for the market" rendered alongside the
  "case for the model" so the operator sees both sides.
- Produces analytical sizing analyses (Kelly / half-Kelly math,
  conditional language) and a daily report.

Razor-Rooster does **not**:

- Place trades, send orders, or move money. There is no execution
  layer in v1; the codebase has no broker integrations.
- Recommend specific actions. Every rendered output uses
  conditional language ("if the operator chose to act"), presents
  both sides, and runs through an imperative-language linter that
  refuses output containing directive phrasing.
- Claim certainty about outcomes. Probability estimates carry
  credible intervals; calibration is tracked across cycles and
  surfaced in every report.
- Track real capital. The bankroll figure used for sizing math is
  declared by the operator as an analytical figure; the system
  has no notion of executed positions or P&L.

The operator decides what to do with the comparisons it surfaces.
That decision lives outside the system.

### Prerequisites

| Requirement | Why | Where to get it |
| - | - | - |
| Python 3.12 | Runtime | [python.org](https://www.python.org/) or `brew install python@3.12` |
| Disk: ≥ 2 GB free under the workspace | DuckDB store + report artifacts grow with use | — |
| macOS / Linux shell | The bootstrap script and CLI | — |
| Git | Cloning + version pinning | — |
| Operator jurisdiction not on the platform restricted lists | Polymarket and Kalshi geo gates refuse requests from restricted jurisdictions; this is enforced, not theatre | — |

The platform does not require a specific OS, but every command
in this guide is shown using the macOS / Linux shell convention.

### Step 1 — Install

```bash
git clone <your-fork-or-the-source-url> razorrooster
cd razorrooster
make install            # creates .venv and installs the package + dev extras
```

Verify the install:

```bash
.venv/bin/razor-rooster --version
.venv/bin/razor-rooster --help
```

You will see the eight subsystem command groups: `ingest`,
`polymarket`, `kalshi`, `pattern-library`, `scan`, `mispricing`,
`position-engine`, `monitor`, `report`.

**✓ Verification checkpoint.** If both commands succeed and the
help output lists the subsystems, the package and CLI entry
point are wired correctly. If `razor-rooster --version` reports
"command not found", the venv didn't activate; re-run
`make install` and confirm `.venv/bin/razor-rooster` exists.

### Step 2 — Configure credentials

Razor-Rooster reads every credential from environment variables.
The bootstrap script auto-loads a `.env` file in the repo root if
present.

```bash
cp .env.example .env
$EDITOR .env
```

`.env.example` enumerates every supported variable with a comment
explaining what it does and where to get the key. None of the
keys are mandatory in the sense that the bootstrap will skip
sources whose credentials are absent, but more credentials means
more upstream coverage.

Free public-data accounts that take ~5 minutes each:

| Variable | Source | Sign-up URL |
| - | - | - |
| `FRED_API_KEY` | Federal Reserve Economic Data | `https://fred.stlouisfed.org/docs/api/api_key.html` |
| `EIA_API_KEY` | U.S. Energy Information Administration | `https://www.eia.gov/opendata/register.php` |
| `NRC_ADAMS_API_KEY` | Nuclear Regulatory Commission | `https://adams.nrc.gov/wba/` |
| `REGULATIONS_GOV_API_KEY` | Federal rulemaking docket | `https://open.gsa.gov/api/regulationsgov/` |
| `NOAA_CDO_TOKEN` | NOAA Climate Data Online | `https://www.ncdc.noaa.gov/cdo-web/token` |

Sources without sign-up gates are always available: World Bank,
GDELT, USGS, federal_register.

ACLED (`ACLED_USERNAME` + `ACLED_PASSWORD`) requires an
institutional account and OAuth2 password grant; if you don't
have one, leave it unset and the bootstrap will skip it.

Two operator-acknowledgement variables are also worth setting in
`.env`:

| Variable | What it does |
| - | - |
| `RAZOR_ROOSTER_JURISDICTION` | Your jurisdiction string (e.g. `US-NY`). The Polymarket and Kalshi geo gates check this against restricted-jurisdiction allow-lists before any outbound request. |
| `RAZOR_ROOSTER_CONTACT` | Contact string interpolated into outbound `User-Agent` headers (REQ-PM-COMPAT-002 / REQ-KALSHI-COMPAT-002). Use an email or repo URL. |

**✓ Verification checkpoint.** Confirm `.env` is set up
correctly without exposing secrets:

```bash
# Show which credential vars are set (values masked).
for v in FRED_API_KEY EIA_API_KEY NRC_ADAMS_API_KEY \
         REGULATIONS_GOV_API_KEY NOAA_CDO_TOKEN \
         ACLED_USERNAME ACLED_PASSWORD \
         RAZOR_ROOSTER_JURISDICTION RAZOR_ROOSTER_CONTACT; do
    if [[ -n "$(grep "^$v=" .env 2>/dev/null | cut -d= -f2-)" ]]; then
        echo "  ✓ $v set"
    else
        echo "  - $v not set (source will be skipped)"
    fi
done
```

The bootstrap script you'll run in Step 5 prints the same
detection table on every run and will skip cleanly past any
absent credentials.

### Step 3 — One-time terms-of-service acknowledgements

Polymarket and Kalshi each have a one-time ToS acknowledgement
gate. The CLI fetches the live ToS, hashes it, displays the URL,
and records the ack:

```bash
.venv/bin/razor-rooster polymarket ack-tos
.venv/bin/razor-rooster kalshi ack-tos
```

Both prompt for confirmation. To run them non-interactively
during bootstrap, set `RAZORROO_AUTOACK_POLYMARKET=1` and
`RAZORROO_AUTOACK_KALSHI=1` in `.env`. The ack is still recorded
with the live ToS hash, and re-prompting still triggers when the
hash changes on a future ToS update.

**✓ Verification checkpoint.** After running both `ack-tos`
commands, confirm both venues report the ack as recorded:

```bash
.venv/bin/razor-rooster polymarket status | grep -i tos
.venv/bin/razor-rooster kalshi status | grep -i tos
```

Both lines should report acknowledgement state as recorded /
yes / true. If either reports otherwise, re-run the matching
`ack-tos` and check that your jurisdiction satisfies the
relevant gate (Polymarket deny-list / Kalshi allow-list — see
the help text and §3 / §5 / §5b for the gate semantics).

### Step 4 — Declare the analytical bankroll

The position-engine refuses to run until the operator declares an
analytical bankroll figure. This is **not** a real-capital
declaration; it's the dollar value used for sizing math
(`fraction × bankroll = analyzed position size`). The system has
no execution layer — there is nothing this figure exposes to risk.

```bash
.venv/bin/razor-rooster position-engine config \
    --bankroll 1000 \
    --acknowledge-analytical \
    --notes "first-time setup, analytical figure only"
```

Or set `RAZORROO_BANKROLL_USD=1000` in `.env` and the bootstrap
will declare it automatically the first time it sees a
no-bankroll error.

The default sizing knobs (`--max-pct 0.05`,
`--kelly-fraction 0.5`, `--min-edge 0.03`) are deliberately
conservative; they mirror the conservatism described in
[§9 position-engine](#9-razor-rooster-position-engine--sizing-analysis).

**✓ Verification checkpoint.** Confirm the bankroll config
landed:

```bash
.venv/bin/razor-rooster position-engine config --help    # confirm flags
.venv/bin/razor-rooster position-engine list --watched   # should not error
```

If `position-engine list` errors with "no bankroll_config",
re-run the config command above (or set `RAZORROO_BANKROLL_USD`
in `.env` and re-run bootstrap).

### Step 5 — Run the bootstrap

```bash
make bootstrap
```

This script (located at `scripts/bootstrap.sh`) is idempotent and
safe to re-run any time. It:

1. Applies schema migrations to the DuckDB store
   (`data/trough.duckdb` by default; override with
   `RAZOR_ROOSTER_DB`).
2. Detects which `.env` credentials are present and runs an
   ingest cycle scoped to those sources.
3. Refreshes the pattern library against the ingested rows.
4. Runs Polymarket / Kalshi sync (only if ToS is acked).
5. Runs the signal scanner, mispricing detector, position
   engine, and monitor in DAG order.
6. Generates a daily report (markdown + HTML) under
   `data/reports/{YYYYMMDD}.{md,html}`.
7. Prints a per-step timing summary and a structured JSON
   artifact under `data/logs/bootstrap-{timestamp}.json`.
8. Lists every blocked stage and exactly which env var or
   subcommand would unblock it.

Typical first run on a fresh workspace with the public-data keys
filled in but no Polymarket / Kalshi ack: ~60 seconds, 8 stages
ran, 2 stages blocked. With everything configured: ~3-5 minutes
including the venue syncs.

**✓ Verification checkpoint.** A successful bootstrap exits 0
and prints a summary like:

```
Bootstrap summary
  ran:     15 step(s)
  skipped: 0 step(s)
  blocked: 0 step(s)
  total:   183s
```

Concretely, the system is fully operational when:

- `ran` count is 15 (or more if future steps land).
- `blocked` is 0 (or only blocked on credentials you
  intentionally chose to omit).
- The structured summary file `data/logs/bootstrap-{timestamp}.json`
  exists and parses as JSON.
- `data/trough.duckdb` exists and is non-empty.
- `data/reports/{YYYYMMDD}.md` and `.html` exist for today's
  date.

Spot-check the data:

```bash
.venv/bin/razor-rooster ingest status     # per-source freshness
.venv/bin/razor-rooster polymarket status # market data state
.venv/bin/razor-rooster kalshi status     # kalshi data state
.venv/bin/razor-rooster pattern-library list  # registered classes
```

If any `status` command shows all sources as "?" or "stale",
re-run `make bootstrap` after fixing the matching credential.

### Step 6 — Read the daily report

```bash
.venv/bin/razor-rooster report latest
```

Or open `data/reports/{YYYYMMDD}.html` in a browser.

The report has up to nine sections (some are opt-in via
`config/report.yaml`):

- **System health** — stale sources, failed cycles, anything
  that needs attention before reading further.
- **Surfaced comparisons** — the model-vs-market disagreements
  the system found this cycle. Each carries a reasoning trace,
  the model's posterior with credible interval, the market
  price, and the case for both sides.
- **Cross-venue disagreements** — when the same event class is
  priced on Polymarket and Kalshi, the cross-venue spread.
- **Active watched** — analyses the operator marked
  `--watch` with new monitor follow-ups since last cycle.
- **Calibration log** — recent resolutions and their
  realized-vs-predicted Brier scores.
- **Reliability diagram** — calibration plot bins.
- **Watchlist** — `signal_scanner` candidates that didn't
  surface this cycle but are close to threshold.
- **Recent threshold changes** — operator-driven config
  edits in the last window.
- **At-a-glance** (opt-in) — top item from each section
  lifted into a structured key/value summary at the very top.

The report renderer linter rejects any imperative phrasing.
What's printed is descriptive: observations, comparisons,
reasoning traces. The operator decides what, if anything, to
do with that information.

### Step 7 — Schedule the daily cadence

Once the manual bootstrap proves the pipeline works, schedule
it. The simplest form is cron:

```bash
# Crontab line for daily 06:00 UTC bootstrap + watch handoff:
0 6 * * * cd /path/to/razorrooster && \
    RAZORROO_BOOTSTRAP_THEN_WATCH=1 \
    RAZORROO_WATCH_INTERVAL=3600 \
    make bootstrap >> data/logs/cron.log 2>&1
```

Or run the watch loop alone after the first bootstrap:

```bash
.venv/bin/razor-rooster report watch \
    --interval 3600 \
    --html data/reports/latest.html \
    --on-change \
    --summary-file 'data/logs/watch-{timestamp}.json' \
    --summary-retention 30
```

The `--on-change` flag skips cycles when nothing changed
upstream; `--summary-file` with `{timestamp}` rotation +
`--summary-retention` keeps the log directory bounded.

### Step 8 — Confirm you are fully operational

Once the cron / launchd schedule fires once cleanly, the system
is operational. The honest definition of "operational" for this
codebase:

```
[✓] .venv/bin/razor-rooster --version succeeds
[✓] .env is populated with at least the credentials you intend to use
[✓] Polymarket + Kalshi ToS acknowledgements recorded
[✓] Analytical bankroll declared
[✓] make bootstrap exits 0 and the JSON summary lands in data/logs/
[✓] data/trough.duckdb exists and contains rows
[✓] data/reports/{YYYYMMDD}.md and .html exist for today
[✓] razor-rooster ingest status shows your enabled sources fresh
[✓] razor-rooster polymarket status shows ToS acked + recent sync
[✓] razor-rooster kalshi status shows allow-list pass + recent sync
[✓] razor-rooster pattern-library list shows the seed classes
[✓] razor-rooster scan list-candidates --since YYYY-MM-DD shows recent candidates
[✓] A scheduled job (cron / launchd) runs make bootstrap on a cadence
```

Run this one command to spot-check most of them at once:

```bash
.venv/bin/razor-rooster ingest status \
    && .venv/bin/razor-rooster polymarket status \
    && .venv/bin/razor-rooster kalshi status \
    && .venv/bin/razor-rooster pattern-library list \
    && .venv/bin/razor-rooster scan list-candidates \
        --since "$(date -u -v-7d +%Y-%m-%d 2>/dev/null \
            || date -u -d '7 days ago' +%Y-%m-%d)" \
    && .venv/bin/razor-rooster report latest --db data/trough.duckdb \
        | head -40
```

If every command succeeds and the report renders with a
populated header showing today's cycle date, the platform is
fully set up, has downloaded relevant data, and is operating
functionally on the daily cadence.

### What the operator does once operational

The day-to-day flow is light. The cron / launchd job writes
new reports overnight; the operator reads the latest report
on a cadence that matches their domain interest:

1. **Read the report** — `razor-rooster report latest` or open
   today's HTML in a browser.
2. **Mark interesting comparisons** for follow-up with
   `position-engine watch <analysis_id>` so subsequent monitor
   cycles surface change-since-mark alerts.
3. **Drill into an analysis** with `position-engine show
   <analysis_id> --verbose` to read the case for the model,
   the case for the market, the sensitivity table, and the
   sizing math.
4. **Compare cycles over time** — `report compare-latest`
   for last-vs-current, or `report compare-latest --offset N`
   to step further back.
5. **Tune thresholds** — after a few weeks of cycles,
   `report measurements --kind cross_venue_spread_bps` and
   `report suggest-thresholds` show whether the configured
   thresholds match the empirical distribution and propose
   changes if they don't.
6. **Decide, outside the system**, whether and how to act on
   any of what the system surfaced. The codebase has no
   execution layer; operator decisions and any external
   actions live entirely outside the platform.

### What can go wrong (and where to look)

| Symptom | Most likely cause | Where to look |
| - | - | - |
| `ingest cycle` raises `403` from FRED | Missing or invalid `FRED_API_KEY` | `.env`, then `data/logs/{timestamp}-ingest-cycle-*.log` |
| `polymarket sync` refuses with `geo_gate` | `RAZOR_ROOSTER_JURISDICTION` resolves to a restricted jurisdiction | `config/restricted_jurisdictions.yaml` lists which ones |
| `position-engine run` errors on `no bankroll_config` | Bankroll never declared | [Step 4](#step-4--declare-the-analytical-bankroll) |
| `report generate` raises `ImperativeLanguageDetected` | Operator-supplied content (e.g., a class title) contains a forbidden phrase | `config/forbidden_phrases.yaml` lists every blocked phrase |
| Report renders empty "No comparisons surfaced this cycle" forever | `mispricing` thresholds too strict for the operator's corpus | `report measurements --kind cross_venue_spread_bps` shows the empirical distribution; consider `report suggest-thresholds` |

§14 [Troubleshooting](#14-troubleshooting) has the full list.

### Where to go next

- **[§2 Daily operating loop](#2-daily-operating-loop)** — the
  per-stage cadence diagram.
- **[§13 Common workflows](#13-common-workflows)** — recipes for
  things like "diff the last two reports", "step backward through
  history", "find the most-failed cycles in the last month".
- **[`docs/reports.md`](reports.md)** — engine internals,
  threshold tuning, calibration math.
- **[`docs/sources.md`](sources.md)** — every supported upstream
  source, its rate limits, retention semantics, and known
  quirks.

---

## 1. Architecture at a glance

Razor-Rooster is eight subsystems wired in a directed acyclic graph.
Data flows top-down; CLI commands generally run in order:

```
data_ingest          (public API ingestion)
  ↓
polymarket_connector (market data ingestion)
  ↓
pattern_library      (historical base rates + signatures)
  ↓
signal_scanner       (current-conditions probabilities)
  ↓
mispricing_detector  (model vs. market deltas)
  ↓
position_engine      (Kelly sizing analyses)
  ↓
monitor              (follow-up tracking)
  ↓
report_generator     (operator-facing document)
```

Every subsystem persists to a single DuckDB file at
`data/trough.duckdb` (override with `--db PATH` or `RAZOR_ROOSTER_DB`
env var). Each subsystem has its own table namespace and its own
schema-migration version range:

| Range | Subsystem |
| - | - |
| 1–999 | `data_ingest` |
| 1001–1999 | `polymarket_connector` |
| 2001–2999 | `pattern_library` |
| 3001–3999 | `signal_scanner` |
| 4001–4999 | `mispricing_detector` |
| 5001–5999 | `position_engine` |
| 6001–6999 | `monitor` |
| 7001+ | `report_generator` |

Migrations apply automatically when any CLI subcommand opens the store.

---

## 2. Daily operating loop

The intended cadence on the operator's hardware:

```bash
razor-rooster ingest cycle              # pull public data
razor-rooster polymarket sync           # pull market data
razor-rooster pattern-library refresh   # recompute base rates + signatures
razor-rooster scan run                  # score current conditions
razor-rooster mispricing run            # compare model to market
razor-rooster position-engine run       # produce sizing analyses
razor-rooster monitor run               # update watched-analysis follow-ups
razor-rooster report generate           # render the daily report
```

Each step depends on the prior step's output but is failure-isolated —
if `polymarket sync` fails, `ingest cycle` and earlier-state queries
still work; `report generate` will render system-health warnings
showing the gap rather than crashing.

The whole loop is also packaged as a single one-shot bootstrap:

```bash
make bootstrap          # or: bash scripts/bootstrap.sh
```

The bootstrap script auto-loads `.env` (copy `.env.example` to `.env`
and fill in what you have), detects which credentials are present,
runs every safe stage, and reports what's still operator-blocked.
Idempotent and safe to re-run; writes a structured JSON summary
under `data/logs/bootstrap-{timestamp}.json` for cron-driven
invocations. Set `RAZORROO_BOOTSTRAP_THEN_WATCH=1` to hand off to
`report watch --on-change` after bootstrap finishes.

For first-time setup, see [§13 Common workflows](#13-common-workflows).

---

## 3. Top-level CLI

```bash
razor-rooster --help
razor-rooster --version
```

The top-level entry point is a click group. Every subsystem contributes
a subcommand group:

| Group | Subsystem | Codename |
| - | - | - |
| `ingest` | `data_ingest` | The Trough |
| `polymarket` | `polymarket_connector` | The Wire |
| `pattern-library` | `pattern_library` | The Bone Pile |
| `scan` | `signal_scanner` | The Nose |
| `mispricing` | `mispricing_detector` | The Liver |
| `position-engine` | `position_engine` | The Spur |
| `monitor` | `monitor` | The Comb |
| `report` | `report_generator` | The Crow |

Most commands accept `--db PATH` to override the DuckDB store
location. The default is `data/trough.duckdb`; the env var
`RAZOR_ROOSTER_DB` is also honored.

---

## 4. `razor-rooster ingest` — data acquisition

Pulls public data from the configured upstream sources (FRED, World
Bank, ACLED, GDELT, EIA, NOAA, NRC ADAMS, regulations.gov, Federal
Register, USGS, WHO DON) into the local DuckDB store.

### `ingest init`

Apply schema migrations to a fresh database.

```bash
razor-rooster ingest init [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--db` | path | `data/trough.duckdb` | Where to write the store. |

Run this exactly once after install; subsequent commands apply
pending migrations automatically.

### `ingest status`

Print per-source freshness from the `freshness` view.

```bash
razor-rooster ingest status [--db PATH]
```

Output columns: `source_id`, `last_successful_fetch`,
`STALE`/`fresh`, `freshness_threshold_seconds`. A source is `STALE`
when current time - `last_successful_fetch` exceeds its threshold,
or when the source has never been successfully fetched.

### `ingest cycle`

Run one incremental cycle. Each source is evaluated against its
configured cadence and freshness threshold; only due sources run.
Failures in one source are isolated from others.

```bash
razor-rooster ingest cycle [--source IDS] [--db PATH] [--schedule PATH] [--quiet|--verbose]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--source` | string | (all due) | Comma-separated source ids: `--source fred,worldbank`. Forces those sources to run regardless of cadence. |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--schedule` | path | `config/ingest_schedule.yaml` | Schedule YAML. |
| `--quiet/--verbose` | flag | `--verbose` | Suppress per-source progress. |

Writes a structured JSONL log under `logs/cycles/`.

### `ingest backfill`

Pull historical data for a single source, resumable from prior state.

```bash
razor-rooster ingest backfill --source ID [--db PATH] [--restart] [--batch-size N]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--source` | string | required | Source id (e.g. `fred`, `gdelt_events`, `acled`). |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--restart` | flag | off | Ignore prior `backfill_state` and start over from the source's earliest data. Destructive of resume progress only; data already in the store remains. |
| `--batch-size` | int | `10000` | Records per page request. Lower for memory-constrained backfills, higher for throughput. |

Backfill respects per-source byte caps and the global 100 GB corpus
cap configured in `config/source_caps.yaml`. When a cap is reached the
backfill exits cleanly with status `CAP_REACHED`.

---

## 5. `razor-rooster polymarket` — market data

Read-only Polymarket API client. Markets, prices, resolutions, and
optionally trade history for watched markets. Two non-bypassable
startup gates: jurisdiction allow-check and ToS acknowledgement.

### `polymarket ack-tos`

Read the live Terms of Service, hash it, prompt for confirmation,
record the acknowledgement.

```bash
razor-rooster polymarket ack-tos [--db PATH] [--yes]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--yes` | flag | off | Skip the interactive prompt. Required for non-interactive use; you are still confirming you read the ToS. |

Stores a SHA-256 hash on the `polymarket` source row. On every
subsequent `polymarket` invocation, the gate fetches the live ToS,
re-hashes, and compares. A mismatch refuses the connector and
re-prompts.

### `polymarket status`

Print Polymarket source freshness and sync state.

```bash
razor-rooster polymarket status [--db PATH]
```

### `polymarket sync`

Run one full sync cycle: markets → prices → resolutions → watched
trades, with sector heuristic mapping.

```bash
razor-rooster polymarket sync [--db PATH] [--config PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--config` | path | `config/polymarket.yaml` | Connector config. |

### `polymarket snapshot`

Pull price snapshots only (no markets/resolutions/trades).

```bash
razor-rooster polymarket snapshot [--db PATH] [--config PATH] [--watched|--all]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--watched/--all` | flag | `--all` | Whether to snapshot only the configured watched markets or every active market. |

### `polymarket backfill-resolutions`

Pull historical resolved-contract data via Gamma API pagination.
Used once at install for the calibration backtest, then never again.

```bash
razor-rooster polymarket backfill-resolutions [--db PATH] [--restart] [--page-size N] [--config PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--restart` | flag | off | Ignore prior backfill state. |
| `--page-size` | int | `100` | Resolutions per page. |

### Watched-market management

```bash
razor-rooster polymarket watch <condition_id> [--config PATH]
razor-rooster polymarket unwatch <condition_id> [--config PATH]
razor-rooster polymarket list-watched [--config PATH]
```

Watched-market state lives in `config/polymarket.yaml` under
`sync.prices.watched_markets`. Watched markets get higher-frequency
snapshots and full trade-history pulls.

### `polymarket fetch-orderbook`

Fetch order-book depth for one market on demand.

```bash
razor-rooster polymarket fetch-orderbook <condition_id> --token-id <id> [--db PATH] [--config PATH] [--persist]
```

| Argument / Option | Type | Default | Meaning |
| - | - | - | - |
| `<condition_id>` | string | required | Polymarket market condition_id. |
| `--token-id` | string | required | Outcome token id (`yes` or `no` token). |
| `--persist` | flag | off | Write the orderbook snapshot to `polymarket_orderbook_snapshots`. Default just prints in-memory; orderbook snapshots are too large to persist by default. |

### Sector mapping triage

```bash
razor-rooster polymarket needs-review [--db PATH] [--limit N]
razor-rooster polymarket map <condition_id> <sector> [--secondary SECTOR ...] [--db PATH]
razor-rooster polymarket mapping-stats [--db PATH]
```

| Command | Argument / Option | Meaning |
| - | - | - |
| `needs-review` | `--limit N` | Cap output rows. Defaults to no cap. |
| `map` | `<sector>` | One of `public_health`, `geopolitical`, `regulatory`, `commodity`, `climate`, `infrastructure_energy`, `macroeconomic`, `cross_cutting`, or `none` to mark as out-of-scope. |
| `map` | `--secondary` | Repeatable secondary sector(s). |

Manual mappings are never overwritten by the heuristic.

---

## 5b. `razor-rooster kalshi` — Kalshi market data

Read-only Kalshi API client. Series, events, markets, prices,
settlements, optional orderbook depth, and trade history for watched
markets. Sibling to `polymarket`; shares the same DuckDB store and
the same operator-jurisdiction declaration. Two non-bypassable
startup gates: an eligibility allow-list (the inverse of
Polymarket's deny-list, so the same `OPERATOR_JURISDICTION` value
produces opposite outcomes for the two venues) and a ToS
acknowledgement gate with explicit read-only posture.

### `kalshi ack-tos`

Read the live Kalshi Terms of Service, hash it, prompt for
confirmation, record the acknowledgement under the `read_only`
posture.

```bash
razor-rooster kalshi ack-tos [--db PATH] [--config PATH] [--yes]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--config` | path | `config/kalshi.yaml` | Connector config (the gate reads `tos_url` from here). |
| `--yes` | flag | off | Skip the interactive prompt. Required for non-interactive use; you are still confirming you read the ToS. |

Stores a SHA-256 hash and the literal `'read_only'` posture on the
`kalshi` source row. Re-running on a hash mismatch refuses the
gate and re-prompts. Running on a `'trading'` posture (a v2 concept)
refuses with `ToSPostureMismatch` so v1 cannot accidentally inherit
a v2 acknowledgement.

### `kalshi status`

Print Kalshi source freshness, ToS posture, cutoff snapshot, and
sector-mapping counts.

```bash
razor-rooster kalshi status [--db PATH]
```

### `kalshi sync`

Run one full Kalshi cycle: cutoff → series → events → markets →
prices → settlements → trades.

```bash
razor-rooster kalshi sync [--db PATH] [--config PATH] [--skip-trades]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--config` | path | `config/kalshi.yaml` | Connector config. |
| `--skip-trades` | flag | off | Skip the watched-markets trades pull. Settlements still run. |

Per-stage failure isolation: an exception in one stage is captured
into the cycle report and subsequent stages still run.

### `kalshi snapshot-prices`

Run price snapshots only (no markets/settlements/trades).

```bash
razor-rooster kalshi snapshot-prices [--db PATH] [--config PATH] [--watched|--all]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--watched/--all` | flag | `--all` | Whether to snapshot only the configured watched markets or every active binary market. |

### `kalshi backfill-settlements`

One-shot historical settlement backfill. Snapshots the cutoff
first, then routes the read across `/markets?status=settled` (live)
and `/historical/markets` (older than the cutoff).

```bash
razor-rooster kalshi backfill-settlements [--db PATH] [--config PATH] [--page-size N]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--page-size` | int | `100` | Settlements per page. |

### Watched-market management

Edit the list of tickers that warrant the higher-frequency price
snapshots and the trade-history pull.

```bash
razor-rooster kalshi watch <ticker> [--config PATH]
razor-rooster kalshi unwatch <ticker> [--config PATH]
razor-rooster kalshi list-watched [--config PATH]
```

Watched-market state lives in `config/kalshi.yaml` under
`sync.prices.watched_markets`.

### `kalshi fetch-orderbook`

Fetch order-book depth for one ticker on demand. Persists YES +
derived NO levels to `kalshi_orderbook_snapshots` (Kalshi returns
YES depth on the wire; the connector mirrors `1 - yes_ask` to
produce NO bids).

```bash
razor-rooster kalshi fetch-orderbook <ticker> [--db PATH] [--config PATH] [--depth N] [--persist|--no-persist]
```

| Argument / Option | Type | Default | Meaning |
| - | - | - | - |
| `<ticker>` | string | required | Kalshi market ticker. |
| `--depth` | int | `10` | Levels per side. |
| `--persist/--no-persist` | flag | `--persist` | Write to `kalshi_orderbook_snapshots`. `--no-persist` prints in-memory only. |

### Sector mapping triage

```bash
razor-rooster kalshi needs-review [--db PATH] [--limit N]
razor-rooster kalshi map <ticker> <sector> [--secondary SECTOR ...] [--db PATH]
razor-rooster kalshi mapping-stats [--db PATH]
```

| Command | Argument / Option | Meaning |
| - | - | - |
| `needs-review` | `--limit N` | Cap output rows. Default 20. |
| `map` | `<sector>` | One of `public_health`, `geopolitical`, `regulatory`, `commodity`, `climate`, `infrastructure_energy`, `macroeconomic`, `cross_cutting`, `out_of_scope`, or `none` for explicit-null operator decision. |
| `map` | `--secondary` | Repeatable secondary sector(s). |

The `out_of_scope` value is Kalshi-specific: sports / entertainment /
daily-life markets that have no Razor-sector analogue. Operators or
the Pass-1 category-match heuristic mark them; downstream subsystems
filter them out. Manual mappings are never overwritten by the
heuristic.

### Cross-venue mapping

A single class can map to both Polymarket and Kalshi. From the
`mispricing` CLI:

```bash
razor-rooster mispricing map cpi_above_target 0xPOLY_CONDITION_ID
razor-rooster mispricing map cpi_above_target KX-CPI --venue kalshi
```

The `mispricing run` cycle then writes one comparison row per (class,
venue) pair; downstream subsystems (`position_engine`, `monitor`,
`report_generator`) carry the `venue` discriminator end-to-end and
render `(<venue>)` after each market identifier in operator-facing
output. Non-binary Kalshi markets (scalar / categorical) are deferred
to v1.2; `mispricing map --venue kalshi` refuses with a clear error.

---

## 6. `razor-rooster pattern-library` — historical patterns

Computes per-class historical base rates with credible intervals,
empirically-derived precursor signatures, and analogue feature
spaces. Operator-extensible class registry under
`src/razor_rooster/pattern_library/classes/`.

### `pattern-library list`

List registered classes.

```bash
razor-rooster pattern-library list [--sector SECTOR]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--sector` | string | (all) | Filter by domain sector. |

### `pattern-library show <class_id>`

Print one class's metadata, precursor signatures, analogue features,
and current calibration outputs.

```bash
razor-rooster pattern-library show <class_id>
```

### `pattern-library validate <class_id>`

Run registration-time validation for a class without persisting
anything. Useful when adding or modifying a class.

```bash
razor-rooster pattern-library validate <class_id>
```

### `pattern-library sync-classes`

Reconcile the database's `pl_event_classes` table with the registry.

```bash
razor-rooster pattern-library sync-classes [--db PATH]
```

### `pattern-library refresh`

Recompute all per-class outputs against the current `data_ingest`
corpus. Bumps `library_version` if the registry changed since the
last refresh.

```bash
razor-rooster pattern-library refresh [--class CLASS_ID] [--force] [--db PATH] [--max-workers N]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--class` | string | (all) | Refresh only this class. Other classes are left untouched. |
| `--force` | flag | off | Bump `library_version` with `bump_reason='code_change'` even if the registry is unchanged. Use after a Python-side change to a class definition. |
| `--db` | path | `data/trough.duckdb` | Store path. |
| `--max-workers` | int | `1` | Per-class parallelism. v1 ships with 1 to keep memory footprint predictable. |

### `pattern-library eval <class_id>`

Ad-hoc evaluation against a custom window. Does **not** persist.

```bash
razor-rooster pattern-library eval <class_id> [--window-start ISO] [--window-end ISO] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--window-start` | ISO-8601 UTC | (class default) | Start of the base-rate window. |
| `--window-end` | ISO-8601 UTC | now | End of the window. |

---

## 7. `razor-rooster scan` — current-conditions scoring

Evaluates every registered event class against current
`data_ingest` data. For each class, computes a posterior probability
estimate (Bayesian update with co-occurrence correction) with Monte
Carlo CI propagation. Flags classes whose estimate has materially
diverged from the base rate as candidate situations.

### `scan run`

Run one daily scan.

```bash
razor-rooster scan run [--class CLASS_ID] [--strict] [--max-workers N] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--class` | string | (all) | Scan only this class. Other classes are skipped. |
| `--strict` | flag | off | Abort on definition-version drift. By default, drift is flagged but the scan continues. |
| `--max-workers` | int | `4` | Per-class parallelism bound. |
| `--db` | path | `data/trough.duckdb` | Store path. |

### `scan show <scan_id>`

Print summary stats for a scan execution: counts, candidate count,
errors.

```bash
razor-rooster scan show <scan_id> [--db PATH]
```

### `scan show-trace <scan_id> <class_id>`

Print the reasoning trace for one (scan, class) pair: prior, each
precursor's current value vs. its threshold, applied likelihood
ratios, posterior, log-odds shift, warnings.

```bash
razor-rooster scan show-trace <scan_id> <class_id> [--json] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--json` | flag | off | Print raw trace JSON instead of rendered text. |

### `scan list-candidates`

List candidate scan records — those whose log-odds shift exceeded
the configured threshold.

```bash
razor-rooster scan list-candidates [--since ISO] [--sector SECTOR] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--since` | ISO-8601 | (all) | Only candidates from scans started after this point. |
| `--sector` | string | (all) | Filter by class domain sector. |

### `scan prune`

Delete scans older than a cutoff. Mandatory `--confirm` flag.

```bash
razor-rooster scan prune --before ISO --confirm [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--before` | ISO-8601 | required | Cutoff. Scans started before this time are deleted. |
| `--confirm` | flag | required | Without it, the command refuses. |

---

## 8. `razor-rooster mispricing` — model-vs-market comparison

For each `signal_scanner` posterior, finds Polymarket markets in
the same event class and computes the delta between model and market
probabilities with credible-interval-overlap analysis. Emits a
structured comparison record with a reasoning trace that presents the
case for the model and the case for the market at equal prominence.

### `mispricing map <class_id> <condition_id>`

Register an operator-curated class-to-market mapping.

```bash
razor-rooster mispricing map <class_id> <condition_id> --type TYPE [--polarity POL] [--notes TEXT] [--db PATH]
```

| Argument / Option | Type | Default | Meaning |
| - | - | - | - |
| `<class_id>` | string | required | Pattern-library class id. |
| `<condition_id>` | string | required | Polymarket market condition_id. |
| `--type` | choice | required | `direct` (1:1), `proxy` (related-but-not-identical), `aggregate` (composite of multiple markets). |
| `--polarity` | choice | `aligned` | `aligned` when YES means event happens; `inverted` when YES means event does NOT happen. |
| `--notes` | string | none | Operator commentary recorded with the mapping. |

### `mispricing unmap <mapping_id>`

Soft-delete a mapping. The row stays in the table with `removed_at` set.

```bash
razor-rooster mispricing unmap <mapping_id> [--db PATH]
```

### `mispricing list-mappings`

```bash
razor-rooster mispricing list-mappings [--class CLASS_ID] [--confidence LEVEL] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--class` | string | (all) | Filter by class_id. |
| `--confidence` | choice | (all) | `exact`, `inferred`, or `low`. |

### `mispricing run`

Run one comparison cycle: every active mapping plus auto-derived
mappings against the latest scanner posteriors. Linkage pass at the
end ties resolved markets to prior comparisons.

```bash
razor-rooster mispricing run [--class CLASS_ID] [--liquidity-floor USD] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--class` | string | (all) | Run for one class only. |
| `--liquidity-floor` | float | `10000.0` | Minimum 24h volume in USD. Markets below get `low_liquidity` flag and surfacing is suppressed. Per-sector overrides in `config/mispricing.yaml`. |

### `mispricing show <comparison_id>`

```bash
razor-rooster mispricing show <comparison_id> [--json] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--json` | flag | off | Print raw trace JSON instead of rendered text. |

### `mispricing list-comparisons`

```bash
razor-rooster mispricing list-comparisons [--surfaced-only] [--since ISO] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--surfaced-only` | flag | off | Only comparisons where `surfaced = TRUE`. |
| `--since` | ISO-8601 | (all) | Only comparisons computed at or after this timestamp. |

### `mispricing relink`

Run the linkage pass on demand. Idempotent.

```bash
razor-rooster mispricing relink [--db PATH]
```

---

## 9. `razor-rooster position-engine` — sizing analysis

For each surfaced comparison, produces a sizing analysis: Kelly
fraction, half-Kelly bound, expected value per dollar,
bankroll-survival under 1 / 3 / 5 adverse outcomes, liquidity-feasibility
clamp, time-to-resolution, invalidation criteria. Outputs use
conditional language and include a standard disclaimer block.

### `position-engine config`

Declare or update the analytical bankroll configuration. Each call
appends a new row; latest wins. Auditable.

```bash
razor-rooster position-engine config \
    --bankroll USD \
    [--max-pct PCT] \
    [--kelly-fraction FRAC] \
    [--min-edge EDGE] \
    [--no-prompt --acknowledge-analytical] \
    [--notes TEXT] \
    [--db PATH]
```

| Option | Type | Default | Bound | Meaning |
| - | - | - | - | - |
| `--bankroll` | float | required | > 0 | Analytical bankroll in USD. The system does **not** track real capital; this is sizing math only. |
| `--max-pct` | float | `0.05` | `[0, 0.25]` | Hard cap on single-position fraction of bankroll. Engine refuses higher. |
| `--kelly-fraction` | float | `0.5` | `[0, 0.5]` | Aggressiveness multiplier. `0.5` is half-Kelly (conservative ceiling). Engine refuses higher. |
| `--min-edge` | float | `0.03` | `[0, 0.5]` | Minimum \|delta\| in probability units. Below this, no sizing math is performed and the analysis is flagged `sub_threshold`. |
| `--no-prompt` | flag | off | — | Skip the interactive disclaimer-confirmation prompt. Requires `--acknowledge-analytical`. |
| `--acknowledge-analytical` | flag | off | — | Required with `--no-prompt`. Confirms you understand the bankroll figure is analytical. |
| `--notes` | string | none | — | Operator commentary recorded with the config snapshot. |

### `position-engine run`

Run one analysis cycle over surfaced comparisons.

```bash
razor-rooster position-engine run [--include-suppressed] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--include-suppressed` | flag | off | Also analyze non-surfaced comparisons. Useful during development; not the daily-cycle default. |

The expiration pass runs at the end of every cycle, transitioning
`watching` and `acted_on` watch states to `expired` for analyses
whose underlying market has resolved.

### `position-engine analyze <comparison_id>`

Run the analyzer for one comparison ad-hoc.

```bash
razor-rooster position-engine analyze <comparison_id> [--db PATH]
```

### `position-engine show <analysis_id>`

```bash
razor-rooster position-engine show <analysis_id> [--verbose] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--verbose` | flag | off | Include the sensitivity-analysis section (model_p ±10% / ±20%). |

### Watch state transitions

```bash
razor-rooster position-engine watch <analysis_id> [--note TEXT] [--db PATH]
razor-rooster position-engine acted-on <analysis_id> [--note TEXT] [--db PATH]
razor-rooster position-engine dismiss <analysis_id> [--reason TEXT] [--db PATH]
```

| Command | Meaning |
| - | - |
| `watch` | Mark `watching`. Operator wants to keep an eye on this. |
| `acted-on` | Mark `acted_on`. Operator declares they took some real-world action. The system itself doesn't know what the action was. |
| `dismiss` | Mark `dismissed`. Terminal state for analyses the operator has decided not to track. |

`watching` and `acted_on` auto-expire to `expired` when the
underlying market resolves. `dismissed` and `expired` are terminal.

### `position-engine list`

List analyses by watch state. Provide exactly one state flag.

```bash
razor-rooster position-engine list (--watched | --acted-on | --dismissed | --expired) [--db PATH]
```

---

## 10. `razor-rooster monitor` — watched-analysis follow-ups

Reads watched and acted-on analyses, snapshots current upstream
state (latest scan, latest market price, resolution status),
classifies the change since the analysis was produced, and writes
one *follow-up* per analysis with a ranked alert.

### `monitor run`

Run one cycle over all `watching` and `acted_on` analyses.

```bash
razor-rooster monitor run [--db PATH]
```

Output prints aggregate counts: total follow-ups, follow-ups with
alerts, alerts by tier, resolutions detected, expirations written.

When a resolution is detected, the cycle calls
`position_engine.run_expiration_pass` so any active watch state on
the resolved analysis transitions to `expired` immediately.

### `monitor evaluate <analysis_id>`

Evaluate one analysis ad-hoc and persist the follow-up.

```bash
razor-rooster monitor evaluate <analysis_id> [--db PATH]
```

### `monitor show <follow_up_id>`

```bash
razor-rooster monitor show <follow_up_id> [--db PATH]
```

Prints the reasoning text and any operator notes.

### `monitor list-alerts`

List follow-ups with alerts, ranked by tier priority then recency.

```bash
razor-rooster monitor list-alerts [--tier TIER] [--since ISO] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--tier` | choice | (all tiers) | One of `resolution`, `invalidation_triggered`, `material_shift`, `precursor_shift`, `time_decay`. |
| `--since` | ISO-8601 | (all) | Only alerts computed at or after this timestamp. |

The five tiers in priority order:

| Tier | Triggered when |
| - | - |
| `resolution` | The underlying market has resolved. Triggers automatic watch-state expiration. |
| `invalidation_triggered` | At least one of the analysis's stated invalidation criteria has fired. |
| `material_shift` | Model or market probability has moved by a `material` or `major` band since analysis time. |
| `precursor_shift` | At least one underlying precursor variable has crossed its threshold since analysis time. |
| `time_decay` | Days-to-resolution is at or below `time_decay_alert_days` (default 7). |

### `monitor trajectory <analysis_id>`

Print every follow-up for an analysis ordered chronologically.

```bash
razor-rooster monitor trajectory <analysis_id> [--db PATH]
```

### `monitor note <follow_up_id> "TEXT"`

Append an operator note to a follow-up.

```bash
razor-rooster monitor note <follow_up_id> "Reviewed; deciding to hold." [--db PATH]
```

Notes are append-only. They appear in `show` output below the
reasoning text.

---

## 11. `razor-rooster report` — operator-facing document

Assembles outputs from every upstream subsystem into one
structured report with a fixed top-to-bottom layout: header → at
a glance (opt-in) → system health → recent threshold changes
(opt-in) → surfaced comparisons → cross-venue disagreements →
active watched → calibration log → reliability diagram (opt-in)
→ watchlist → footer with disclaimer.

Output formats:

- **Terminal** (default): plain ASCII text, piped to stdout
  unless `--quiet`.
- **Markdown** (`--markdown PATH`): GFM with tables and code
  blocks.
- **HTML** (`--html PATH`, new in v0.44.0): self-contained HTML
  document. Inline CSS only, no external assets, no JavaScript.
  Renders fine offline; supports light/dark mode via
  `prefers-color-scheme`.

### `report generate`

```bash
razor-rooster report generate [--since ISO] [--markdown PATH] [--html PATH] [--quiet] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--since` | ISO-8601 | (prior report's `generated_at`, or 24h ago on first run) | Cycle-window start. |
| `--markdown` | path | none | Optional path to write a parallel markdown file. The parent directory is created if missing. |
| `--html` | path | none | Optional path to write a parallel HTML file. Self-contained: inline CSS only, no external assets, no JavaScript. Renders fine offline; supports light/dark mode. New in v0.44.0. |
| `--quiet` | flag | off | Skip terminal output. Useful when you only want the markdown or HTML export. |

Behavior:

- Per-section failure isolation: a broken section becomes a
  `section error: <reason>` placeholder; other sections still render.
- The shared imperative-language linter runs on terminal,
  markdown, and HTML outputs before persistence. If the linter
  rejects, the report is **not** persisted; the next run
  re-attempts.
- Persists full rendered text (terminal + optional markdown +
  optional HTML) to `report_log` plus a SHA-256 hash of the
  disclaimer text used.

### `report list`

```bash
razor-rooster report list [--since ISO] [--limit N] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--since` | ISO-8601 | (all) | Only reports generated at or after this timestamp. |
| `--limit` | int | `20` | Maximum rows. |

### `report show <report_id>`

```bash
razor-rooster report show <report_id> [--db PATH]
```

Prints the stored terminal text of a previous report.

### `report latest`

```bash
razor-rooster report latest [--db PATH]
```

Prints the most-recent report's terminal text.

### `report measurements`

New in v0.40.0. Inspect the per-cycle threshold-distribution
measurements recorded by the generator. Helps you decide whether
your configured thresholds in `config/report.yaml` are well-
calibrated for the corpus.

```bash
razor-rooster report measurements [--kind KIND] [--since ISO] [--limit N] [--json] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--kind` | string | `cross_venue_spread_bps` | Measurement kind to inspect. v0.41.0 ships three: `cross_venue_spread_bps`, `single_venue_dominance_share`, `brier_per_sector`. |
| `--since` | ISO-8601 | (all) | Only show measurements at or after this timestamp. |
| `--limit` | int | `20` | Maximum rows to display. |
| `--json` | flag | off | Print the raw distribution payload one record per line. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Plain output shows, per cycle:

- `n` (number of observations) and `above_threshold` (count of
  observations strictly greater than the configured threshold).
- `threshold` (the configured global threshold value at the time
  of the measurement).
- `min` / `max` / `mean` / `stddev`.
- Percentiles `p10` / `p25` / `p50` / `p75` / `p90` / `p95` / `p99`.

If most of your cycles have many observations comfortably below
the threshold, your threshold is conservative; if most are
clustered just above the threshold, your section will be noisy
and the operator may want to raise it. The CLI doesn't tell you which
direction to move; you read the distribution and decide.

### `report explain-thresholds`

New in v0.41.0. For each shipped measurement kind, prints a
descriptive summary of where the configured threshold sits in the
most recent cycle's distribution.

```bash
razor-rooster report explain-thresholds [--kind KIND] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--kind` | string | (every shipped kind) | Scope to one kind: `cross_venue_spread_bps`, `single_venue_dominance_share`, or `brier_per_sector`. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Output per kind: latest cycle (timestamp + report_id), configured
threshold, n / above_threshold, percentile-rank line ("the
configured threshold sits at the p65 of this cycle's
distribution"), and the seven recorded percentile cuts so you
can see the shape.

The CLI is strictly descriptive — it never tells you to change
a threshold.

### `report suggest-thresholds`

New in v0.41.0. Reads the most recent N cycles' measurements,
averages the recorded percentile cuts across cycles, and prints
suggested threshold values that would land at each target
percentile. Useful for "what threshold would put me at p70 of
my corpus?" investigations.

```bash
razor-rooster report suggest-thresholds [--kind KIND] [--lookback-cycles N] [--target-pct PCT [...]] [--db PATH]
razor-rooster report suggest-thresholds --kind KIND --target-pct PCT --apply [--yes] [--diff] [--note TEXT] [--config PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--kind` | string | (every shipped kind) | Scope to one kind. Required when `--apply` is set. |
| `--lookback-cycles` | int | `30` | Number of recent cycles to average. |
| `--target-pct` | float | `0.50, 0.70, 0.90` | Target percentile(s) to suggest values for. Repeatable. Range [0.0, 1.0]. With `--apply`, exactly one. |
| `--apply` | flag | off | Write the suggested value back to `config/report.yaml`. New in v0.42.0. |
| `--yes` | flag | off | Skip the confirmation prompt when `--apply` is set. |
| `--diff` | flag | off | When `--apply` is set, print a unified-diff-style preview of the YAML change before the prompt. New in v0.43.0. |
| `--note` | string | (none) | Free-text commentary recorded with the tuning-log entry. New in v0.43.0. |
| `--config` | path | `config/report.yaml` | Path to the YAML when `--apply` is set. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Output per kind (read path): cycles inspected, cycles with data,
the current configured threshold, a stability line (v0.42.0 —
"stable" or "unstable" with the coefficient of variation), and
one line per target percentile with the suggested value.

The read path is strictly descriptive — the output reports what
each percentile cut would mean if the operator chose to use it;
it never directs the operator to apply a suggestion.

The `--apply` path (v0.42.0) is reversible:

- Saves a timestamped backup of `config/report.yaml` to
  `config/report.yaml.bak.<ISO timestamp>` before writing.
- Prompts for confirmation unless `--yes` is set. The prompt is
  descriptive: "Apply suggested value X to thresholds for
  '<kind>'?".
- Refuses postures that would silence guard rails (e.g.
  `--target-pct 1.0` for `single_venue_dominance_share` would
  effectively turn off the warning entirely).
- When the underlying distribution is flagged unstable, the
  prompt prepends a short descriptive note so operators don't
  tune to noise. Operators can still apply the suggestion — the
  note is descriptive, not blocking.
- On a write failure, restores from backup so the operator
  never ends up with a half-written config.

The `--diff` flag (v0.43.0) prints a unified-diff preview before
the confirmation prompt:

```text
--- config/report.yaml
+++ config/report.yaml (proposed)
@@ thresholds.cross_venue_spread_bps @@
- thresholds.cross_venue_spread_bps: 500
+ thresholds.cross_venue_spread_bps: 750
```

The `--note` flag (v0.43.0) attaches free-text commentary to the
tuning-log entry, so retroactive review tells you not just what
changed but why.

To revert an applied change, copy the timestamped backup back
over the live config:

```bash
cp config/report.yaml.bak.20260516T143000Z config/report.yaml
```

### `report prune-measurements`

New in v0.42.0. Delete old `report_threshold_measurements` rows
when disk pressure becomes a concern, or when you want to reset
the measurement history before a tuning cycle. Default retention
is unbounded — most operators can leave the table alone.

```bash
razor-rooster report prune-measurements [--before ISO] [--keep-last N] [--kind KIND] --confirm [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--before` | ISO-8601 | (none) | Delete measurements older than this point. |
| `--keep-last` | int | (none) | Keep only the N most recent measurements per kind. Pass 0 to delete every row for the targeted kind. |
| `--kind` | string | (all) | Optional measurement kind to scope the prune to. |
| `--confirm` | flag | required | Without it, the command refuses. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Either `--before` or `--keep-last` (or both) must be set. The
two strategies stack: rows are deleted when *either* condition
fires.

Examples:

```bash
# Delete everything older than May 1.
razor-rooster report prune-measurements --before 2026-05-01T00:00:00 --confirm

# Keep only the newest 90 measurements per kind.
razor-rooster report prune-measurements --keep-last 90 --confirm

# Reset history for one kind only.
razor-rooster report prune-measurements --kind cross_venue_spread_bps --keep-last 0 --confirm
```

For hands-off retention, see the `auto_prune:` block in
`config/report.yaml` (added in v0.43.0). When `enabled: true`,
every successful report cycle prunes measurements that match
the configured strategy.

### `report tuning-log`

New in v0.43.0. Lists historical
`razor-rooster report suggest-thresholds --apply` writes so
you can review retroactively how thresholds drifted.

```bash
razor-rooster report tuning-log [--kind KIND] [--since ISO] [--limit N] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--kind` | string | (all) | Optional measurement kind to scope the log to. |
| `--since` | ISO-8601 | (all) | Only show entries at or after this timestamp. |
| `--limit` | int | `20` | Maximum rows to display. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Output per entry: applied_at + kind + knob, previous → new
values, target percentile, backup file path, and any operator
note attached via `--note` at apply time.

### `report tuning-log-undo`

New in v0.44.0. Restores `config/report.yaml` from a tuning-log
entry's recorded backup. Itself reversible — saves a fresh
pre-undo backup of the current config before overwriting.

```bash
razor-rooster report tuning-log-undo <log_id> [--yes] [--config PATH] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `<log_id>` | string | required | The tuning-log entry to undo. Find it via `razor-rooster report tuning-log`. |
| `--yes` | flag | off | Skip the confirmation prompt. |
| `--config` | path | `config/report.yaml` | Path to the YAML to restore. |
| `--db` | path | `data/trough.duckdb` | Store path. |

The undo prompt is descriptive: "Undo tuning-log entry X?". On
success, the CLI prints which file was restored and where the
pre-undo backup was saved. A new tuning-log entry is recorded
describing the undo with a `note` referencing the original
`log_id`.

To undo the undo, run `tuning-log-undo` again with the new
log_id (visible in `razor-rooster report tuning-log` output).

### `report compare`

New in v0.45.0. Diffs two persisted reports by ID. Useful for
"what changed since last week?" investigations.

```bash
razor-rooster report compare <report_id_a> <report_id_b> [--diff/--no-diff] [--diff-lines N] [--html PATH] [--word-diff/--no-word-diff] [--side-by-side/--no-side-by-side] [--quick-jump/--no-quick-jump] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `<report_id_a>` | string | required | Older report ID. |
| `<report_id_b>` | string | required | Newer report ID. |
| `--diff/--no-diff` | flag | `--diff` | Include the unified terminal-text diff. |
| `--diff-lines` | int | `200` | Maximum diff lines to print. |
| `--html` | path | none | Write a self-contained two-column HTML view of the comparison (added v0.46.0). |
| `--word-diff/--no-word-diff` | flag | `--word-diff` | When set (default), paired deletion/addition lines in the HTML unified-diff panel get word-level highlights (added v0.49.0). |
| `--side-by-side/--no-side-by-side` | flag | `--side-by-side` | When set (default), the HTML page includes a two-column side-by-side terminal-text panel (added v0.49.0). |
| `--quick-jump/--no-quick-jump` | flag | `--quick-jump` | When set (default), the HTML header includes a nav block with anchor links (added v0.51.0). Section ids remain in place either way. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Output covers: time between the two reports, library-version
drift, disclaimer-hash drift, sections added/removed, terminal-
text length delta, and an optional unified-diff preview. The
output is strictly descriptive — observed differences only, no
ranking or interpretation.

`--html PATH` (added in v0.46.0) writes a self-contained HTML
document with a metadata table that highlights changed fields,
a sections list with `added`/`removed` styling, and a two-column
side-by-side panel of the rendered terminal text from each
report. Inline CSS only; no external assets, no JavaScript. The
output passes through the imperative-language linter before
being written to disk. Parent directories are created on demand.

The HTML header now includes a quick-jump nav (added v0.50.0)
with inline `href="#..."` anchor links to each panel; each
section carries a stable `id="..."` so URL fragments deep-link
to the matching panel.

A fourth panel (added v0.47.0) renders the unified terminal-text
diff with line-level color highlighting: green for additions, red
for deletions, accent-color for `@@` hunk headers, muted for
`---`/`+++` file headers. The same `--diff-lines N` flag that
caps the terminal preview also caps this panel; truncation
emits a "more line(s) truncated" footer.

Paired deletion/addition runs of equal length now get
word-level highlighting (added v0.48.0): unchanged tokens
render as plain text inside the line; replaced tokens get an
inline `<span class="word-del">` (red tint with strike-through)
or `<span class="word-add">` (green tint). Unequal-length
runs fall back to whole-line styling. Pass `--no-word-diff`
(added v0.49.0) to suppress word-level highlighting (helpful
on narrow viewports where the wrap obscures the line boundary).

Pass `--no-side-by-side` (added v0.49.0) to suppress the
two-column terminal-text panel. The two flags compose:
`--no-side-by-side --no-word-diff` produces the most compact
view (metadata table, sections list, and line-level unified
diff only).

Pass `--no-quick-jump` (added v0.51.0) to additionally suppress
the nav block from the header. Section `id="..."` attributes
remain so manual deep-linking still works.

The side-by-side panel routes each report's terminal text
through an ANSI-to-HTML translator (added v0.47.0). Today's
terminal renderer doesn't emit ANSI, so the translator is a
no-op. It activates if a future renderer change emits ANSI or
if external content with ANSI is pasted in. Eight standard +
eight bright foreground colors plus bold / dim / italic /
underline are supported.

### `report compare-latest`

New in v0.50.0. Convenience wrapper over `report compare`:
resolves the two newest persisted reports' ids (newer is `b`,
older is `a`) and forwards the rendering flags to the compare
path.

```bash
razor-rooster report compare-latest [--offset N] [--diff/--no-diff] [--diff-lines N] [--html PATH] [--word-diff/--no-word-diff] [--side-by-side/--no-side-by-side] [--quick-jump/--no-quick-jump] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--offset` | int | `0` | Step backward through history (added v0.51.0). 0 diffs reports `[0]` and `[1]`; 1 diffs `[1]` and `[2]`; and so on. |
| `--diff/--no-diff` | flag | `--diff` | Include the unified terminal-text diff. |
| `--diff-lines` | int | `200` | Maximum diff lines to print. |
| `--html` | path | none | Write a self-contained HTML view. |
| `--word-diff/--no-word-diff` | flag | `--word-diff` | Word-level highlights inside paired del/add lines. |
| `--side-by-side/--no-side-by-side` | flag | `--side-by-side` | Two-column terminal-text panel in the HTML. |
| `--quick-jump/--no-quick-jump` | flag | `--quick-jump` | Nav block in the HTML header (added v0.51.0). |
| `--db` | path | `data/trough.duckdb` | Store path. |

Refuses with a clear message when fewer than `offset + 2`
reports are persisted. Echoes
`comparing latest pair: a=<id>  b=<id>` before running the
diff so the operator sees which pair was selected.

### `report watch`

New in v0.45.0. Runs `report generate` on a fixed cadence in a
loop. Pure ergonomics — same engine, just looped.

```bash
razor-rooster report watch [--interval SEC] [--html PATH] [--markdown PATH] [--once] [--max-cycles N] [--on-change] [--summary-file PATH] [--summary-retention DAYS] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--interval` | int | `3600` | Seconds between cycles. Range [60, 86400] (1 minute to 24 hours). |
| `--html` | path | none | Path to overwrite with the HTML rendering on each cycle. Browsers can keep the file open and refresh. |
| `--markdown` | path | none | Same for markdown. |
| `--once` | flag | off | Run a single cycle and exit. |
| `--max-cycles` | int | none | Optional cap on cycle count before exit. |
| `--on-change` | flag | off | Skip the `generate()` call when upstream tables haven't changed since the prior cycle (added v0.46.0). |
| `--summary-file` | path | none | Write the exit-summary block to a file. Suffix `.json` produces a single JSON object; otherwise plain text matching stdout (added v0.49.0). May contain `{timestamp}` for rotation (added v0.50.0). |
| `--summary-retention` | int | none | After writing the new summary, prune older summaries matching the same template (added v0.51.0). Range `[1, 365]`. Requires `--summary-file` with `{timestamp}`. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Loops until interrupted (Ctrl+C). Per-cycle failures are logged
but don't terminate the loop — the next interval re-attempts.

Typical use: pair `--interval 3600` with `--html data/today.html`
and keep a browser tab pointed at the file. The page updates on
each refresh.

`--on-change` (added in v0.46.0) skips the `generate()` call
when the upstream fingerprint matches the prior cycle's. The
fingerprint covers the latest IDs in `scan_summaries`,
`comparisons`, `follow_ups`, and `threshold_tuning_log`. The
first cycle always runs (it seeds the baseline). Skipped cycles
count toward `--max-cycles` and the exit summary reports the
skip count when nonzero. Useful for long-running watch loops
where most intervals would re-render an unchanged report.

When the loop transitions skip→run after one or more skipped
cycles (added v0.47.0), the next non-skipped cycle's log line
includes a parenthesized note like `(resume after 3 skipped:
tuning_log changed)` naming which fingerprint field(s) drove
the resume.

When the watch loop exits (Ctrl+C / `--max-cycles` / `--once`),
the summary block (extended v0.48.0) reports the average cycle
duration, the count of failed cycles, the distinct fingerprint
fields encountered as changed across the loop (when at least
one change occurred), and the total skip time (when at least
one cycle was skipped):

```
Watch exited after 5 cycle(s) (3 skipped).
  cycles failed: 0  avg cycle duration: 0.214s
  fingerprint fields changed during loop: comparison, tuning_log
  total skip time: ~10800s (3 cycle(s) x 3600s interval)
```

Pass `--summary-file PATH` (added v0.49.0) to also write the
summary block to disk. Suffix-driven dispatch: paths ending in
`.json` get a single `{"kind": "watch_summary", ...}` JSON
object (machine-readable for cron-driven invocations); other
paths get plain text matching the stdout format. Parent
directories are created on demand.

The summary path may include a `{timestamp}` placeholder
(added v0.50.0) that expands to a UTC ISO 8601 timestamp
(filesystem-safe — colons replaced with hyphens) so successive
cron invocations produce discrete files instead of overwriting
one another. Example:
`--summary-file logs/watch-{timestamp}.json` writes
`logs/watch-2026-05-16T14-30-00+00-00.json`. The CLI prints
`summary written to: <resolved>` when the path was rewritten.

Pass `--summary-retention DAYS` (added v0.51.0) to delete older
summaries from the same directory after writing the new file.
Strict on filename pattern: only files whose names match the
same template glob are candidates. The just-written file is
always kept regardless of mtime. Useful for keeping cron-driven
summary directories from growing without bound.

### `report digest`

New in v0.46.0. Prints a one-line-per-report digest of recent
reports. Strictly descriptive.

```bash
razor-rooster report digest [--days N | --since ISO] [--report-id PREFIX] [--sort-by FIELD] [--sort-direction DIR] [--top N] [--json] [--db PATH]
```

| Option | Type | Default | Meaning |
| - | - | - | - |
| `--days` | int | `7` | Window in days. Range `[1, 365]`. Mutually exclusive with `--since`. |
| `--since` | ISO 8601 string | none | Window starts at this timestamp (added v0.48.0). Naive timestamps interpreted as UTC. Mutually exclusive with `--days`. |
| `--report-id` | string | none | Prefix filter on `report_id` (added v0.49.0). E.g. `--report-id rpt-2026-05` to scope to May 2026 cycles. Combines with `--days`/`--since`/`--json`. |
| `--sort-by` | choice | `generated_at` | Sort field (added v0.50.0). One of `generated_at`, `sections_failed`, `terminal_chars`. |
| `--sort-direction` | choice | `desc` | Sort direction (added v0.50.0). One of `asc`, `desc`. |
| `--top` | int | none | Limit the per-row listing to the first N reports after sorting (added v0.51.0). Range `[1, 1000]`. Aggregate header reports totals over the full window. |
| `--json` | flag | off | Emit JSON Lines output (added v0.48.0). One report object per line followed by a single aggregate object. |
| `--db` | path | `data/trough.duckdb` | Store path. |

Default terminal output:

```
reports in the last 7 day(s): 3

2026-05-16T14:00:00+00:00  rpt-2026-05-16  sections=8/8  failed=0  terminal_chars=12345 [md, html]
2026-05-15T14:00:00+00:00  rpt-2026-05-15  sections=8/8  failed=0  terminal_chars=11987 [md]
2026-05-14T14:00:00+00:00  rpt-2026-05-14  sections=8/8  failed=1  terminal_chars=11543
```

Each line shows the generated_at timestamp (ISO 8601), the
report_id, the sections-rendered/sections-enabled count, the
failed-section count, the terminal-text length in characters,
and bracketed `[md]`/`[html]`/`[md, html]` markers when the
underlying ReportRecord persisted those output paths. Reports
appear in newest-first order. The digest reports observed
activity over the window without ranking or recommending.

A small aggregate header sits above the per-row listing (added
v0.47.0):

```
reports in the last 7 day(s): 3
  cycles with failures: 1  with markdown: 2  with html: 1
  avg sections rendered: 2.0  avg terminal chars: 1000
```

The header gives at-a-glance answers to questions like "is the
generator producing clean cycles?" and "how often does the
operator request markdown / HTML exports?" without scanning
each row.

`--json` mode (added v0.48.0) emits JSON Lines: one
`{"kind": "report", ...}` object per line in newest-first
order, followed by one final `{"kind": "aggregate", ...}` line:

```
{"generated_at": "2026-05-16T14:00:00+00:00", "html_path": null, "kind": "report", "markdown_path": "/tmp/r.md", "report_id": "rpt-2026-05-16", "sections_enabled": 8, "sections_failed": 0, "sections_rendered": 8, "terminal_chars": 12345}
{"avg_sections_rendered": 8.0, "avg_terminal_chars": 11958.3, "cycles_with_failures": 1, "cycles_with_html": 1, "cycles_with_markdown": 2, "kind": "aggregate", "report_count": 3, "since": "2026-05-09T14:00:00+00:00", "window": "in the last 7 day(s)"}
```

Each line parses standalone, so `jq`, `head`, `awk`, and other
unix tooling work without preprocessing.

`--since ISO` (added v0.48.0) replaces `--days` for
operator-defined windows. The two flags are mutually exclusive.

`--report-id PREFIX` (added v0.49.0) further filters the listing
to reports whose `report_id` starts with `PREFIX`. Combines
cleanly with `--days`/`--since`/`--json`. The JSON aggregate
object carries the prefix in its `report_id_prefix` field
(or `null` when the flag isn't set).

`--sort-by FIELD` and `--sort-direction DIR` (added v0.50.0)
re-order the per-row listing. Useful for finding the longest
reports (`--sort-by terminal_chars`) or the most-failed cycles
(`--sort-by sections_failed`). The default
(`generated_at desc`) preserves the existing newest-first
ordering.

`--top N` (added v0.51.0) caps the per-row listing to the first
N reports after sorting. The aggregate header still reports
totals over the full unsliced window so the operator's
selection remains accurate. Pairs naturally with `--sort-by`
to surface only the top few most-failed or longest cycles.

### At-a-glance section (opt-in)

New in v0.45.0. Sits at the very top of the body. Lifts the top
item from each major section's already-ordered list and emits a
short structured summary so the operator sees a navigation view
at a glance before reading the full report.

To enable, add `at_a_glance` to `enabled_sections` in
`config/report.yaml`:

```yaml
enabled_sections:
  - at_a_glance
  - system_health
  - surfaced
  - cross_venue
  - watched
  - calibration
  - watchlist
```

Output is structured `label: value` lines, not prose. Example:

```
AT A GLANCE

  top cross-venue spread: CPI 2.5% at 1500 bps (kalshi vs polymarket)
  top surfaced comparison: Top class (polymarket) delta +0.123
  top watched alert: Watched class at tier material_shift
  top miscalibrated sector: geopolitical brier 0.32
```

The section does not rank items independently — it pulls the
first element from each section's existing ordered list. The
shared imperative-language linter has nine new editorial-flavor
phrases added in v0.45.0 to defend against drift entering this
section via operator-supplied data.

### Multi-venue features in the report

When mappings span both Polymarket and Kalshi, four pieces of the
report behave differently:

**Cross-venue disagreement section (between `surfaced` and
`watched`).** For each event class with comparisons on more than one
venue this cycle, the section reports the spread between the
highest and lowest market-implied probabilities. Items appear when
the spread is at least 5 percentage points (500 bps); items are
ordered by spread descending. Each item shows a per-venue
breakdown plus a liquidity-weighted consensus probability (the
volume-weighted mean of venue prices); when no per-venue volume is
available, the consensus falls back to an unweighted mean. The
section is purely descriptive. The disagreement is informative;
the renderer does not say which venue is "right".

**Single-venue dominance warning (within `surfaced`).** When a
class is mapped on multiple venues but one venue holds strictly
greater than 80% of the combined 24h volume across them, every
surfaced comparison for that class gets a `single_venue_dominance`
warning appended to its warnings list. This says: the cross-venue
spread is real, but the smaller-venue side may be too thin to
trust.

**Per-sector Brier scores (within `calibration`).** A rolling
90-day window aggregates resolutions by `domain_sector`. Sectors
with Brier above 0.25 get a `miscalibrated` flag and the renderer
labels them so you can weight that sector's outputs lower in your
own thinking. Markdown emits a per-sector table with sector,
Brier, sample count, window, and miscalibration status. Sectors
sorted alphabetically. Invalidated resolutions are excluded.
Crucially, the calibration section now renders even when the
report's window has no fresh resolutions, as long as Brier data
exists in the rolling window — you still get the sector summary on
quiet days.

**Consensus column (markdown only).** The cross-venue markdown
table includes a `Consensus` column showing the liquidity-weighted
or unweighted-fallback consensus probability per class, alongside
the per-venue cells.

---

## 12. Configuration reference

All config files live in `config/` at the workspace root. Most are
operator-tunable without code changes. Each subsystem reads its
config at every invocation, so edits take effect on the next cycle.

### `config/ingest_schedule.yaml`

Per-source cadence and freshness thresholds. Each connector entry
specifies `cadence`, `enabled`, `freshness_threshold_seconds`. See
`docs/sources.md` for the per-source defaults.

### `config/source_caps.yaml`

Disk-budget enforcement.

| Key | Default | Meaning |
| - | - | - |
| `global.max_corpus_bytes` | `100 GB` | Global cap on the DuckDB store. |
| `global.warn_at_pct` | `80.0` | Emit warning when corpus is this percentage full. |
| `global.pause_backfill_at_pct` | `95.0` | Refuse new backfill batches above this percentage. |
| `per_source.<id>.max_backfill_years` | varies | Per-source historical depth cap. |
| `per_source.<id>.max_bytes` | varies | Per-source disk-footprint cap. |

GDELT events is the highest-volume source; capped to 5 years and 30 GB.

### `config/restricted_jurisdictions.yaml`

Polymarket geo-block list. The connector compares
`OPERATOR_JURISDICTION` (env var or `config/operator.yaml`) against
this list and refuses to start on a match. ISO 3166-1 alpha-2
country codes; matching is case-insensitive. **Operator
responsibility** to keep current with Polymarket's published
restrictions.

### `config/polymarket.yaml`

Polymarket connector tuning.

| Key | Default | Meaning |
| - | - | - |
| `sync.markets.cadence` | `daily` | Markets sync cadence. |
| `sync.markets.time_of_day` | `08:30` | Local-time hint for the markets sync. |
| `sync.prices.default_cadence` | `hourly` | Default price-snapshot cadence. |
| `sync.prices.minimum_interval_seconds` | `60` | Floor on per-market snapshot frequency. Protects rate budget. |
| `sync.prices.watched_markets` | `[]` | List of `condition_id`s for higher-frequency snapshots and trade-history pulls. Edit via the `polymarket watch` command. |
| `sync.resolutions.cadence` | `daily` | Resolutions sync cadence. |
| `sync.trades.cadence` | `daily` | Trade-history pull cadence (only runs against watched markets). |
| `rate_limit.bucket_capacity` | `50` | Token-bucket capacity. 50% headroom under Polymarket's 100 req/sec cap. |
| `rate_limit.refill_per_second` | `50.0` | Token refill rate per second. |
| `rate_limit.backoff_base_seconds` | `1.0` | Initial backoff on rate-limit error. |
| `rate_limit.backoff_max_seconds` | `60.0` | Backoff ceiling. |
| `rate_limit.max_retries` | `5` | Max retry attempts before surfacing failure. |
| `freshness.markets_threshold_seconds` | `172800` (48h) | Stale threshold for markets. |
| `freshness.prices_threshold_seconds` | `21600` (6h) | Stale threshold for prices. |
| `freshness.resolutions_threshold_seconds` | `172800` (48h) | Stale threshold for resolutions. |
| `sector_mapping.heuristic_version` | `1` | Bump when changing the keyword heuristic. |
| `sector_mapping.keywords_file` | `config/sector_keywords.yaml` | Path to the operator-extensible keyword catalog. |

### `config/sector_keywords.yaml`

Per-sector keyword catalog used by Polymarket's auto-mapping
heuristic. Six sector buckets (`public_health`, `geopolitical`,
`regulatory`, `commodity`, `climate`, `infrastructure_energy`,
`macroeconomic`, `cross_cutting`) with lists of trigger words.
Operator-extensible.

### `config/kalshi.yaml`

Kalshi connector tuning.

| Key | Default | Meaning |
| - | - | - |
| `base_url` | `https://external-api.kalshi.com/trade-api/v2` | Kalshi production base URL. v1 is production-only; the `demo.kalshi` URL is reserved for v2 trading work. |
| `tier` | `Basic` | API tier. One of `Basic`, `Advanced`, `Premier`, `Paragon`, `Prime`. Drives the rate limiter's bucket capacity. |
| `tos_url` | `https://kalshi.com/docs/kalshi-terms-of-service` | URL the ToS gate fetches and hashes. Operator-updateable when Kalshi revises the Terms. |
| `sync.cutoff.cadence` | `every_cycle` | `/historical/cutoff` snapshotted at every cycle's start. Single-row replace. |
| `sync.series.cadence` | `daily` | Series catalogue sync. |
| `sync.events.cadence` | `daily` | Events catalogue sync. |
| `sync.markets.cadence` | `daily` | Markets catalogue sync. |
| `sync.prices.default_cadence` | `every_30min` | Price-snapshot cadence (tighter than Polymarket's hourly because Kalshi's snapshot endpoint is cheap). |
| `sync.prices.minimum_interval_seconds` | `60` | Floor on per-market snapshot frequency. |
| `sync.prices.watched_markets` | `[]` | List of tickers for higher-frequency snapshots and trade-history pulls. Edit via `kalshi watch`. |
| `sync.settlements.cadence` | `daily` | Settlement reconcile (live + historical). |
| `sync.trades.cadence` | `daily` | Trade-history pull cadence (only runs against watched markets). |
| `rate_limit.tier_budget_tokens_per_sec.<tier>` | `Basic: 200`, `Advanced: 300`, `Premier: 1000`, `Paragon: 2000`, `Prime: 4000` | Per-tier read-token budgets. The limiter scales bucket capacity + refill to `headroom_pct * budget`. |
| `rate_limit.headroom_pct` | `0.5` | Target 50% of tier budget. Lower for stricter conservation; higher to push closer to the cap. |
| `rate_limit.backoff_base_seconds` | `1.0` | Initial backoff on retryable response. |
| `rate_limit.backoff_max_seconds` | `60.0` | Backoff ceiling. |
| `rate_limit.max_retries` | `5` | Max retry attempts. The retry helper deliberately does not honor `Retry-After` headers (Kalshi 429 doesn't include them). |
| `freshness.markets_threshold_seconds` | `172800` (48h) | Stale threshold for markets. |
| `freshness.prices_threshold_seconds` | `10800` (3h) | Stale threshold for prices (tighter than Polymarket's 6h since cadence is 30min). |
| `freshness.settlements_threshold_seconds` | `172800` (48h) | Stale threshold for settlements. |
| `sector_mapping.heuristic_version` | `1` | Bump when changing the keyword heuristic. |
| `sector_mapping.keywords_file` | `config/kalshi_sector_keywords.yaml` | Path to the operator-extensible keyword catalog. |

### `config/kalshi_sector_keywords.yaml`

Per-sector keyword catalog for Kalshi's auto-mapping heuristic.
Same eight Razor sectors as Polymarket plus a Kalshi-specific
`out_of_scope` bucket for sports / entertainment / daily-life
markets (NFL, Oscar, Super Bowl, etc.). Operator-extensible.

### `config/kalshi_allowed_jurisdictions.yaml`

Kalshi eligibility allow-list. The connector compares
`OPERATOR_JURISDICTION` (env var or `config/operator.yaml`)
against this list and refuses to start if the value is **not** in
the list. The seed list is `["US"]`. ISO 3166-1 alpha-2 country
codes; matching is case-insensitive. **Operator
responsibility** to keep current with Kalshi's published eligibility
scope.

This is the inverse of `config/restricted_jurisdictions.yaml`
(Polymarket's deny-list). One operator declaration drives both
gates from opposite postures.

### `config/scanner.yaml`

Signal scanner.

| Key | Default | Meaning |
| - | - | - |
| `candidate_thresholds.log_odds_shift_min` | `0.5` | Default minimum log-odds shift for candidate marking. |
| `candidate_thresholds.per_sector` | varies | Per-sector overrides. Example: `geopolitical: 0.6` (tighter threshold). |
| `confidence_floor` | `0.3` | Minimum signature confidence (0..1) for candidate eligibility. |
| `stale_source_eligible_for_candidate` | `false` | Whether scans flagged source-stale can be candidates. |
| `library_stale_threshold_days` | `14` | Library is stale if its last refresh is older than this. |
| `disabled_classes` | `[]` | Class ids to skip. |
| `monte_carlo_samples` | `1000` | Sample count for credible-interval propagation. |
| `max_workers` | `4` | Per-class scan parallelism bound. |

### `config/mispricing.yaml`

Mispricing detector.

| Key | Default | Meaning |
| - | - | - |
| `surfacing_thresholds.log_odds_delta_min` | `0.5` | Default minimum log-odds delta for surfacing. |
| `surfacing_thresholds.per_sector` | varies | Per-sector overrides. |
| `market_price_freshness_seconds` | `43200` (12h) | Comparisons whose latest price snapshot is older than this are flagged `stale_market_price`. |
| `liquidity_floors.default` | `10000.0` | Minimum 24h volume in USD for surfacing eligibility. |
| `liquidity_floors.per_sector` | varies | Per-sector overrides. Example: `regulatory: 2500.0` (smaller markets, lower floor). |
| `auto_mapping.min_keyword_overlap_for_inferred` | `3` | Number of keyword matches needed for `inferred` confidence. |
| `auto_mapping.require_temporal_qualifier_for_inferred` | `true` | Require a date/time qualifier for `inferred` mappings. |
| `max_workers` | `4` | Per-mapping parallelism bound. |

### `config/position_engine.yaml`

Position engine. Bankroll itself is set via the CLI; values here are
only the seed defaults.

| Key | Default | Meaning |
| - | - | - |
| `bankroll_defaults.analytical_bankroll_usd` | `1000.0` | Seed bankroll if no row exists. |
| `bankroll_defaults.max_single_position_pct` | `0.05` | Seed cap. |
| `bankroll_defaults.kelly_fraction_default` | `0.5` | Seed aggressiveness. |
| `bankroll_defaults.min_edge_threshold` | `0.03` | Seed min-edge. |
| `bankroll_validation.kelly_fraction_default_max` | `0.5` | Hard ceiling on kelly fraction. |
| `bankroll_validation.max_single_position_pct_max` | `0.25` | Hard ceiling on max-pct. |
| `bankroll_validation.min_edge_threshold_max` | `0.5` | Hard ceiling on min-edge. |
| `liquidity_feasibility.default_pct_of_24h_volume` | `0.05` | Suggested size as % of 24h volume that triggers `low_liquidity`. |
| `liquidity_feasibility.per_sector` | varies | Per-sector overrides. |
| `long_resolution_days_threshold` | `365` | Markets resolving further out get `long_time_to_resolution` flag. |
| `sensitivity_perturbations` | `[0.10, 0.20]` | Model-p perturbation magnitudes for sensitivity analysis. |
| `bankroll_survival_scenarios` | `[1, 3, 5]` | Adverse-outcome step counts for bankroll-survival metric. |

### `config/forbidden_phrases.yaml`

Imperative-language linter catalog. Shared by `position_engine` and
`report_generator`. Case-insensitive substring matching. Any match
in a rendered output raises `ImperativeLanguageDetected` and refuses
to ship the output. Operator-extensible — add patterns as new
imperative drift is noticed in real outputs.

Categories present in the seed catalog:

- Buy / sell directives addressed at the reader
- Trading-jargon directives (long / short / position-opening verbs)
- First-person recommendations (the system speaking as "I")
- Declarative buy / sell calls ("this is a ___")
- Confidence-pumping that crosses into directive territory
  (guarantee language, no-risk framings)
- Editorial framings added in v0.45.0 alongside the
  at-a-glance section, where synthesis can drift toward the
  operator's job most easily

The full list is in `config/forbidden_phrases.yaml`. Add to it
when new imperative-drift patterns turn up in real outputs.

### `config/monitor.yaml`

Monitor.

| Key | Default | Meaning |
| - | - | - |
| `shift_bands.default.minor_threshold` | `0.01` | Below this magnitude, shift band is `none`. |
| `shift_bands.default.material_threshold` | `0.05` | Above this, shift band is `material`. |
| `shift_bands.default.major_threshold` | `0.15` | Above this, shift band is `major`. |
| `shift_bands.per_sector` | varies | Per-sector overrides. Example: `geopolitical` has wider thresholds (`0.02 / 0.07 / 0.20`). |
| `time_decay_alert_days` | `7` | Window in days. When `days_to_resolution <=` this, the `time_decay` tier fires. |
| `material_shift_alert_threshold` | `0.10` | Informational threshold; the alert ranker uses the band classifier instead. |

### `config/report.yaml`

Report generator.

| Key | Default | Meaning |
| - | - | - |
| `enabled_sections` | five default | Body sections that render. Disabled sections produce a one-line note in the header. `cross_venue` is included by default; `reliability` is opt-in. |
| `verbosity.watchlist` | `full` | `full` includes scan reasoning text per candidate; `compact` omits it. |
| `verbosity.calibration` | `full` | Reserved for future tuning; v1 always shows verdicts. |
| `calibration_first_run_lookback_days` | `30` | Default `since` window when no prior report exists. |
| `thresholds.cross_venue_spread_bps` | `500` | Min spread (bps) for the cross_venue section. Range [0, 10000]. |
| `thresholds.cross_venue_spread_bps_per_sector` | `{}` | Per-sector overrides. |
| `thresholds.single_venue_dominance_pct` | `0.80` | Strict `>` share-of-volume threshold for the dominance warning. Range [0.0, 1.0]. |
| `thresholds.single_venue_dominance_pct_per_sector` | `{}` | Per-sector overrides. |
| `thresholds.brier_window_days` | `90` | Per-sector Brier rolling window in days. Range [1, 3650]. Also drives the reliability section's window. |
| `thresholds.brier_window_days_per_sector` | `{}` | Per-sector overrides. |
| `thresholds.brier_miscalibration` | `0.25` | Threshold above which a sector's rolling Brier is flagged miscalibrated. Range [0.0, 1.0]. |
| `thresholds.brier_miscalibration_per_sector` | `{}` | Per-sector overrides. |
| `thresholds.reliability_bin_count` | `10` | Equal-width bins covering [0, 1] for the reliability section. Range [2, 50]. |
| `thresholds.reliability_bin_count_per_sector` | `{}` | Per-sector overrides (v0.40.0). |
| `thresholds.reliability_min_resolutions_per_bin` | `5` | Sparse-bin floor for the reliability section. Range [1, 1000]. |
| `thresholds.reliability_min_resolutions_per_bin_per_sector` | `{}` | Per-sector overrides (v0.40.0). |

Out-of-range or non-coercible values fall back to the default with
a warning logged. Per-sector overrides for sectors not listed use
the global value transparently.

---

## 13. Common workflows

### First-time setup

```bash
make install                              # creates .venv and installs deps
cp .env.example .env                       # if it exists; otherwise create .env
# edit .env with credentials for the sources you want enabled

razor-rooster ingest init                  # apply schema migrations

# pick the sources you want and backfill them
razor-rooster ingest backfill --source fred
razor-rooster ingest backfill --source worldbank

# Polymarket onboarding (once)
export OPERATOR_JURISDICTION=DE             # ISO alpha-2; must NOT match config/restricted_jurisdictions.yaml
razor-rooster polymarket ack-tos
razor-rooster polymarket sync               # initial sync
razor-rooster polymarket backfill-resolutions

# (Optional) Kalshi onboarding — see "First-time Kalshi setup" below.
# Kalshi requires a US OPERATOR_JURISDICTION; the same env var drives
# both gates from opposite postures, so an operator can run only one
# of the two venues at a time on a given machine.

# Pattern library: refresh against whatever data_ingest has
razor-rooster pattern-library refresh

# Set the analytical bankroll (interactive disclaimer prompt)
razor-rooster position-engine config --bankroll 1000
```

### First-time Kalshi setup

Kalshi is a CFTC-regulated US designated contract market. The
connector enforces an allow-list posture (the inverse of
Polymarket's deny-list); the seed allow-list is `["US"]`. The same
`OPERATOR_JURISDICTION` env var drives both connectors, so an
operator declared as `DE` will pass the Polymarket geo gate and
fail the Kalshi eligibility gate, and vice versa.

```bash
# Declare a Kalshi-eligible jurisdiction.
export OPERATOR_JURISDICTION=US

# Acknowledge Kalshi's Terms of Service. Records the ack under the
# v1 read_only posture; v2 trading work will require a separate ack.
razor-rooster kalshi ack-tos

# Initial sync: cutoff → series → events → markets → prices →
# settlements. Trades are skipped because no markets are watched yet.
razor-rooster kalshi sync

# (Optional) backfill historical settlements for the calibration log.
razor-rooster kalshi backfill-settlements

# (Optional) start watching specific markets for tight-cadence price
# snapshots and trade history pulls.
razor-rooster kalshi watch KXCPI-26AUG-T2.5
razor-rooster kalshi list-watched
```

The first `kalshi sync` populates the sector heuristic's
`kalshi_sector_mapping` rows. Triage them:

```bash
razor-rooster kalshi needs-review
razor-rooster kalshi map KX-FOO macroeconomic
razor-rooster kalshi map KX-NFL out_of_scope    # Kalshi-specific value
razor-rooster kalshi map KX-WHATEVER none       # explicit-null operator decision
razor-rooster kalshi mapping-stats
```

### Mapping a class across both venues

A single event class can map to both Polymarket and Kalshi
simultaneously. The `mispricing run` cycle then writes one
comparison row per (class, venue) pair and downstream subsystems
(`position_engine`, `monitor`, `report_generator`) carry the
discriminator end-to-end, rendering `(<venue>)` after each market
identifier.

```bash
# Polymarket mapping (default; --venue is optional).
razor-rooster mispricing map cpi_above_target 0xCONDITION_ID

# Kalshi mapping (explicit --venue kalshi). The CLI verifies the
# ticker is binary; non-binary Kalshi markets are deferred to v1.2.
razor-rooster mispricing map cpi_above_target KX-CPI --venue kalshi

# Run the mispricing cycle. Two comparisons are produced for cpi_above_target.
razor-rooster mispricing run
razor-rooster mispricing list-comparisons --since 2026-05-15

# The report renders both comparisons side-by-side with venue tags.
razor-rooster report generate
```

### Spotting cross-venue disagreement

When the same class is mapped on Polymarket and Kalshi, the report
adds a Cross-Venue Disagreements section between the surfaced
comparisons and the active-watched block. The section is empty when
no class has venue prices that differ by at least five percentage
points; on quiet days you won't see it.

When it does fire, read it like this:

- **Spread** — the gap between the highest and lowest
  market-implied probability across the venues mapped to the class.
  Larger spreads mean the two venues are pricing the same question
  more differently. This is information about the markets, not
  about the model.
- **Consensus** — a liquidity-weighted average of the venue prices.
  When 24h volume data is available, the consensus weights heavier
  toward the deeper venue. When volume data is missing or zero on
  every venue, the consensus falls back to an unweighted mean and
  the renderer says so.
- **Per-venue breakdown** — each venue's `condition_id`, market
  probability, 24h volume, spread (where available), and the model
  probability + CI used in that comparison.
- **`single_venue_dominance` warning on surfaced comparisons** — if
  one venue holds more than 80% of the combined 24h volume on a
  class, every surfaced comparison for that class gets the warning
  appended. The cross-venue spread is still real, but the
  thinner-volume side may not be informative.

The framing rule still applies: the report describes the
disagreement and the volume distribution; it does not direct
the operator to act on either venue's price.

### Reading per-sector Brier scores

The calibration section now includes a per-sector Brier-score
table at the bottom, computed over the most recent 90 days of
resolutions:

- **Brier** — mean squared error between the model probability and
  the observed binary outcome. Lower is better. 0.25 is the
  random-guesser baseline at p=0.5; below 0.25 means the model
  beats coin-flip in that sector.
- **n** — number of scoreable resolutions in the window for the
  sector. Invalidated resolutions are excluded. Don't read too much
  into a sector with only 1–3 resolutions.
- **Window** — fixed at 90 days in v1; the configurable knob lives
  in `config/report.yaml.calibration.brier_window_days` (when
  wired in v1.2).
- **Status** — `miscalibrated` when Brier > 0.25, `ok` otherwise.
  The report explicitly suggests weighting outputs from
  miscalibrated sectors less. The threshold is configurable
  (default 0.25; see `config/report.yaml`).

The calibration section renders even when there are no fresh
resolutions in the report window, as long as Brier data exists in
the rolling window. On quiet days you'll see the per-sector table
without a per-resolution table above it.

### Tuning multi-venue and reliability thresholds

Every multi-venue threshold and the reliability section's knobs
live in `config/report.yaml` under the `thresholds:` block (new
in v0.39.0). Each global knob has a per-sector override sibling
keyed by `domain_sector`; sectors without an override entry use
the global value.

```yaml
thresholds:
  # Cross-venue: classes spreading by at least N bps appear in the
  # cross_venue section. Default 500 (5 pp).
  cross_venue_spread_bps: 500
  cross_venue_spread_bps_per_sector:
    geopolitical: 700           # widen for noisy sectors
    macroeconomic: 300          # tighten for sharper sectors

  # Single-venue dominance: warning when one venue holds > pct of
  # 24h volume. Default 0.80 (strict >).
  single_venue_dominance_pct: 0.80
  single_venue_dominance_pct_per_sector:
    regulatory: 0.65

  # Brier rolling window in days. Default 90.
  brier_window_days: 90
  brier_window_days_per_sector:
    macroeconomic: 30           # macro moves fast; tighter window
    geopolitical: 180           # geopolitical events are infrequent

  # Brier miscalibration threshold. Default 0.25 (random-guesser at p=0.5).
  brier_miscalibration: 0.25
  brier_miscalibration_per_sector:
    public_health: 0.20         # stricter standard

  # Reliability section (opt-in via enabled_sections).
  reliability_bin_count: 10
  reliability_min_resolutions_per_bin: 5
```

Out-of-range or non-coercible values fall back to the default
with a warning logged. Empty per-sector blocks are fine — every
sector then uses the global value.

### Reading the reliability diagram

The `reliability` section is **opt-in**. Add `reliability` to
`enabled_sections` in `config/report.yaml` once you have enough
resolutions per sector to populate the bins meaningfully (a few
months of cycles, typically).

When enabled, the section sits between calibration and
watchlist. For each domain sector, it shows 10 equal-width bins
(by default) covering [0, 1] with:

- **n** — observations in the bin
- **mean_predicted** — mean of model probabilities in the bin
- **empirical** — empirical hit rate (observed mean of binary outcomes)
- **gap** — `empirical - mean_predicted`
- **sparse** — flag for bins with fewer than the floor (default 5)

A perfectly calibrated model has `gap` near zero in every bin.

- **Positive gap** = under-confident: events happened more often
  than the model predicted.
- **Negative gap** = over-confident: events happened less often
  than predicted. The more interesting direction in v1.

Per-bin numbers in sparse bins are noisy by construction; treat
them as visual cues, not signal. Bins with zero observations are
shown as `(empty)` to make gaps in coverage visible.

v0.40.0 adds an ASCII calibration-curve overlay below each
sector's per-bin table:

```
    1.0 |                    .
        |                  . *
        |                .
    0.5 |              .
        |           # 
        |        +.
    0.0 |.
        +---------------------
         0.0                1.0
```

Read it like this:

- The diagonal of `.` characters is perfect calibration.
- `*` marks per-sector empirical points (mean_predicted, empirical_rate).
- `+` marks sparse bins (treat as noisy).
- `#` means an observation lands exactly on the diagonal cell.

Points above the diagonal are under-confident bins; points below
are over-confident. The chart is a visual aid; the per-bin table
above it is the primary surface.

### Tuning thresholds with measurements

v0.40.0 records the cross-venue spread distribution at every
cycle into a new `report_threshold_measurements` table. v0.41.0
adds two more kinds (`single_venue_dominance_share` and
`brier_per_sector`) so the generator now records all three on
every cycle. Use the three CLI subcommands to read the historical
distribution and decide whether to re-tune your configured
thresholds:

```bash
# Plain text view: one stanza per cycle. Default kind.
razor-rooster report measurements

# Filter by kind.
razor-rooster report measurements --kind brier_per_sector

# Just the last week.
razor-rooster report measurements --since 2026-05-09T00:00:00

# Raw JSON for piping into your own analysis.
razor-rooster report measurements --json | jq .

# What does the latest cycle's distribution say about my
# configured thresholds?
razor-rooster report explain-thresholds

# What threshold values would land at the p70 of my corpus's
# distribution? Reads the last 30 cycles by default.
razor-rooster report suggest-thresholds

# Tighter target percentiles.
razor-rooster report suggest-thresholds --target-pct 0.50 --target-pct 0.95

# Look back further.
razor-rooster report suggest-thresholds --lookback-cycles 90

# Apply a suggested value to config/report.yaml. Prompts before
# writing; saves a timestamped backup. New in v0.42.0.
razor-rooster report suggest-thresholds \
    --kind cross_venue_spread_bps --target-pct 0.70 --apply

# Same, non-interactively.
razor-rooster report suggest-thresholds \
    --kind cross_venue_spread_bps --target-pct 0.70 --apply --yes

# When the measurements table grows large, prune old rows.
razor-rooster report prune-measurements --keep-last 90 --confirm
razor-rooster report prune-measurements --before 2026-05-01T00:00:00 --confirm

# Or set hands-off retention via config/report.yaml's auto_prune block,
# new in v0.43.0:
#   auto_prune:
#     enabled: true
#     older_than_days: 365

# Show a unified-diff preview before applying.
razor-rooster report suggest-thresholds \
    --kind cross_venue_spread_bps --target-pct 0.70 --apply --diff

# Attach a note for retroactive review.
razor-rooster report suggest-thresholds \
    --kind cross_venue_spread_bps --target-pct 0.70 --apply --yes \
    --note "bumped after measuring p70 jumped"

# Review historical applies.
razor-rooster report tuning-log
razor-rooster report tuning-log --kind cross_venue_spread_bps

# Undo a previous apply (find the log_id from `tuning-log` first).
razor-rooster report tuning-log-undo <log_id>

# Render the report as a self-contained HTML document
# (open in any browser, including offline).
razor-rooster report generate --html data/reports/today.html
```

What to look for in `measurements`:

- If the **median spread** (`p50`) sits well below your
  configured threshold across many cycles, your section is too
  quiet and you might lower the threshold to surface narrower
  disagreements.
- If the **upper tail** (`p90`–`p99`) sits well above your
  threshold and your section is full every day, your threshold
  is too loose for your corpus.
- Rapid changes in `n` between cycles point at an upstream
  data-quality shift (e.g. fewer mappings spanning both venues),
  not a threshold problem.

What `explain-thresholds` adds: a one-line summary per kind
saying "the configured threshold sits at the p65 of this cycle's
distribution" so you don't need to eyeball the percentile column
yourself.

What `suggest-thresholds` adds: averages the recorded percentile
cuts across the last N cycles and tells you what threshold value
would correspond to each target percentile. If you'd want
roughly 30% of cross-venue cases to surface, target p70.

What v0.42.0's `--apply` flag adds: a reversible config edit. The
CLI saves a timestamped backup of `config/report.yaml` before
writing, prompts for confirmation, and refuses postures that
would silence guard rails. To revert, copy the backup back over
the live config.

What v0.42.0's stability flag adds: when the percentile cuts are
bouncing around between cycles (high coefficient of variation),
the suggestion engine prints `unstable` and the apply prompt
shows a short note. The note is descriptive — operators can
still apply, but they see the noise first.

What `prune-measurements` adds: a way to delete old rows when
the table grows large. Default retention is unbounded; the
table is small per cycle so most operators can leave it alone.

The CLIs are intentionally descriptive; they don't tell you to
move thresholds up or down. Read the picture and decide.

### Daily cycle

```bash
razor-rooster ingest cycle
razor-rooster polymarket sync
razor-rooster kalshi sync                   # if Kalshi is configured
razor-rooster pattern-library refresh
razor-rooster scan run
razor-rooster mispricing run
razor-rooster position-engine run
razor-rooster monitor run
razor-rooster report generate
```

### Reviewing alerts

```bash
razor-rooster report latest                            # today's report
razor-rooster monitor list-alerts --tier material_shift
razor-rooster monitor show <follow_up_id>
razor-rooster monitor trajectory <analysis_id>          # how this analysis evolved
razor-rooster monitor note <follow_up_id> "..."         # record your reasoning
```

### Adding a new event class

1. Copy a class module under `src/razor_rooster/pattern_library/classes/` as a template.
2. Edit `class_id`, `title`, sector, occurrence query, precursors, analogues.
3. `razor-rooster pattern-library validate <class_id>` to confirm.
4. `razor-rooster pattern-library refresh --class <class_id>` to compute outputs.
5. Optionally write per-class docs at `specs/seed_event_classes/<class_id>.md`.

### Adding a new class-to-market mapping

```bash
razor-rooster mispricing map pheic_declaration_12mo 0xCONDITION_ID \
    --type direct --notes "PHEIC declaration in 2026 question"
razor-rooster mispricing list-mappings --class pheic_declaration_12mo
razor-rooster mispricing run --class pheic_declaration_12mo
```

For markets framed inverse ("Will X NOT happen?") add `--polarity inverted`.

### Working with watched analyses

```bash
razor-rooster position-engine list --watched
razor-rooster position-engine watch <analysis_id> --note "interesting"
razor-rooster position-engine acted-on <analysis_id> --note "decided to act"
razor-rooster position-engine dismiss <analysis_id> --reason "..."
```

`watching` and `acted_on` auto-expire to `expired` when the
underlying market resolves.

### Customizing report sections

Edit `config/report.yaml` to remove sections you don't want:

```yaml
enabled_sections:
  - system_health
  - surfaced
  - watched
  # - calibration   # disabled until enough resolutions accumulate
  - watchlist

verbosity:
  watchlist: compact   # tighter output once you've curated mappings
```

The header records which sections were disabled.

---

## 14. Troubleshooting

### "DuckDB store not found"

Run `razor-rooster ingest init` once. Most subcommands require an
initialized store; the error message tells you to do this when it
applies.

### A source is `STALE` in `ingest status`

The connector either failed to fetch (check `logs/cycles/` for the
cycle's JSONL) or is past its freshness threshold without a new
attempt. Forcing a fetch:

```bash
razor-rooster ingest cycle --source <id>
```

If the source has never been fetched (`last_successful_fetch` is
NULL), check `.env` for missing credentials.

### Polymarket connector refuses to start

Two possible reasons:

1. **Geo gate.** Set `OPERATOR_JURISDICTION` env var to your ISO
   alpha-2 country code, ensuring it is not on
   `config/restricted_jurisdictions.yaml`. The error message tells
   you which check failed.
2. **ToS gate.** Run `razor-rooster polymarket ack-tos` and
   acknowledge the current Terms.

### `pattern-library refresh` produces empty outputs

Most likely the underlying `data_ingest` corpus has no rows matching
the class's predicate yet. The class still completes — its base
rate is zero with a `low_sample_warning`, and its calibration record
is the `insufficient_data` sentinel. This is not a bug; it's the
class registry waiting for data to land.

Run `razor-rooster ingest cycle` and any pending backfills first,
then re-refresh.

### `scan run` errors with definition-version drift

A class's `definition_version` was bumped after the last refresh.
Run `razor-rooster pattern-library refresh --class <id>` first,
then scan again. Or use `--strict` to abort the scan when drift is
detected.

### `mispricing run` produces no surfaced comparisons

Likely causes, in order of frequency:

1. No active mappings — `razor-rooster mispricing list-mappings` to check.
2. All comparisons below `surfacing_thresholds.log_odds_delta_min` —
   tighten the model or relax the threshold in `config/mispricing.yaml`.
3. All mapped markets `low_liquidity` — wait for volume or lower
   `liquidity_floors` per sector.
4. All mapped markets `stale_market_price` — run
   `razor-rooster polymarket sync`.
5. All scanner posteriors flagged `low_signature_confidence` —
   refresh the pattern library or lower `confidence_floor`.

### `position-engine run` produces no analyses

If `mispricing list-comparisons --surfaced-only` is empty, that's
the cause; address upstream first. If surfaced comparisons exist
but no analyses, every comparison is below `min_edge_threshold` —
either lower it via `position-engine config --min-edge` or accept
that there's nothing to size today.

### `monitor run` follow-ups don't show resolution detection

Ensure `polymarket sync` ran since the market resolved. The
monitor reads `polymarket_resolutions`; without a fresh sync,
resolution detection lags.

### `report generate` raises `ImperativeLanguageDetected`

Either the disclaimer text was edited to contain a forbidden
phrase, or an upstream rendering picked up a phrase. Check the
exception's `phrase` and `snippet` fields. The report is **not**
persisted on a linter rejection; the next run re-attempts. To
investigate without re-running the full chain, render a single
analysis manually:

```bash
razor-rooster position-engine show <analysis_id>
```

Edit `config/forbidden_phrases.yaml` only to add patterns. Removing
seed patterns to silence false positives is not the right fix; find
the upstream content drift instead.

### Disk usage approaching the cap

```bash
du -sh data/trough.duckdb
```

If above 80 GB, check `config/source_caps.yaml` for which sources
are growing. GDELT events is usually the dominant consumer. To
prune:

```bash
razor-rooster scan prune --before 2024-01-01T00:00:00+00:00 --confirm
```

For data_ingest tables, manual `DELETE` against the DuckDB store is
the path; back up first.

### Disclaimer hash drift across reports

Each `report_log` row carries a SHA-256 hash of the disclaimer text
used. If you've edited the disclaimer template, every subsequent
report will show a different hash. To check current hash:

```bash
python3 -c "from razor_rooster.report_generator.engines.section_assemblers.footer import load_disclaimer_text; from razor_rooster.report_generator.renderer.shared import disclaimer_version_hash; print(disclaimer_version_hash(load_disclaimer_text()))"
```

---

## See also

- `README.md` — quick-start, install, daily cadence summary.
- `razorrooster.md` — LOOM (architecture state of truth).
- `specs/` — full requirements / design / tasks per subsystem.
- `docs/sources.md` — per-source reference (license, ToS, free-tier limits).
- `docs/pattern_library.md` — class registry and per-class docs.
- `docs/scanner.md` — candidate-identification math.
- `docs/mispricing.md` — surfacing gates and trace schema.
- `docs/position_engine.md` — Kelly pipeline and watch-state lifecycle.
- `docs/monitor.md` — alert tiers and trajectory views.
- `docs/reports.md` — section structure and framing constraints.
- `docs/kalshi_connector.md` — Kalshi engine reference (cutoff routing, cross-venue mapping, per-endpoint cost map, post-T-KSI-072 measurement guidance).
