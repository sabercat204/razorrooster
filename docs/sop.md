# Razor-Rooster — Standard Operating Procedure

End-user setup, configuration, first-run, and steady-state operation.
This is the action-oriented checklist. For deeper conceptual material
see [user_guide.md](user_guide.md); for per-source detail see
[sources.md](sources.md); for the architectural overview see the
[README](../README.md) and the LOOM at
[razorrooster.md](../razorrooster.md).

> **Framing reminder.** Razor-Rooster is decision-support analysis.
> It does not place orders, hold positions, or interact with trading
> APIs. The "bankroll" you configure is analytical only. The system
> writes reports for you to read; you make decisions.

---

## At a glance

You will:

1. Install the package and create the DuckDB store.
2. Configure credentials for the data sources you want enabled.
3. Onboard the prediction-market venues (Polymarket, optionally
   Kalshi).
4. Refresh the pattern library against your initial data.
5. Set the analytical bankroll.
6. Schedule the daily cycle.
7. Read the daily report and triage as needed.

Estimated wall-clock time for the first install + initial backfill:
**~6–10 hours** (most of it is unattended backfill against rate-
limited public APIs). After that, daily cycles complete in under
30 minutes.

---

## 0. Prerequisites

| Requirement | Detail |
| - | - |
| OS | macOS or Linux. The development machine is macOS; Linux works identically. Windows is not supported. |
| Python | 3.11+; **3.12 recommended**. On macOS use `/opt/homebrew/bin/python3.12`. |
| Disk | ~150 GB free for the data corpus (the system enforces a 100 GB hard cap with 80% / 95% warnings). |
| Network | Outbound HTTPS only. No inbound ports. The system never sends data anywhere except the configured upstream sources. |
| Hardware | An HP EliteBook G8 (i7-8665U / 16 GB DDR4) is the development reference. Anything comparable suffices. No GPU required. |

Before starting, decide:

- **Your jurisdiction** (ISO 3166-1 alpha-2 country code, e.g. `US`,
  `DE`, `JP`). Both prediction-market connectors will refuse to run
  without it. The same value drives both gates: Polymarket refuses
  US, Kalshi requires US.
- **Which data sources you want enabled.** See
  [sources.md](sources.md) for the full list. You can start with the
  free unauthenticated sources and add credentialed ones later.

---

## 1. Install

### 1.1 Clone and enter the repository

```bash
git clone <your-fork-or-the-canonical-url> razorrooster
cd razorrooster
```

The repository root is the working directory for every command in
this document.

### 1.2 Create the virtual environment and install

```bash
make install
```

This creates `.venv/` at the repository root and installs the
package in editable mode along with the dev dependencies (`pytest`,
`ruff`, `mypy`, etc.).

The `make` targets are the single source of truth for tooling
commands. Run `make help` to list every target.

### 1.3 Verify the install

```bash
.venv/bin/razor-rooster --help
```

You should see the top-level command groups (`ingest`, `polymarket`,
`kalshi`, `pattern-library`, `scan`, `mispricing`, `position-engine`,
`monitor`, `report`).

If you prefer not to type the full path, activate the venv:

```bash
source .venv/bin/activate
razor-rooster --help
```

The rest of this document writes commands as `razor-rooster ...`
assuming the venv is activated. Substitute `.venv/bin/razor-rooster
...` if you don't activate.

### 1.4 Run the test suite

```bash
make test
```

Should report ~1,859 tests passing on a clean checkout. If anything
fails, stop and investigate before proceeding — a clean test result
is the install-verification contract.

---

## 2. Configure credentials

### 2.1 Create `.env`

```bash
cp .env.example .env || touch .env
```

Open `.env` in your editor. **Do not commit it to git** — `.gitignore`
already excludes it.

### 2.2 Add only the credentials you need

The minimum useful set is none — Razor-Rooster runs against
unauthenticated sources alone (FRED-via-proxy, World Bank, GDELT
events, USGS minerals, WHO, Federal Register, Polymarket public
endpoints, Kalshi public endpoints). Authenticated sources are
opt-in per source.

| Source | Variables | How to get the key |
| - | - | - |
| FRED | `FRED_API_KEY` | https://fred.stlouisfed.org/ — free key, instant. |
| ACLED | `ACLED_USERNAME`, `ACLED_PASSWORD` | https://acleddata.com/ register. OAuth 2.0 password grant. |
| EIA | `EIA_API_KEY` | https://www.eia.gov/opendata/ — free key. |
| NRC ADAMS | `NRC_ADAMS_API_KEY` | NRC Public Search API subscription. |
| regulations.gov | `REGULATIONS_GOV_API_KEY` | https://api.data.gov/signup — free. |
| NOAA CDO | `NOAA_CDO_TOKEN` | https://www.ncdc.noaa.gov/cdo-web/token — free. |

If a credential is absent, that source is skipped cleanly on every
cycle. Other sources still run.

### 2.3 ACLED license posture

ACLED data is non-commercial-use by default. The connector enforces
this at the gate:

- `commercial_use_recorded_grant` defaults to `FALSE`.
- The first ACLED run prompts you to acknowledge ACLED's then-
  current Terms; the acknowledgement is hash-versioned.
- A change in the Terms triggers a re-acknowledgement.

If you intend to use ACLED data commercially, **stop**, contact
ACLED, obtain a written grant, and only then run the
`razor-rooster ingest backfill --source acled` command, which will
prompt you to confirm the grant.

### 2.4 Set the operator jurisdiction

Required for both prediction-market connectors. Pick one:

```bash
# macOS / Linux: declare in your shell profile so it persists.
echo 'export OPERATOR_JURISDICTION=US' >> ~/.zshrc      # macOS default
# or
echo 'export OPERATOR_JURISDICTION=DE' >> ~/.bashrc     # Linux

# Reload the shell.
source ~/.zshrc                                         # or ~/.bashrc
```

Or use `config/operator.yaml` (the env var wins on conflict):

```yaml
# config/operator.yaml
jurisdiction: DE
```

**Picking the value matters for venue eligibility:**

| Jurisdiction | Polymarket | Kalshi |
| - | - | - |
| US | refuses (US is on Polymarket's deny-list) | accepts (US is on Kalshi's allow-list) |
| DE / GB / JP / etc. | accepts | refuses |
| KP, CU, IR, etc. (Polymarket-restricted) | refuses | refuses |

So in practice you operate **one** prediction-market venue per
machine. Razor-Rooster supports both architecturally; the gating
rules just make them mutually exclusive at runtime per operator
declaration.

---

## 3. Initialize the database

### 3.1 Apply schema migrations

```bash
razor-rooster ingest init
```

This creates `data/trough.duckdb` (override with `--db PATH` or the
`RAZOR_ROOSTER_DB` env var) and applies migrations for every
subsystem. The DuckDB file is the only persisted state — back it up
to back up the whole system.

### 3.2 Confirm the store is healthy

```bash
razor-rooster ingest status
```

Should print one row per registered source, with `last_successful_fetch`
NULL on every row (no fetches have run yet). This is the expected
state after `init`.

---

## 4. Backfill the data corpus

The data sources have varying historical depth. The backfill is
per-source, resumable, and respects per-source byte caps.

### 4.1 Plan the backfill order

Start with the cheap fast sources, finish with the expensive ones:

```bash
# Free, fast, well-bounded: FRED + World Bank + USGS + WHO
razor-rooster ingest backfill --source fred                  # ~5 minutes
razor-rooster ingest backfill --source worldbank             # ~10 minutes
razor-rooster ingest backfill --source usgs                  # ~2 minutes
razor-rooster ingest backfill --source who_don               # ~1 minute

# Federal Register + regulations.gov + NRC ADAMS — slower
razor-rooster ingest backfill --source federal_register      # ~30 minutes
razor-rooster ingest backfill --source regulations_gov       # ~2 hours
razor-rooster ingest backfill --source nrc_adams             # ~30 minutes

# NOAA — long, rate-limited
razor-rooster ingest backfill --source noaa                  # ~3 hours

# GDELT events — capped at 5 years; biggest single-source footprint
razor-rooster ingest backfill --source gdelt_events          # ~1–2 hours

# EIA + ACLED if you have credentials
razor-rooster ingest backfill --source eia                   # ~30 minutes
razor-rooster ingest backfill --source acled                 # ~1 hour
```

Each command writes a structured JSONL log under `logs/cycles/` and
respects `config/source_caps.yaml`. The 100 GB global cap kicks in
with a warning at 80% full and refuses new backfill batches at 95%.

### 4.2 If a backfill is interrupted

Re-running the same command picks up from the last committed resume
token. To force a restart from scratch:

```bash
razor-rooster ingest backfill --source <source_id> --restart
```

### 4.3 Confirm freshness

```bash
razor-rooster ingest status
```

Every backfilled source should now show a recent `last_successful_fetch`.

---

## 5. Onboard a prediction-market venue

You need at least one prediction-market venue connected for the
mispricing detector to have anything to compare against. Pick the
one that matches your jurisdiction.

### 5.1 Polymarket onboarding (non-US operators)

```bash
# 1. Confirm OPERATOR_JURISDICTION is set and is NOT on Polymarket's
#    deny-list. The deny-list lives at config/restricted_jurisdictions.yaml.
echo $OPERATOR_JURISDICTION

# 2. Acknowledge the Polymarket Terms of Service. The CLI fetches
#    the live ToS, hashes it, displays the URL, prompts for
#    confirmation, and records the hash on the polymarket source row.
razor-rooster polymarket ack-tos

# 3. Initial sync.
razor-rooster polymarket sync

# 4. (One-shot) Backfill historical resolutions for the calibration log.
razor-rooster polymarket backfill-resolutions

# 5. Verify.
razor-rooster polymarket status
```

If `polymarket ack-tos` refuses with "geo gate refused", your
declared jurisdiction is on Polymarket's restricted list. Your
options are: switch to Kalshi (if your jurisdiction allows it),
operate from a different machine, or stop here.

### 5.2 Kalshi onboarding (US operators)

```bash
# 1. Confirm OPERATOR_JURISDICTION=US (Kalshi's seed allow-list is US-only).
echo $OPERATOR_JURISDICTION

# 2. Acknowledge Kalshi's Terms of Service. Records the ack under
#    the v1 read_only posture; v2 trading work would require a
#    separate acknowledgement.
razor-rooster kalshi ack-tos

# 3. Initial sync (cutoff → series → events → markets → prices →
#    settlements; trades skipped because no markets are watched yet).
razor-rooster kalshi sync

# 4. (One-shot) Backfill historical settlements for the calibration log.
razor-rooster kalshi backfill-settlements

# 5. Verify.
razor-rooster kalshi status
```

### 5.3 Configure watched markets (optional, recommended)

A "watched market" gets tighter-cadence price snapshots and full
trade-history pulls. Use this for markets you actively care about:

```bash
# Polymarket — by condition_id (the 0x... hex string).
razor-rooster polymarket watch 0xCONDITION_ID
razor-rooster polymarket list-watched

# Kalshi — by ticker.
razor-rooster kalshi watch KXCPI-26AUG-T2.5
razor-rooster kalshi list-watched
```

State persists in `config/polymarket.yaml.sync.prices.watched_markets`
or `config/kalshi.yaml.sync.prices.watched_markets`. Removed via the
`unwatch` command.

### 5.4 Sector mapping triage

The connectors run a keyword heuristic that classifies each market
into one of the eight Razor sectors (`public_health`, `geopolitical`,
`regulatory`, `commodity`, `climate`, `infrastructure_energy`,
`macroeconomic`, `cross_cutting`). Kalshi adds an `out_of_scope`
bucket for sports / entertainment markets.

After the first sync, triage the markets the heuristic couldn't
classify:

```bash
# Polymarket
razor-rooster polymarket needs-review
razor-rooster polymarket map 0xCONDITION_ID macroeconomic
razor-rooster polymarket map 0xCONDITION_ID none      # explicit "no Razor sector"
razor-rooster polymarket mapping-stats

# Kalshi
razor-rooster kalshi needs-review
razor-rooster kalshi map KX-FOO macroeconomic
razor-rooster kalshi map KX-NFL out_of_scope          # Kalshi-specific value
razor-rooster kalshi map KX-WHATEVER none
razor-rooster kalshi mapping-stats
```

Manual mappings are never overwritten by the heuristic on subsequent
syncs. Plan for ~30 minutes of triage on first install with a couple
of hundred markets.

---

## 6. Refresh the pattern library

The pattern library computes per-class historical base rates,
precursor signatures, and analogue feature spaces from the
`data_ingest` corpus. It is the foundation the signal scanner reads.

```bash
# Show registered classes (the v1 seed library has eight).
razor-rooster pattern-library list

# Refresh everything against your data corpus.
razor-rooster pattern-library refresh
```

Expected duration on EliteBook G8 hardware: **under 15 minutes**
against the v1 seed library and a populated corpus.

Some classes (`eia_grid_reliability_event`,
`polymarket_resolution_calibration`) produce empty outputs until the
operator's data corpus or downstream subsystems land — that is by
design. They get a `low_sample_warning` flag and an
`insufficient_data` calibration row.

To inspect one class:

```bash
razor-rooster pattern-library show pheic_declaration_12mo
```

To validate a class definition without persisting:

```bash
razor-rooster pattern-library validate pheic_declaration_12mo
```

---

## 7. Set the analytical bankroll

The `position_engine` needs a bankroll figure to compute Kelly
sizing. **This is analytical only.** The system does not track real
money.

### 7.1 Interactive (recommended for the first call)

```bash
razor-rooster position-engine config --bankroll 1000
```

You will get a disclaimer prompt. Read it, confirm, and the
bankroll is recorded.

### 7.2 Non-interactive (for scripted setup)

```bash
razor-rooster position-engine config \
  --bankroll 1000 \
  --max-pct 0.05 \
  --kelly-fraction 0.5 \
  --min-edge 0.03 \
  --no-prompt --acknowledge-analytical
```

Bounds:

| Parameter | Bound | Default |
| - | - | - |
| `--kelly-fraction` | `[0, 0.5]` | `0.5` (half-Kelly ceiling) |
| `--max-pct` | `[0, 0.25]` | `0.05` (max single-position % of bankroll) |
| `--min-edge` | `[0, 1]` | `0.03` |

Updating the bankroll is append-only — each `config` call writes a
new row; the latest by `effective_at` wins.

---

## 8. First full cycle (manual dry run)

Run every subsystem once, in order, before automating the daily
cadence:

```bash
razor-rooster ingest cycle               # incremental data refresh
razor-rooster polymarket sync            # if you onboarded Polymarket
razor-rooster kalshi sync                # if you onboarded Kalshi
razor-rooster pattern-library refresh    # recompute base rates
razor-rooster scan run                   # current-conditions probabilities
razor-rooster mispricing run             # model vs market
razor-rooster position-engine run        # sizing math for surfaced
razor-rooster monitor run                # check on watched analyses
razor-rooster report generate            # the operator-facing document
```

Read the report. The first one will be sparse — most analyses don't
exist yet because nothing has been watched. As you mark surfaced
comparisons as `watching` in the position engine, the monitor and
report start producing richer output.

To mark an analysis as watching:

```bash
razor-rooster position-engine watch <analysis_id> --note "interesting setup"
```

Other watch states:

```bash
razor-rooster position-engine acted-on <analysis_id>  --note "took action"
razor-rooster position-engine dismiss   <analysis_id> --reason "not actionable"
```

`watching` and `acted_on` auto-expire to `expired` when the
underlying market resolves.

---

## 9. Schedule the daily cycle

Razor-Rooster does not run itself. Schedule the daily cycle via
your host's scheduler.

### 9.1 macOS — `launchd`

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
      <string>/Users/YOUR_USERNAME/Sloptropy/razorrooster/scripts/daily_cycle.sh</string>
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
    <key>EnvironmentVariables</key>
    <dict>
      <key>OPERATOR_JURISDICTION</key>
      <string>US</string>
    </dict>
  </dict>
</plist>
```

Create the wrapper script `scripts/daily_cycle.sh` (set executable):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

VENV_BIN="./.venv/bin"

"$VENV_BIN/razor-rooster" ingest cycle
"$VENV_BIN/razor-rooster" polymarket sync || true   # don't fail the cycle on venue outage
"$VENV_BIN/razor-rooster" kalshi sync || true
"$VENV_BIN/razor-rooster" pattern-library refresh
"$VENV_BIN/razor-rooster" scan run
"$VENV_BIN/razor-rooster" mispricing run
"$VENV_BIN/razor-rooster" position-engine run
"$VENV_BIN/razor-rooster" monitor run
"$VENV_BIN/razor-rooster" report generate
```

Make it executable:

```bash
chmod +x scripts/daily_cycle.sh
```

Load the launchd plist:

```bash
launchctl load ~/Library/LaunchAgents/com.razorrooster.cycle.plist
launchctl start com.razorrooster.cycle    # immediate test run
```

### 9.2 Linux — `cron`

```cron
30 8 * * *  cd /home/YOUR_USER/razorrooster && ./scripts/daily_cycle.sh >> logs/cycle.stdout.log 2>> logs/cycle.stderr.log
```

Set `OPERATOR_JURISDICTION` in your shell profile or in the cron
environment file so the connectors find it.

### 9.3 Verify the schedule fires

The next morning (or after a manual `launchctl start` /
`run-parts`), inspect:

```bash
ls -lt logs/                 # cycle.stdout.log should be recent
tail -50 logs/cycle.stdout.log
ls -lt logs/cycles/ | head   # one JSONL log per cycle
```

The full cycle should complete in under 30 minutes per
NFR-PERF-001.

---

## 10. Steady-state operation

### 10.1 Daily morning workflow

```bash
# Read today's report.
razor-rooster report latest

# Triage active alerts.
razor-rooster monitor list-alerts
razor-rooster monitor list-alerts --tier material_shift
razor-rooster monitor show <follow_up_id>
razor-rooster monitor trajectory <analysis_id>

# Take notes — append-only, retrospective.
razor-rooster monitor note <follow_up_id> "Reviewed; deciding to hold."

# Mark new surfaced comparisons that interest you as 'watching'.
razor-rooster position-engine list --watched
razor-rooster mispricing list-comparisons --surfaced-only --since 2026-05-01
razor-rooster position-engine show <analysis_id>
razor-rooster position-engine watch <analysis_id> --note "..."
```

### 10.2 Weekly housekeeping

```bash
# Disk-budget check.
du -sh data/trough.duckdb
razor-rooster ingest status

# Re-triage unmapped markets — both venues' new listings show up here.
razor-rooster polymarket needs-review
razor-rooster kalshi needs-review

# Review the calibration log section in the report. Over time this
# tells you whether the model is well-calibrated.
razor-rooster report latest
```

### 10.3 Adding a new event class

The pattern library is operator-extensible. To add a class:

1. Copy a class module under `src/razor_rooster/pattern_library/classes/`.
2. Edit `class_id`, `title`, sector, occurrence query, precursors,
   analogue features, base-rate window, and refractory period.
3. `razor-rooster pattern-library validate <class_id>` to confirm.
4. `razor-rooster pattern-library refresh --class <class_id>` to
   compute the first round of outputs.
5. Optionally write per-class docs at
   `specs/seed_event_classes/<class_id>.md`.

Library version auto-bumps when the registry changes.

### 10.4 Adding a class-to-market mapping

When you find a market that corresponds to one of your event classes:

```bash
# Polymarket (default; --venue is optional).
razor-rooster mispricing map cpi_above_target 0xCONDITION_ID

# Kalshi (explicit --venue).
razor-rooster mispricing map cpi_above_target KX-CPI --venue kalshi

# For markets framed inverted ("Will X NOT happen?"):
razor-rooster mispricing map cpi_above_target 0xCONDITION_ID --polarity inverted
```

A single class can map to both Polymarket and Kalshi simultaneously.
The `mispricing run` cycle then writes one comparison row per (class,
venue) pair.

### 10.5 Tuning thresholds

After ~30 days of cycles, the empirical distribution of model + market
shifts and signal-scanner divergences is informative. Review and
adjust:

| Knob | File | What it controls |
| - | - | - |
| `candidate_thresholds.log_odds_shift_min` | `config/scanner.yaml` | When does the scanner flag a class as a candidate. |
| `confidence_floor` | `config/scanner.yaml` | Minimum signature confidence for candidate eligibility. |
| Per-sector thresholds | `config/mispricing.yaml` | When does the mispricing detector surface a comparison. |
| `liquidity_floor` | `config/mispricing.yaml` | Minimum 24h volume for a market to be considered. |
| `shift_bands.default` | `config/monitor.yaml` | Magnitude classification thresholds for change detection. |
| `time_decay_alert_days` | `config/monitor.yaml` | Days-to-resolution that fires the time-decay alert. |
| `forbidden_phrases` | `config/forbidden_phrases.yaml` | Imperative-language linter catalog (operator-extensible). |

Restart the daily cycle after editing — configs are re-read on every
run.

---

## 11. Backup and recovery

### 11.1 Backup

The DuckDB store is the entire persisted state. Back it up while
nothing is writing:

```bash
# Stop the scheduler temporarily, OR run the backup right before
# the next daily-cycle launch window.
cp data/trough.duckdb data/trough.duckdb.backup-$(date +%Y%m%d)
```

For point-in-time backups during operation, use DuckDB's `EXPORT
DATABASE` command from a SQL client. The DuckDB WAL (`.duckdb.wal`)
is inflight state — copy the WAL alongside the main file or run a
checkpoint first.

The `config/` directory and `.env` should also be backed up — `.env`
to a password-manager-style vault (it contains API credentials);
`config/` can go to git in a private mirror, sans `.env`.

### 11.2 Recovery from a broken cycle

```bash
# Inspect the most recent cycle log.
ls -lt logs/cycles/ | head
cat logs/cycles/<latest>.jsonl | jq .   # JSONL — one entry per connector

# Per-source state lives in the backfill_state table.
razor-rooster ingest status
```

If a single source is broken but everything else is fine, force a
restart for just that source:

```bash
razor-rooster ingest backfill --source <source_id> --restart
```

### 11.3 Full reset

If the store is corrupt and you want to start over:

```bash
# Back up first.
mv data/trough.duckdb data/trough.duckdb.broken-$(date +%Y%m%d)
mv data/trough.duckdb.wal data/trough.duckdb.wal.broken-$(date +%Y%m%d) 2>/dev/null || true

# Re-init.
razor-rooster ingest init

# Re-onboard the prediction-market venue(s).
razor-rooster polymarket ack-tos     # if applicable
razor-rooster kalshi ack-tos         # if applicable

# Re-backfill (this is the slow part — see §4).
# ...
```

---

## 12. Troubleshooting quick reference

| Symptom | Likely cause | First step |
| - | - | - |
| `polymarket ack-tos refuses with geo gate` | Your `OPERATOR_JURISDICTION` is on Polymarket's deny-list. | Check `config/restricted_jurisdictions.yaml`. If you intended a different jurisdiction, fix the env var. |
| `kalshi ack-tos refuses with eligibility gate` | Your `OPERATOR_JURISDICTION` is not on Kalshi's allow-list. | The seed allow-list is `["US"]`. If Kalshi has extended access, edit `config/kalshi_allowed_jurisdictions.yaml`. |
| `kalshi ack-tos refuses with ToSPostureMismatch` | A previously-recorded acknowledgement is for the `'trading'` posture (a v2 concept). | Re-run `razor-rooster kalshi ack-tos` (it records under `read_only`). |
| Sources are STALE in `ingest status` | Backfill incomplete or scheduler not firing. | `razor-rooster ingest backfill --source <id>`. Inspect `logs/cycle.stderr.log`. |
| `disk usage near 95%` warning | Corpus approaching the 100 GB cap. | Trim retention via `config/source_caps.yaml` or remove an unused source's data. |
| Pattern library `low_sample_warning` on a class | Insufficient occurrences in the data corpus for that class. | Expected for some seed classes. To investigate: `razor-rooster pattern-library show <class_id>`. |
| Mispricing comparison surfaced but no analysis | `position-engine run` hasn't yet evaluated it. | Run it: `razor-rooster position-engine run`. |
| Report renderer rejects output (linter) | The renderer caught imperative language somewhere. | The next run will re-attempt. If it persists, inspect `config/forbidden_phrases.yaml` for an over-aggressive entry. |
| Daily cycle takes > 30 minutes | One or more sources is slow or rate-limited. | `tail -50 logs/cycle.stdout.log` to see which step. Most often: NOAA, GDELT events. |
| `"DuckDB store not found"` | The store hasn't been initialized yet, or the path is wrong. | `razor-rooster ingest init` or set `RAZOR_ROOSTER_DB`. |

For deeper troubleshooting, see [user_guide.md](user_guide.md)
section 14.

---

## 13. What to read next

- [user_guide.md](user_guide.md) — every CLI command, every option,
  every config knob. Reference manual.
- [README.md](../README.md) — architectural overview and threat
  context.
- [sources.md](sources.md) — per-source license, ToS, free-tier
  limits, expected disk footprint.
- Per-subsystem docs:
  - [pattern_library.md](pattern_library.md)
  - [scanner.md](scanner.md)
  - [mispricing.md](mispricing.md)
  - [position_engine.md](position_engine.md)
  - [monitor.md](monitor.md)
  - [reports.md](reports.md)
  - [kalshi_connector.md](kalshi_connector.md)

---

## Appendix A — Disclaimer

Razor-Rooster is an educational decision-support tool for
understanding model-vs-market disagreement on prediction-market
contracts. **It does not constitute investment advice, trading
recommendations, or an offer to trade.** Markets are correct more
often than not; when the model and the market disagree, the trace
presents the case for both views at equal prominence and asks you
to weigh them.

The system does not place orders, hold positions, or interact with
trading APIs. The "bankroll" you configure is analytical only — the
system has no knowledge of your real positions and no ability to
affect them.

Sources retain their own license terms. ACLED data is non-commercial
by default; do not use it commercially without a written grant from
ACLED. See [sources.md](sources.md).
