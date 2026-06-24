# Razor-Rooster — Starter Guide

**Audience:** you're comfortable in a terminal (you know `cd`, `git clone`,
editing a text file) but new to this project. By the end you'll have the
pipeline installed, configured, and producing a daily report against live
data.

**Time:** ~20 minutes, most of it spent signing up for free API keys.

If you've done this before and just want the command list, jump to
[the cheat sheet](#cheat-sheet) at the bottom. For the full operator
reference (every command, every flag, every config knob), see
[`user_guide.md`](user_guide.md).

---

## What this is (and is not)

Razor-Rooster is a **decision-support analysis tool** for one operator. It:

- pulls public economic / geopolitical / event data on a schedule,
- computes historical base rates per event class,
- scores current conditions to produce probability estimates with reasoning,
- pulls live prices from Polymarket and Kalshi (read-only public data),
- surfaces *comparisons* where the model and the market disagree — showing
  the case for the model **and** the case for the market at equal prominence,
- writes a daily report.

It **does not** place trades, move money, recommend specific actions, or track
real capital. There is no execution layer. Every rendered output uses
conditional language and is checked by a linter that rejects directive
phrasing. You read what it surfaces and decide for yourself, outside the
system.

---

## Before you start

| You need | Why | Where |
| - | - | - |
| **Python 3.12** | Runtime (the project requires 3.12+) | [python.org](https://www.python.org/downloads/) or `brew install python@3.12` |
| **Git** | To clone the repo | preinstalled on most macOS/Linux |
| **macOS or Linux shell** | Every command below uses it | — |
| **~2 GB free disk** | DuckDB store + report files grow with use | — |
| A few **free API keys** | More keys → more data coverage (all optional) | links in [Step 3](#step-3--add-credentials-optional-but-recommended) |

> **Jurisdiction note.** Polymarket and Kalshi enforce a real geo gate. If
> your jurisdiction is on a platform's restricted list, that venue's commands
> refuse to run. The data-ingest and pattern-library stages don't care — you
> can run the whole analytical core without either market venue.

---

## Step 1 — Get the code

```bash
git clone <the-repo-url> razorrooster
cd razorrooster
```

Everything from here runs from inside the `razorrooster/` directory.

---

## Step 2 — Install

```bash
make install
```

This creates a virtual environment at `.venv/` and installs the package plus
its dependencies into it. You don't need to "activate" anything — every
command below calls the CLI by its full path, `.venv/bin/razor-rooster`.

> **Heads up — private dependency.** The project depends on a private library,
> `sloptropy-common`, that is not on PyPI. If `make install` fails resolving
> it, you need either a local clone installed first
> (`pip install -e ../sloptropy-common`) or access to the deploy key. Ask the
> repo owner if you hit this.

**Check it worked:**

```bash
.venv/bin/razor-rooster --version
.venv/bin/razor-rooster --help
```

`--help` lists eleven command groups: `ingest`, `polymarket`, `kalshi`,
`pattern-library`, `scan`, `mispricing`, `position-engine`, `monitor`,
`report`, `gui`, `calibration-backtest`. If you see them, the install is good.

> If `razor-rooster: command not found` or the path doesn't exist, the venv
> didn't build. Re-run `make install` and confirm `.venv/bin/razor-rooster`
> is present.

---

## Step 3 — Add credentials (optional, but recommended)

Razor-Rooster reads credentials from a `.env` file in the repo root. Start
from the template:

```bash
cp .env.example .env
$EDITOR .env        # open it in your editor and fill in what you have
```

**You don't need any of these to get a first report** — sources without a key
are simply skipped. But more keys means more data. These are all free and take
a few minutes each:

| Variable | Source | Sign-up |
| - | - | - |
| `FRED_API_KEY` | Federal Reserve Economic Data | https://fred.stlouisfed.org/docs/api/api_key.html |
| `EIA_API_KEY` | U.S. Energy Information Administration | https://www.eia.gov/opendata/register.php |
| `NRC_ADAMS_API_KEY` | Nuclear Regulatory Commission | https://adams.nrc.gov/wba/ |
| `REGULATIONS_GOV_API_KEY` | Federal rulemaking docket | https://open.gsa.gov/api/regulationsgov/ |
| `NOAA_CDO_TOKEN` | NOAA Climate Data Online | https://www.ncdc.noaa.gov/cdo-web/token |

**Always free, no key needed:** World Bank, GDELT, USGS, Federal Register.

**`ACLED_USERNAME` + `ACLED_PASSWORD`** need an institutional ACLED account and
carry a non-commercial-use license gate; leave them unset if you don't have
one.

Two more worth setting while you're in `.env`:

| Variable | What it does |
| - | - |
| `OPERATOR_JURISDICTION` | Your jurisdiction as an ISO 3166-1 code, e.g. `US` or `DE`. The Polymarket/Kalshi geo gates read this before any request. (You can instead set the `jurisdiction` field in `config/operator.yaml`; the env var wins on conflict.) |
| `RAZOR_ROOSTER_CONTACT` | An email or URL put into outbound `User-Agent` headers (a courtesy to the upstream APIs). |

> **Which jurisdiction value?** Polymarket uses a **deny-list** — your value
> must *not* be on `config/restricted_jurisdictions.yaml`. Kalshi uses an
> **allow-list** — your value *must* be on `config/kalshi_allowed_jurisdictions.yaml`
> (seeded with `US`). The same `OPERATOR_JURISDICTION` drives both gates from
> opposite directions, so a single machine typically runs one venue or the
> other, not both.

> `.env` is git-ignored. Never commit it.

---

## Step 4 — Acknowledge venue terms (only if you want market data)

Polymarket and Kalshi each require a **one-time** Terms-of-Service
acknowledgement. The command fetches the live ToS, hashes it, shows you the
URL, and records your ack:

```bash
.venv/bin/razor-rooster polymarket ack-tos
.venv/bin/razor-rooster kalshi ack-tos
```

Both prompt for confirmation. (To do it non-interactively later — e.g. in a
scheduled job — set `RAZORROO_AUTOACK_POLYMARKET=1` and
`RAZORROO_AUTOACK_KALSHI=1` in `.env`.)

If you skip this step, the market-sync stages stay blocked and everything else
still runs.

---

## Step 5 — Declare an analytical bankroll (only if you want sizing analyses)

The position-engine won't run until you declare a bankroll figure. **This is
not real money** — it's just the dollar number the sizing math multiplies
against (`fraction × bankroll = analyzed size`). The system has no execution
layer; nothing is at risk.

```bash
.venv/bin/razor-rooster position-engine config \
    --bankroll 1000 \
    --acknowledge-analytical \
    --notes "first-time setup, analytical figure only"
```

(Or set `RAZORROO_BANKROLL_USD=1000` in `.env` and the bootstrap declares it
for you on first run.)

---

## Step 6 — Run the whole pipeline once

```bash
make bootstrap
```

This runs `scripts/bootstrap.sh`, which is **idempotent** — safe to run as
often as you like. In one pass it:

1. applies the database schema to `data/trough.duckdb`,
2. detects which `.env` credentials are present and runs an ingest cycle for
   those sources,
3. refreshes the pattern library,
4. syncs Polymarket and Kalshi (only if you acked their ToS),
5. runs the scanner → mispricing → position-engine → monitor stages in order,
6. generates today's report as markdown + HTML under `data/reports/`,
7. prints a per-step timing summary and writes a JSON summary to
   `data/logs/`.

It finishes by listing exactly which stages (if any) are **blocked** and the
one env var or command that would unblock each. A blocked stage is not a
failure — bootstrap still exits 0.

**A healthy first run looks like:**

```
Bootstrap summary
  ran:     15 step(s)
  skipped: 0 step(s)
  blocked: 0 step(s)
  total:   183s
```

If you skipped the venue acks, you'll see `blocked: 2` with a note telling you
to run `polymarket ack-tos` / `kalshi ack-tos` — that's expected.

---

## Step 7 — Read your first report

```bash
.venv/bin/razor-rooster report latest
```

Or open `data/reports/<today's-date>.html` in a browser. The report has up to
nine sections — system health, surfaced comparisons, cross-venue
disagreements, watched analyses, calibration log, reliability diagram,
watchlist, recent threshold changes, and an optional at-a-glance summary.

Prefer to click around? Launch the local read-only GUI:

```bash
.venv/bin/razor-rooster gui --open
```

It binds to `127.0.0.1:8765` (loopback only — it is never a shared service)
and opens in your browser.

---

## Did it work? Quick health check

Run these; none should error:

```bash
.venv/bin/razor-rooster ingest status          # per-source freshness
.venv/bin/razor-rooster pattern-library list    # the 8 seed event classes
.venv/bin/razor-rooster report latest | head -40
```

If you enabled the venues:

```bash
.venv/bin/razor-rooster polymarket status        # ToS acked + recent sync
.venv/bin/razor-rooster kalshi status            # allow-list pass + recent sync
```

You're fully operational when `make bootstrap` exits 0, `data/trough.duckdb`
exists with rows, and today's report renders.

---

## Step 8 — Run it daily (optional)

Razor-Rooster does not run itself. Once the manual bootstrap works, schedule
it. Simplest form, via cron — daily at 06:00 UTC, then hand off to a watch
loop that re-renders the report as new data arrives:

```cron
0 6 * * * cd /path/to/razorrooster && \
    RAZORROO_BOOTSTRAP_THEN_WATCH=1 RAZORROO_WATCH_INTERVAL=3600 \
    make bootstrap >> data/logs/cron.log 2>&1
```

The [user guide](user_guide.md) §0 Step 7 covers `launchd` (macOS) and the
standalone `report watch` loop in detail.

---

## When something goes wrong

| Symptom | Likely cause | Fix |
| - | - | - |
| `command not found` | venv didn't build | re-run `make install` |
| install fails on `sloptropy-common` | private dep unavailable | install local clone / get deploy key |
| `ingest cycle` returns 403 | bad or missing API key | check that key in `.env` |
| `polymarket sync` refused with a geo message | jurisdiction restricted | check `OPERATOR_JURISDICTION` vs `config/restricted_jurisdictions.yaml` |
| `position-engine run` says `no bankroll_config` | bankroll not declared | redo [Step 5](#step-5--declare-an-analytical-bankroll-only-if-you-want-sizing-analyses) |
| report says "No comparisons surfaced" every cycle | thresholds too strict for your corpus | see `report suggest-thresholds` in the user guide |

The bootstrap writes a log per step under `data/logs/`. Read the matching log
for the full error. Full troubleshooting lives in
[`user_guide.md`](user_guide.md) §14.

---

## Cheat sheet

```bash
# One-time setup
git clone <repo-url> razorrooster && cd razorrooster
make install
cp .env.example .env && $EDITOR .env             # add what keys you have
.venv/bin/razor-rooster polymarket ack-tos       # if using Polymarket
.venv/bin/razor-rooster kalshi ack-tos           # if using Kalshi
.venv/bin/razor-rooster position-engine config --bankroll 1000 \
    --acknowledge-analytical --notes "setup"     # if using sizing

# Run everything + read the report
make bootstrap
.venv/bin/razor-rooster report latest

# Or run the daily loop stage-by-stage (what bootstrap does internally)
.venv/bin/razor-rooster ingest cycle
.venv/bin/razor-rooster polymarket sync
.venv/bin/razor-rooster kalshi sync
.venv/bin/razor-rooster pattern-library refresh
.venv/bin/razor-rooster scan run
.venv/bin/razor-rooster mispricing run
.venv/bin/razor-rooster position-engine run
.venv/bin/razor-rooster monitor run
.venv/bin/razor-rooster report generate
```

---

## Where to next

- **[`user_guide.md`](user_guide.md)** — the full operator reference: every
  command and flag, all config knobs, common workflows, troubleshooting.
- **[`../README.md`](../README.md)** — project overview and status.
- **`../specs/`** and **`../razorrooster.md`** — requirements, design, and the
  living project state, for anyone modifying the code.
