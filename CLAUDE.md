# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
make install          # create .venv and install package + dev deps (uses python3.12)

# Testing
make test             # all tests except smoke (integration tests included)
make test-unit        # unit tests only (no integration, no smoke)
make smoke            # smoke tests against live services (requires .env)

# Run a single test file
.venv/bin/pytest tests/signal_scanner/test_scanner.py -v

# Linting and formatting
make lint             # ruff check + format --check
make format           # ruff format + fix in-place

# Type checking (strict on most subsystem modules)
make typecheck        # mypy on data_ingest, polymarket_connector, pattern_library

# Full pipeline (first run)
make bootstrap        # runs scripts/bootstrap.sh — idempotent, handles all stages
```

The CLI entry point is `razor-rooster` (or `.venv/bin/razor-rooster`). All subsystem commands pass `--db <path>` to the DuckDB store; it defaults to `~/Projects/razor-rooster/data/trough.duckdb` if unset, or `RAZOR_ROOSTER_DB` env var.

## Architecture

Razor-Rooster is a geopolitical event forecasting engine that ingests public data sources, computes statistical base rates, scans for signal candidates, compares against prediction market prices, and generates operator reports. All subsystems share a **single DuckDB file** (`trough.duckdb`) and run as CLI subcommands in a daily pipeline.

### Pipeline execution order

```
ingest cycle       → pattern-library refresh → polymarket sync / kalshi sync
                                             ↓
scan run  →  mispricing run  →  position-engine run  →  monitor run  →  report generate
```

### Subsystem layout

Each of the 10 subsystems (`data_ingest`, `polymarket_connector`, `kalshi_connector`, `pattern_library`, `signal_scanner`, `mispricing_detector`, `position_engine`, `monitor`, `report_generator`, `gui`) follows the same internal structure:

```
<subsystem>/
  cli.py              # Click subcommand group
  models.py           # Pydantic / dataclass domain models
  config/loader.py    # YAML config loader (reads config/<subsystem>.yaml)
  engines/            # Pure computation (no I/O side effects)
  persistence/
    schemas.py        # Table DDL strings
    operations.py     # SQL read/write functions (accept a DuckDB connection)
    migrations/       # Numbered migration files (m<NNNN>_<name>.py)
```

### Database access

`data_ingest/persistence/duckdb_store.py` — `DuckDBStore` is the only sanctioned way to open the database. It provides a thread-safe connection pool (default 4 connections). All subsystems receive a `DuckDBStore` instance from their CLI layer and pass it into engine/persistence functions; engines never open their own connections.

```python
with store.connection() as conn:
    operations.persist_record(conn, record)
```

Migrations are numbered by subsystem prefix: `m0001` (data_ingest), `m1001` (polymarket), `m2001` (pattern_library), `m3001` (signal_scanner), `m4001` (mispricing_detector), `m5001` (position_engine), `m6001` (monitor), `m7001` (report_generator), `m8001` (kalshi).

### Data ingest connector pattern

Each data source is a `Connector` subclass (`data_ingest/connectors/`), decorated with `@register` from `data_ingest/registry.py`. The class attribute `source_id` is the canonical key. Connectors implement `fetch_incremental`, `fetch_backfill`, `normalize`, and `health_check`. The registry holds classes, not instances — the cycle scheduler constructs instances fresh per run.

No-auth sources (World Bank, GDELT, USGS, Federal Register) always run. API-key sources (FRED, ACLED, EIA, NRC ADAMS, regulations.gov, NOAA) gate on env vars defined in `.env.example`.

### Pattern library

`pattern_library/library.py` is the **read-only API facade** for all downstream subsystems. They must import from it; direct SQL queries against `pl_*` tables are prohibited. The library stores versioned base rates, signatures, and analogues for each `EventClass` defined under `pattern_library/classes/`.

`signal_scanner/engines/scanner.py` is the scan orchestrator: it pulls base rates and signatures from the pattern library, evaluates precursor data from `data_ingest`, computes a Bayesian posterior via Monte Carlo, and applies candidate-identification gates.

### Venue connectors

Polymarket (`polymarket_connector`) and Kalshi (`kalshi_connector`) both require a one-time interactive ToS acknowledgement before syncing. Each has `gates/tos.py` and `gates/eligibility.py` (Kalshi also has a jurisdiction allowlist). The `mispricing_detector` then compares signal-scanner posteriors against synced market prices.

### GUI

`gui/` is a read-only FastAPI + Jinja2 operator dashboard. It has no write paths. Routes are: `/` (dashboard), `/reports`, `/digest`, `/compare`, `/watch`, `/calibration`. Launch with `razor-rooster gui`.

### Config files

All runtime knobs live in `config/`. Key files: `config/ingest_schedule.yaml` (which sources run and when), `config/polymarket.yaml` / `config/kalshi.yaml` (sync settings), `config/scanner.yaml` (candidate thresholds), `config/position_engine.yaml` (Kelly sizing), `config/monitor.yaml` (alert rules), `config/report.yaml` (report sections and verbosity).

### Test conventions

- `integration` marker: tests that write to an in-memory DuckDB; use `pytest -m "not integration"` to skip.
- `smoke` marker: tests against live external services; excluded from `make test` by default.
- End-to-end cycle tests (`test_end_to_end_cycle.py`) exist for every subsystem and are the primary integration harness.
