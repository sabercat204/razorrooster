# DATA_INGEST — Design

**Subsystem:** `data_ingest`
**Codename:** The Trough
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD
**Last updated:** 2026-05-14
**Companion spec:** `DATA_INGEST.md` (Requirements v0.1.0)

---

## 1. Overview

This document specifies the technical design for `data_ingest`. It maps the requirements in `DATA_INGEST.md` to a concrete architecture: connector interface, normalization layer, persistence schema, scheduling model, and operational model. It also resolves the open questions (OQ-001 through OQ-007) carried from the requirements phase.

The design follows three discipline rules:

1. **Source-native preservation.** Every ingest path stores the original source payload verbatim alongside the normalized columns. If our normalization is wrong, the source-of-truth is recoverable.
2. **Failure isolation.** Each connector is a separate process or sub-process that cannot corrupt the work of any other connector.
3. **No silent ingestion.** Cycle reports tell the operator exactly what happened — what succeeded, what failed, what was stale, what was skipped. The system never quietly does the wrong thing.

## 2. Resolved Open Questions

The following open questions from the requirements phase are settled here. Anything not settled is moved to section 12 (deferred to implementation/empirical confirmation).

### OQ-001 — ACLED access tier and licensing

**Resolution:** ACLED provides an **OAuth 2.0** authenticated API at `acleddata.com/api/` (events at `/acled/read`, deletions at `/deleted/read`). Authentication is via password grant against `/oauth/token` returning a 24-hour access token and 14-day refresh token. Account registration is required.

The license posture is **ACLED Terms and Conditions, version-hashed at first use**, *not* a specific Creative Commons variant. The CRAN package `acled.api` lists `CC BY-NC 4.0` for its R-side wrapper code, but that is the wrapper's license, not necessarily the data's. The connector's startup gate hashes ACLED's currently-published Terms and stores the acknowledgement; downstream subsystems consult the operator's `commercial_use_recorded_grant` flag (default false) before exporting ACLED-derived data.

**Design implications:**
- The ACLED connector includes a Terms acknowledgement gate at startup that records the operator's acknowledgement and the SHA-256 of the canonical Terms text in the `sources` table.
- Razor-Rooster's intended use (personal educational forecasting) is consistent with the conservative default. If the project's character ever changes, the operator must explicitly review ACLED's then-current Terms and record a commercial-use grant.
- This constraint is encoded as `LICENSE_NONCOMMERCIAL_REQUIRED = true` and `commercial_use_recorded_grant = false` on the source registration. Any downstream subsystem that exports ACLED-derived data must check both flags.
- Authentication: OAuth 2.0 password grant (`username` + `password` + `grant_type=password` + `client_id=acled` + `scope=authenticated`). Tokens cached in-process memory only, never persisted. See REQ-ACLED-AUTH-001..003.
- The deleted-events endpoint (`/api/deleted/read`) is incorporated into the ACLED sync as a reconciliation pass that runs before the events pull each cycle (REQ-ACLED-DELETED-001..002). Without this, the local store drifts from ACLED's authoritative state as ACLED retracts events on subsequent reviews.

### OQ-002 — GDELT bulk-download size

**Resolution:** GDELT GKG 2.0 at full historical depth (~10 years) reportedly approaches 166 GB compressed / 573 GB uncompressed. This exceeds the 100 GB global corpus cap (NFR-PERF-002).

**Design implications:**
- v1 ingests **GDELT 2.0 Events** only, not the full GKG. Events are smaller and contain the geopolitical signal we actually need (location, actors, event-coded action, tone). GKG can be added in v2 if needed.
- GDELT events are pulled in 15-minute chunks from the GDELT 2.0 raw-data endpoint, but stored at daily aggregate granularity (one row per event, not per 15-minute file).
- Backfill cap for GDELT defaults to **5 years** in v1, configurable upward only after disk-budget verification.
- The connector applies an ingest-time filter (configurable) on event-code categories of interest, with a "store all" option for operators who explicitly opt in. Default is to store all events; the filter is for operators with constrained disk.

### OQ-003 — OPEC MOMR PDF parsing

**Resolution:** Defer to v2. PDF parsing reliability across years of MOMR layout changes is too risky for v1 and the data overlaps substantially with FRED commodity series and EIA reports.

**Design implications:**
- OPEC MOMR is removed from the v1 source list. The source registration mechanism (REQ-EXT-001) supports adding it later without architectural change.
- Updated v1 source count: **12 sources**, not 13.

### OQ-004 — NRC ADAMS access

**Resolution:** NRC provides an **official ADAMS Public Search API** (Azure-managed developer portal at `adams-api-developer.nrc.gov`). API access is free with registration. This replaces the scraping approach considered in requirements.

**Design implications:**
- NRC ADAMS connector uses the official API.
- Authentication follows the same `.env`-loaded pattern as other registered-key sources (REQ-SRC-003).
- Backfill depth: PARS Library (1999+) is the canonical target. Public Legacy Library (pre-1999) is excluded from v1 — bibliographic-only records with limited full-text support add complexity for marginal forecasting value.

### OQ-005 — DuckDB upsert performance

**Resolution:** Known issue: DuckDB upserts are slow when input is unsorted by key. Batched inserts scale sub-linearly. Single-row upserts in a loop are pathological at scale.

**Design implications:**
- All ingest writes go through a **two-stage staging-merge pattern**:
  1. Stage 1: write the batch to a `_staging_<table>` table via a single bulk `INSERT` from a Pandas DataFrame or Arrow table. No deduplication.
  2. Stage 2: a single `INSERT INTO <table> ... ON CONFLICT (...) DO UPDATE` from the staging table, then `TRUNCATE` the staging table.
- The staging table is sorted by the dedup key before the merge step.
- Batch size defaults to 10,000 records. Larger batches risk memory pressure on 16 GB RAM; smaller batches lose throughput.
- This pattern is implemented once in the persistence layer; connectors do not implement their own upsert logic.

### OQ-006 — Geographic identifier normalization

**Resolution:** Normalize at ingest time, but minimally and only into ISO 3166-1 alpha-3 country codes. Sub-national normalization (GADM admin units) is deferred to a downstream geographic layer.

**Design implications:**
- The `event_stream` and `geospatial_indicator` schemas include a normalized `country_iso3` column populated when the source provides unambiguous country information.
- Source-native location identifiers (lat/lon, admin region names, source-specific codes) are preserved verbatim in the JSON payload.
- A small lookup module in `data_ingest.normalization.geo` maps source-native country identifiers to ISO 3166-1 alpha-3. Ambiguous cases store NULL and log a warning.

### OQ-007 — Data quality table

**Resolution:** Implement a minimal `ingest_anomalies` table in v1. Track only ingest-time anomalies that we can detect cheaply: schema mismatches, sudden zero-record cycles for previously-active sources, and source-side error responses outside the rate-limit category.

**Design implications:**
- The table is append-only with `(source_id, anomaly_type, detected_at, details_json)` columns.
- Downstream subsystems can query it but the responsibility for forecasting-relevant data quality (outlier detection, trend break identification) sits with `signal_scanner`, not `data_ingest`.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      data_ingest/
        __init__.py
        cli.py                          # ingest cycle, single-source pull, backfill commands
        registry.py                     # source registration & lookup
        scheduler.py                    # cadence evaluation, cycle orchestration
        persistence/
          __init__.py
          duckdb_store.py               # DuckDB connection, schema migrations
          schemas.py                    # canonical schema definitions
          staging_merge.py              # OQ-005 staging-merge pattern
          provenance.py                 # freshness view, anomaly logging
        normalization/
          __init__.py
          base.py                       # NormalizedRecord dataclass per schema
          geo.py                        # OQ-006 ISO 3166 country mapping
          time.py                       # UTC normalization
        connectors/
          __init__.py
          base.py                       # Connector ABC
          fred.py
          worldbank.py
          who_don.py
          acled.py
          gdelt_events.py
          federal_register.py
          noaa.py
          usgs_minerals.py
          eia.py
          nrc_adams.py
          regulations_gov.py            # EPA dockets via regulations.gov
          bdi.py                        # via FRED proxy
        config/
          ingest_schedule.yaml          # cadence per source
          source_caps.yaml              # per-source size & depth caps
        logging/
          __init__.py
          structured.py                 # JSON cycle logs

### 3.2 Connector Interface (REQ-SRC-001)

All connectors implement an abstract base class:

```python
class Connector(ABC):
    source_id: str                          # e.g., "fred", "acled"
    canonical_schema: SchemaType            # one of the four enums
    cadence_default: str                    # "daily" | "weekly" | "annual"
    license: License                        # PUBLIC_DOMAIN | CC_BY | CC_BY_NC | TERMS_OF_SERVICE
    backfill_supported: bool

    @abstractmethod
    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        """Pull records published on or after `since`."""

    @abstractmethod
    def fetch_backfill(self, until: datetime, resume_token: ResumeToken | None) -> Iterator[RawRecord]:
        """Pull historical records up to `until`, resumable via `resume_token`."""

    @abstractmethod
    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        """Convert source-native record to canonical schema. No imputation."""

    def health_check(self) -> ConnectorHealth:
        """Default impl: hit a known-good endpoint and return latency + status."""
```

`RawRecord` is a frozen dataclass: `(source_id, source_record_id, source_payload_json, source_publication_ts)`.

`NormalizedRecord` is a tagged union over the four canonical schemas. Each variant carries the columns specified in section 4 plus the original `source_payload_json`.

The base class provides default implementations of: rate-limit handling with exponential backoff and jitter (REQ-SRC-002), credential loading from environment (REQ-SRC-003), and per-record error capture so that one bad record doesn't kill the batch.

### 3.3 Cycle Orchestration (REQ-SCHED-002)

A cycle is a single pass through all due connectors:

    1. scheduler.evaluate_due()         → list of (Connector, mode) where mode = INCREMENTAL or BACKFILL
    2. for each connector in parallel (max_workers=4 default; configurable):
         a. connector.fetch_incremental(since=last_successful_fetch)
         b. for each batch of 10,000 raw records:
              - normalize batch
              - persist batch via staging-merge
              - update sources.last_successful_fetch on full success
         c. on failure: log structured error, mark sources.last_failed_fetch, continue cycle
    3. scheduler.write_cycle_report()   → structured JSON to logs/, summary to stdout

Concurrency is bounded (`max_workers=4`) to keep memory pressure low on the EliteBook G8. Per-source pulls are independent and don't share connection pools.

### 3.4 Backfill Mode (REQ-BACKFILL-*)

Backfill runs as a separate top-level command, not as part of a regular cycle:

    razor-rooster ingest backfill --source <id> [--until <iso8601>] [--cap <bytes>]

A backfill maintains a `backfill_state` row per source: `(source_id, started_at, last_resume_token, records_persisted, bytes_persisted, status)`. The connector's `fetch_backfill` method accepts `resume_token` and yields records; the persistence layer updates `last_resume_token` after each successful batch commit. Restart picks up from the last committed token.

Per-source size caps from `config/source_caps.yaml` are checked before each batch commit. If commit would exceed the cap, the connector is paused with status `CAP_REACHED` and the cycle continues. The operator can raise the cap and resume.

A global corpus cap (default: 100 GB, NFR-PERF-002) is checked at the same point. If exceeded, the connector is paused and the operator is alerted.

## 4. Canonical Schemas (REQ-NORM-001)

All four schemas share a common provenance prefix:

    source_id              VARCHAR     NOT NULL          -- e.g. "fred"
    source_record_id       VARCHAR     NOT NULL          -- source's own ID, or deterministic hash
    source_publication_ts  TIMESTAMP   NOT NULL          -- when the source published it
    fetch_ts               TIMESTAMP   NOT NULL          -- when we ingested it
    connector_version      VARCHAR     NOT NULL          -- semver of the connector that ingested
    superseded_at          TIMESTAMP   NULL              -- non-NULL if a newer version exists (REQ-PERSIST-004)
    source_payload_json    JSON        NOT NULL          -- verbatim source payload

Primary key on every table: `(source_id, source_record_id, fetch_ts)`.
Unique key for dedup: `(source_id, source_record_id)` where `superseded_at IS NULL`.

### 4.1 `event_stream`

For point-in-time discrete events (ACLED incidents, GDELT events, WHO DON entries, Federal Register filings).

    [provenance prefix]
    event_ts               TIMESTAMP   NOT NULL          -- when the event itself occurred
    country_iso3           VARCHAR(3)  NULL              -- OQ-006 normalized
    actor_primary          VARCHAR     NULL              -- primary actor (e.g. ACLED Actor1)
    actor_secondary        VARCHAR     NULL
    event_class            VARCHAR     NULL              -- source-native classification, normalized casing
    description            TEXT        NULL              -- short human-readable summary

Indexes: `(country_iso3, event_ts)`, `(source_id, event_ts)`.

### 4.2 `time_series`

For numeric values at timestamps (FRED indices, NOAA readings, EIA stocks).

    [provenance prefix]
    series_id              VARCHAR     NOT NULL          -- source-native series identifier
    observation_ts         TIMESTAMP   NOT NULL
    value                  DOUBLE      NULL              -- NULL preserved (REQ-NORM-003)
    unit                   VARCHAR     NULL              -- source-native unit string
    frequency              VARCHAR     NULL              -- "D" | "W" | "M" | "Q" | "A"

Indexes: `(series_id, observation_ts)`, `(source_id, observation_ts)`.

### 4.3 `document_docket`

For structured documents (NRC ADAMS, regulations.gov dockets, Federal Register full-text rules).

    [provenance prefix]
    title                  TEXT        NOT NULL
    document_type          VARCHAR     NULL              -- e.g. "rule", "proposed_rule", "notice"
    docket_id              VARCHAR     NULL              -- when applicable
    agency                 VARCHAR     NULL
    published_date         DATE        NULL
    effective_date         DATE        NULL
    comment_close_date     DATE        NULL
    full_text_uri          VARCHAR     NULL              -- pointer to source-hosted full text
    full_text_local_path   VARCHAR     NULL              -- if we cached it locally (large docs only)

Indexes: `(agency, published_date)`, `(docket_id)`, `(document_type, published_date)`.

### 4.4 `geospatial_indicator`

For values indexed by geography and time (drought indices, ENSO state, wildfire risk by region).

    [provenance prefix]
    indicator_id           VARCHAR     NOT NULL          -- e.g. "spi_3mo", "enso_oni"
    observation_ts         TIMESTAMP   NOT NULL
    country_iso3           VARCHAR(3)  NULL
    region_code            VARCHAR     NULL              -- source-native sub-national code
    lat                    DOUBLE      NULL
    lon                    DOUBLE      NULL
    value                  DOUBLE      NULL
    unit                   VARCHAR     NULL

Indexes: `(indicator_id, observation_ts)`, `(country_iso3, indicator_id, observation_ts)`.

### 4.5 Operational Tables

    sources                          -- per-source registration, last_successful_fetch, license, freshness_threshold_seconds
    backfill_state                   -- per-source backfill resume tokens
    ingest_anomalies                 -- OQ-007 anomaly log
    cycle_log                        -- one row per cycle, references the structured JSON in logs/
    schema_migrations                -- DuckDB schema version tracking

### 4.6 Freshness View (REQ-PROV-002)

    CREATE VIEW freshness AS
    SELECT
      s.source_id,
      s.last_successful_fetch,
      EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - s.last_successful_fetch)) AS seconds_since_fetch,
      s.freshness_threshold_seconds,
      (EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - s.last_successful_fetch)) > s.freshness_threshold_seconds) AS is_stale
    FROM sources s;

## 5. Persistence Layer Detail

### 5.1 Staging-Merge Implementation (OQ-005)

```python
def staging_merge(conn, table: str, batch: pa.Table, dedup_keys: list[str]) -> int:
    staging = f"_staging_{table}"
    conn.execute(f"DELETE FROM {staging};")
    conn.register("batch", batch)
    conn.execute(f"INSERT INTO {staging} SELECT * FROM batch;")
    conn.execute(f"INSERT INTO {staging} SELECT * FROM {staging} ORDER BY {','.join(dedup_keys)};")  # sort for OQ-005
    sql = f"""
        INSERT INTO {table}
        SELECT s.* FROM {staging} s
        ON CONFLICT ({', '.join(dedup_keys)})
        DO UPDATE SET
          superseded_at = CURRENT_TIMESTAMP
        WHERE {table}.source_payload_json IS DISTINCT FROM EXCLUDED.source_payload_json;
    """
    return conn.execute(sql).fetchone()[0]
```

Notes:
- Arrow tables are used as the in-memory format for batches. Pandas DataFrames are converted to Arrow before staging.
- The `IS DISTINCT FROM` clause means re-fetches of unchanged records are no-ops (REQ-PERSIST-003).
- Source revisions (changed payload for the same `source_record_id`) trigger an update to `superseded_at` on the prior row and an insert of the new row (REQ-PERSIST-004). The actual implementation uses a two-statement transaction; the SQL above is illustrative.

### 5.2 Schema Migrations

Schema versions are tracked in `schema_migrations` (`version`, `applied_at`, `description`). Each migration is a Python module under `data_ingest/persistence/migrations/` named `mNNNN_<description>.py` with `up(conn)` and `down(conn)` functions. Migrations run on every store open if version mismatch is detected.

REQ-EXT-002 is enforced by code review and by the absence of a "create new schema" code path in the connector base class.

### 5.3 Disk Budget Tracking

A small daemon-like check runs at the end of each cycle: it queries DuckDB file size and reports against the global cap. When usage exceeds 80%, a warning is logged. When usage exceeds 95%, all backfills are paused. Incremental cycles continue but log critical alerts.

## 6. Logging & Observability (REQ-LOG-*)

### 6.1 Structured Cycle Log

Every cycle writes a single JSON line to `logs/cycles/cycle-<iso8601>.jsonl`:

    {
      "cycle_id": "uuid",
      "started_at": "2026-05-14T08:00:00Z",
      "ended_at": "2026-05-14T08:14:23Z",
      "duration_seconds": 863,
      "connectors": [
        {
          "source_id": "fred",
          "status": "ok",
          "records_ingested": 1247,
          "records_skipped_duplicate": 0,
          "duration_seconds": 12.4,
          "errors": []
        },
        {
          "source_id": "acled",
          "status": "partial",
          "records_ingested": 8432,
          "records_skipped_duplicate": 0,
          "duration_seconds": 47.2,
          "errors": [
            { "type": "rate_limit", "retries": 3, "final_outcome": "succeeded" }
          ]
        },
        {
          "source_id": "noaa",
          "status": "failed",
          "records_ingested": 0,
          "duration_seconds": 91.0,
          "errors": [
            { "type": "http_5xx", "endpoint_class": "cdo_api", "retries": 5, "final_outcome": "failed" }
          ]
        }
      ],
      "stale_sources": ["who_don"],
      "anomalies_detected": []
    }

### 6.2 Credential Redaction (REQ-LOG-002)

A logging filter strips:
- Any string matching `[A-Za-z0-9_-]{32,}` that originates from environment variables in the `<SOURCE>_API_KEY` namespace.
- Query strings from URL-style logged values.
- HTTP headers containing `authorization`, `x-api-key`, `cookie`.

Filter is applied at the `structured.py` layer for both INFO and ERROR logs. Tested with synthetic credentials in unit tests.

## 7. Configuration

### 7.1 `ingest_schedule.yaml`

    version: 1
    defaults:
      max_workers: 4
      batch_size: 10000
    sources:
      fred:
        cadence: daily
        time_of_day: "08:00"
        freshness_threshold_seconds: 172800   # 2 days
      acled:
        cadence: daily
        time_of_day: "09:00"
        freshness_threshold_seconds: 259200   # 3 days
      gdelt_events:
        cadence: daily
        freshness_threshold_seconds: 86400
      worldbank:
        cadence: weekly
        day_of_week: monday
      usgs_minerals:
        cadence: annual

### 7.2 `source_caps.yaml`

    version: 1
    global:
      max_corpus_bytes: 107374182400      # 100 GB
      warn_at_pct: 80
      pause_backfill_at_pct: 95
    per_source:
      gdelt_events:
        max_backfill_years: 5
        max_bytes: 32212254720             # 30 GB
      acled:
        max_backfill_years: 30
      fred:
        max_backfill_years: 50

### 7.3 Environment Variables

Credentials only. No data paths or operational config in `.env`:

    FRED_API_KEY=
    ACLED_USERNAME=
    ACLED_PASSWORD=
    NRC_ADAMS_API_KEY=
    REGULATIONS_GOV_API_KEY=
    NOAA_CDO_TOKEN=
    EIA_API_KEY=

ACLED uses OAuth 2.0 password grant (REQ-ACLED-AUTH-001) and so requires both `ACLED_USERNAME` and `ACLED_PASSWORD` rather than a single API key. Tokens are obtained at runtime and cached in process memory; they are never persisted to disk and never written to `.env`.

## 8. Threat Model

Threat context is STANDARD. The principal risks for this subsystem:

1. **Credential leakage.** Mitigation: REQ-SRC-003 + REQ-LOG-002 + .gitignore enforcement + filter at log layer. Verification: synthetic-credential log-scan test.
2. **License violation.** Mitigation: license assertion on connector startup, license flag persisted in `sources` table, downstream subsystems must check before exporting derived data. Specifically applies to ACLED CC BY-NC 4.0.
3. **Disk exhaustion.** Mitigation: per-source caps + global corpus cap + cap-checking before commit + warning at 80% / pause at 95%.
4. **Source-side ToS drift.** Mitigation: licenses are recorded in `sources` table with `verified_at` timestamp. A quarterly review item is added to the project's open threads to re-check ToS for each source.
5. **Untrusted source content treated as instructions.** Source content (especially document payloads from NRC ADAMS, Federal Register, regulations.gov) may contain text that looks like instructions. This subsystem does not interpret content; it stores. Downstream subsystems must treat content as untrusted data.

This subsystem does not connect to Polymarket. Polymarket auth (FULL threat context) is handled separately in `polymarket_connector`.

## 9. Test Strategy

### 9.1 Unit Tests

Per-connector tests using recorded HTTP fixtures (via `pytest-recording` or equivalent). Each connector has fixtures for:
- A normal incremental fetch (returns N records).
- An empty fetch (no new data).
- A rate-limited fetch (429 → retry → success).
- A persistent failure (5xx after max retries).
- A source revision (same `source_record_id`, changed payload).

Normalization tests confirm round-tripping: `(raw → normalized → query) → original payload recoverable`.

### 9.2 Integration Tests

In-memory DuckDB store. Tests:
- Full cycle with three mock connectors (one ok, one rate-limited-then-ok, one failing).
- Re-run idempotency (REQ-PERSIST-003).
- Source revision handling (REQ-PERSIST-004).
- Backfill resume after simulated interruption (REQ-BACKFILL-002).
- Cap enforcement (REQ-BACKFILL-003 + global cap).
- Freshness view correctness after a cycle with mixed-success sources.
- Credential leak prevention (REQ-LOG-002) with synthetic keys.

### 9.3 Smoke Tests

A `make smoke` target runs a real-network single-record pull from each free unauthenticated source (FRED with sandbox key, World Bank, GDELT). Authenticated sources are skipped in CI but run locally before any release.

### 9.4 Acceptance Test

The full acceptance test runs against the operator's hardware:
1. Fresh DuckDB.
2. Backfill all v1 sources to their default depth.
3. Confirm corpus size under 100 GB and wall-clock under 72 hours (NFR-PERF-002).
4. Run a daily incremental cycle. Confirm under 30 minutes (NFR-PERF-001).
5. Disable network on one source mid-cycle. Confirm cycle continues and produces correct partial-success report.
6. Query each canonical schema and confirm provenance round-trips.

## 10. Operational Model

### 10.1 First-Run

    razor-rooster ingest init                    # creates DuckDB, runs migrations, registers sources
    razor-rooster ingest backfill --source fred  # one source at a time, monitor disk
    razor-rooster ingest backfill --all          # everything else, parallel-bounded

### 10.2 Steady-State

A cron job (or `launchd` agent on macOS) runs:

    razor-rooster ingest cycle

once per day at the configured time. The cycle runs all sources due that day, writes its log, and exits. No long-running daemon.

### 10.3 Recovery

If DuckDB is corrupted (rare; DuckDB has WAL):

    razor-rooster ingest verify                  # checks integrity, reports issues
    razor-rooster ingest restore --from <backup> # restores from snapshot

Snapshots are not implemented in v1 of this subsystem (responsibility of operator-level backup; documented in operator README, not in code).

### 10.4 Source Addition (REQ-EXT-001)

Adding a source:
1. Implement connector subclass under `connectors/`.
2. Register in `registry.py` (or via decorator registration if implemented).
3. Add entry to `ingest_schedule.yaml`.
4. Run `razor-rooster ingest backfill --source <new_id>`.
5. Confirm participation in next regular cycle.

No changes to `pattern_library` or any downstream subsystem are required as long as the new source maps to one of the four canonical schemas.

## 11. Performance Notes & Risks

- **Pandas vs Polars vs Arrow:** Arrow is the canonical in-memory format for batches because DuckDB's Arrow integration is the fastest ingest path. Pandas is used only at connector boundaries where source SDKs return DataFrames; conversion to Arrow happens before staging.
- **GDELT events backfill** is the highest-risk performance area. 5 years of events at GDELT's cadence is millions of rows. Initial backfill runs may take hours per year. The backfill resume mechanism (REQ-BACKFILL-002) is critical here.
- **DuckDB single-connection contention:** v1 uses a single DuckDB connection serialized across connector workers via a connection-pool wrapper. This is a known scalability ceiling. If we hit it, the next iteration moves to one DuckDB file per canonical-schema table with cross-table queries via `ATTACH`.
- **NOAA CDO API** has a documented hard limit of 1,000 requests per day for free-tier tokens. Backfill of historical climate indicators must be paced, and large historical pulls should use the bulk-archive paths (NCEI direct downloads) where available rather than the CDO endpoint.

## 12. Deferred to Implementation

These items remain to be settled empirically during implementation, not in the design phase:

- **DEFER-001:** Exact GDELT events backfill disk footprint at 5-year depth on the operator's hardware. Measure before raising or lowering the 30 GB per-source cap.
- **DEFER-002:** Whether `httpx` async or synchronous-with-thread-pool gives better throughput for connector concurrency. Default to thread pool; revisit if cycle exceeds NFR-PERF-001.
- **DEFER-003:** Whether to bundle a small SQLite-backed sources/sources-credentials store separately from DuckDB, to keep config tidy. Default: keep in DuckDB.
- **DEFER-004:** Per-source content-size cap on `document_docket.full_text_local_path`. Some Federal Register and NRC ADAMS documents are large PDFs. Default: do not store full text locally in v1; store only `full_text_uri` and let downstream fetch on demand.
- **DEFER-005:** Backup strategy. v1 documents a manual snapshot procedure; automation is out of scope for this subsystem.

## 13. References

- Requirements spec: `DATA_INGEST.md` v0.1.0
- LOOM v0.3.0: `razorrooster.md`
- System prompt v0.2: `razorrooster-prompt.md.txt`
- ACLED license note: data published under CC BY-NC 4.0; see ACLED's terms of use.
- DuckDB upsert behavior: GitHub issue #11275 (slow upserts on unsorted input) and the DuckDB performance tuning guide.
- GDELT GKG 2.0 reported size figures from the GDELT project's own posts.
- NRC ADAMS Public Search API developer portal: `adams-api-developer.nrc.gov`.

Content drawn from external sources is paraphrased per licensing constraints.
