# POLYMARKET_CONNECTOR — Design

**Subsystem:** `polymarket_connector`
**Codename:** The Wire
**Spec version:** 0.1.0 (Design draft)
**Status:** PROPOSED
**Threat context:** STANDARD (v1 read-only)
**Last updated:** 2026-05-14
**Companion spec:** `POLYMARKET_CONNECTOR.md` (Requirements v0.1.0)

---

## 1. Overview

This document specifies the technical design for `polymarket_connector` v1. It maps the requirements in `POLYMARKET_CONNECTOR.md` to a concrete architecture: API client, table layout, sync orchestration, sector mapping, rate limiting, and the operational gates (geo-restriction, ToS acknowledgement). It also resolves the open questions from the requirements phase.

Three discipline rules carry over from `data_ingest`:

1. **Source-native preservation.** Source payloads stored verbatim alongside normalized columns.
2. **Failure isolation.** Polymarket-side failure cannot corrupt or block `data_ingest`.
3. **No silent ingestion.** Cycle reports show what synced, what failed, what was skipped.

Two more specific to this subsystem:

4. **No wallet code paths in v1.** The codebase contains no signing infrastructure, no funder address handling, no API credential loaders for Polymarket. v2 trading work will reintroduce these in a separate spec amendment with FULL threat context.
5. **ToS and geo gates are non-bypassable.** The startup-time gates fail closed: misconfiguration prevents the connector from running, and the failure is loud.

## 2. Resolved Open Questions

### OQ-PMC-001 — Polymarket category taxonomy mapping

**Resolution:** Implement a translation layer. Polymarket's category/subcategory taxonomy does not align with the six Razor-Rooster sectors and changes over time without versioning. A static one-to-one mapping would silently break.

**Design implications:**
- Table `polymarket_sector_mapping` (`condition_id`, `razor_sector`, `confidence` ∈ {`exact`, `inferred`, `manual`}, `mapped_at`, `mapped_by`).
- A small heuristic mapper inspects Polymarket category, subcategory, tags, and question text to assign a sector with `confidence = inferred`. Markets the heuristic can't classify get `razor_sector = NULL` and surface in a "needs review" report.
- Operator can override via a CLI command (`razor-rooster polymarket map <condition_id> <sector>`); these get `confidence = manual`.
- Heuristic mapper is conservative: better to leave a market unmapped than misclassify.

### OQ-PMC-002 — RTDS WebSocket inclusion

**Resolution:** Defer to v2.

**Reasoning:** v1 hourly snapshot cadence is sufficient for `mispricing_detector` use cases as currently understood. RTDS adds long-lived-connection complexity (reconnect logic, missed-message backfill, interleave with REST snapshots) that doesn't pay back at v1 scale. The deferral is recorded explicitly so it doesn't quietly stay deferred forever; if `mispricing_detector` requirements end up demanding sub-hour freshness on a meaningful set of markets, RTDS comes back in scope as a v1.5.

**Design implications:**
- `polymarket_price_snapshots.source` column stays in the schema (tagged `'rest'` for v1) so v2 can add `'rtds'` without migration.
- No `websocket` module created; `connectors/polymarket/` is HTTP-only.

### OQ-PMC-003 — Resolution backfill depth

**Resolution:** Pull whatever Gamma exposes; that's currently the full historical record (Polymarket has been live since 2020 with on-chain settlement, so the data is bounded and recoverable). No need to supplement with third-party mirrors in v1.

**Design implications:**
- `backfill_resolutions()` paginates Gamma's resolved-markets endpoint until exhausted.
- Backfill is resumable (per `data_ingest` REQ-BACKFILL-002 conventions).
- No third-party-archive code path. If a future audit reveals gaps, that's a v2 question.

### OQ-PMC-004 — Multi-outcome markets and CTF tokens

**Resolution:** Support binary YES/NO markets in v1. Multi-outcome markets (3+ outcomes) and negative-risk multi-condition baskets are deferred to v1.1 with their own spec amendment.

**Reasoning:** Most Polymarket volume and most analytically useful markets are binary. Multi-outcome adds schema complexity (variable-length outcome arrays, mutually-exclusive resolution semantics, basket pricing) that's not worth front-loading. The v1 schema uses arrays for `outcome_tokens` so multi-outcome can extend the schema without rewriting it.

**Design implications:**
- `polymarket_markets.outcome_tokens` is JSON-typed and stores the full token list even when there are >2.
- `polymarket_price_snapshots` stores one row per (market, outcome_token) pair, not per market. Binary markets produce two rows per snapshot; multi-outcome markets produce N. The schema accommodates this from day one.
- The v1 sync logic filters out markets with >2 outcomes from active processing (they're persisted in `polymarket_markets` for future reference but no price snapshots are taken for them).
- A `polymarket_markets.market_type` column captures `'binary'` | `'multi'` | `'negrisk'` for filtering.

### OQ-PMC-005 — Polymarket vs Polymarket US

**Resolution:** v1 targets the main Polymarket platform (`gamma-api.polymarket.com`). The US-regulated platform is treated as a distinct future source if and when relevant.

**Reasoning:** The two platforms have separate market universes, separate resolution mechanisms, and distinct regulatory perimeters. Treating them as one source would be wrong; treating them as two is a v2 concern. The geo-restriction gate (REQ-PMC-GEO-001) handles the operator-side jurisdiction question independently of which platform is being read.

**Design implications:**
- `sources` table entry: `polymarket` (single source identifier, main platform).
- A future `polymarket_us` source would be added as a separate entry with its own connector module sharing most of the codebase.

### OQ-PMC-006 — Sector mapping (one-to-one vs many)

**Resolution:** One primary sector per market, plus an optional `secondary_sectors` JSON array for markets that span multiple. Settled by the structure of `polymarket_sector_mapping` defined under OQ-PMC-001.

**Design implications:**
- `polymarket_sector_mapping.razor_sector` is the single primary.
- `polymarket_sector_mapping.secondary_sectors` (JSON array) captures additional sector tags.
- Downstream subsystems (`mispricing_detector`, `pattern_library`) can query either or both depending on use case.

### OQ-PMC-007 — Live-data freshness threshold

**Resolution:** 6 hours for live price snapshots. 48 hours for resolutions. Both are configurable.

**Reasoning:** Hourly snapshot cadence (REQ-PMC-PRICE-003) means a snapshot older than ~6 hours indicates real upstream trouble (network, rate-limit exhaustion, or Polymarket-side outage). 48 hours for resolutions accounts for the slower update cadence of the resolutions endpoint. Tighter thresholds are configurable per-market for watched markets.

## 3. Architecture

### 3.1 Module Layout

    razor_rooster/
      polymarket_connector/
        __init__.py
        cli.py                          # commands: sync, snapshot, backfill-resolutions, map, ack-tos
        client/
          __init__.py
          gamma.py                      # Gamma API: markets, events, resolutions
          clob_public.py                # CLOB public REST: prices, orderbook, trades
          rate_limit.py                 # token-bucket limiter
          retry.py                      # exponential backoff with jitter
          user_agent.py                 # NFR-PMC-TOS-001
        sync/
          __init__.py
          markets.py                    # daily metadata reconciliation
          prices.py                     # cadence-based price snapshots
          resolutions.py                # daily resolution delta + initial backfill
          trades.py                     # opt-in per-market trade pull
          orderbook.py                  # on-demand orderbook pull
        mapping/
          __init__.py
          sector_heuristic.py           # OQ-PMC-001 heuristic mapper
          sector_overrides.py           # operator manual mappings
        gates/
          __init__.py
          geo.py                        # REQ-PMC-GEO-001 startup gate
          tos.py                        # REQ-PMC-TOS-001 startup gate
        persistence/
          __init__.py
          schemas.py                    # Polymarket-namespaced table DDL
          migrations.py                 # m0001_polymarket_initial.py et al
        config/
          polymarket.yaml               # cadence, watched markets, freshness thresholds
        tests/
          fixtures/
            gamma_markets_response.json
            clob_price_response.json
            resolved_markets_response.json
            ...

### 3.2 Reuse from `data_ingest`

The connector consumes these from `data_ingest`:

- `DuckDBStore` and the connection-pool wrapper (T-012) for persistence.
- The migrations framework (T-013) for schema changes.
- The staging-merge upsert pattern (T-014) for batch writes.
- The provenance helpers (T-015) for `last_successful_fetch` updates and freshness-view participation.
- The structured logging layer (T-021) and credential-redaction filter.
- The schedule-cadence machinery (T-033) — Polymarket sync is registered as a virtual "source" in the same scheduler.

Polymarket sync runs as part of the `data_ingest` cycle (operator runs `razor-rooster ingest cycle`, the cycle includes Polymarket sync). It does not run as a separate top-level command, though `razor-rooster polymarket <op>` exists for ad-hoc operations.

### 3.3 Tables (Polymarket namespace)

These five tables live under the `polymarket_*` namespace in the same DuckDB file as `data_ingest`'s canonical schemas. They share the provenance prefix from `data_ingest` design §4 but their non-prefix columns are Polymarket-specific.

#### `polymarket_markets`

    [provenance prefix from data_ingest §4]
    condition_id              VARCHAR     PRIMARY KEY
    slug                      VARCHAR     NOT NULL
    question                  TEXT        NOT NULL
    description               TEXT        NULL
    category                  VARCHAR     NULL              -- Polymarket-side
    subcategory               VARCHAR     NULL
    tags                      JSON        NULL
    event_id                  VARCHAR     NULL              -- Polymarket event grouping
    market_type               VARCHAR     NOT NULL          -- 'binary' | 'multi' | 'negrisk'
    outcome_tokens            JSON        NOT NULL          -- list of {token_id, outcome_label}
    end_date                  TIMESTAMP   NULL              -- expected resolution
    active                    BOOLEAN     NOT NULL
    closed                    BOOLEAN     NOT NULL
    resolved                  BOOLEAN     NOT NULL
    volume_lifetime           DOUBLE      NULL
    created_at_polymarket     TIMESTAMP   NULL
    last_updated_polymarket   TIMESTAMP   NULL
    removed_at                TIMESTAMP   NULL              -- non-NULL when market disappears from Polymarket

Indexes: `(active, end_date)`, `(category)`, `(market_type)`, `(event_id)`.

#### `polymarket_price_snapshots`

    [provenance prefix]
    condition_id              VARCHAR     NOT NULL
    outcome_token_id          VARCHAR     NOT NULL
    snapshot_ts               TIMESTAMP   NOT NULL
    mid_price                 DOUBLE      NULL
    best_bid                  DOUBLE      NULL
    best_ask                  DOUBLE      NULL
    last_trade_price          DOUBLE      NULL
    last_trade_ts             TIMESTAMP   NULL
    volume_24h                DOUBLE      NULL
    liquidity_warning         BOOLEAN     NOT NULL  DEFAULT FALSE
    spread_bps                INTEGER     NULL              -- (ask-bid)/mid in basis points, NULL if either side missing
    source                    VARCHAR     NOT NULL          -- 'rest' (v1) | 'rtds' (v2+)

Primary key: `(condition_id, outcome_token_id, snapshot_ts)`.
Indexes: `(condition_id, snapshot_ts)`, `(snapshot_ts)`.

#### `polymarket_orderbook_snapshots` (opt-in only)

    [provenance prefix]
    condition_id              VARCHAR     NOT NULL
    outcome_token_id          VARCHAR     NOT NULL
    snapshot_ts               TIMESTAMP   NOT NULL
    side                      VARCHAR     NOT NULL          -- 'bid' | 'ask'
    level                     INTEGER     NOT NULL          -- 0 = best
    price                     DOUBLE      NOT NULL
    size                      DOUBLE      NOT NULL

Primary key: `(condition_id, outcome_token_id, snapshot_ts, side, level)`.
Index: `(condition_id, snapshot_ts)`.

#### `polymarket_trades` (opt-in via watched_markets only)

    [provenance prefix]
    condition_id              VARCHAR     NOT NULL
    outcome_token_id          VARCHAR     NOT NULL
    trade_ts                  TIMESTAMP   NOT NULL
    price                     DOUBLE      NOT NULL
    size                      DOUBLE      NOT NULL
    side                      VARCHAR     NULL              -- 'buy_yes' | 'sell_yes' if determinable
    tx_hash                   VARCHAR     NOT NULL          -- Polygon tx hash, used for dedup

Primary key: `(tx_hash, outcome_token_id)`.
Indexes: `(condition_id, trade_ts)`.

#### `polymarket_resolutions`

    [provenance prefix]
    condition_id              VARCHAR     PRIMARY KEY
    winning_outcome_token_id  VARCHAR     NULL              -- NULL if invalid/refunded
    winning_outcome_label     VARCHAR     NULL              -- e.g. "Yes"
    resolution_ts             TIMESTAMP   NOT NULL
    resolution_source         VARCHAR     NOT NULL          -- e.g. 'uma_oracle'
    resolution_metadata       JSON        NULL              -- source-native verbatim
    final_yes_price           DOUBLE      NULL
    final_no_price            DOUBLE      NULL
    total_volume_at_resolution DOUBLE     NULL
    invalidated               BOOLEAN     NOT NULL  DEFAULT FALSE

Index: `(resolution_ts)`.

#### `polymarket_sector_mapping`

    condition_id              VARCHAR     PRIMARY KEY
    razor_sector              VARCHAR     NULL              -- one of the 6 Razor-Rooster sectors, or NULL if unmapped
    secondary_sectors         JSON        NULL              -- list of additional sectors
    confidence                VARCHAR     NOT NULL          -- 'exact' | 'inferred' | 'manual'
    mapped_at                 TIMESTAMP   NOT NULL
    mapped_by                 VARCHAR     NOT NULL          -- 'heuristic_v<version>' | 'operator'

Index: `(razor_sector)`.

### 3.4 Sync Operations

#### Daily metadata sync (REQ-PMC-MARKET-003)

    1. Fetch all active markets from Gamma `/markets?active=true&closed=false&limit=<page>`.
    2. Paginate to exhaustion.
    3. Upsert into `polymarket_markets` via staging-merge.
    4. Mark markets present in DB but absent from response with `removed_at = now()`.
    5. For each new or changed market, run sector heuristic mapper and upsert `polymarket_sector_mapping`.
    6. Update `sources.last_successful_fetch` for `polymarket`.

#### Hourly price snapshots (REQ-PMC-PRICE-001..004)

    1. Determine markets due: active markets per cadence config (default hourly), plus any watched_markets with overrides.
    2. For each market, for each outcome_token_id:
       a. Call CLOB public price endpoint.
       b. Compute spread_bps and liquidity_warning per REQ-PMC-PRICE-004 threshold.
       c. NULL-preserve missing fields.
    3. Batch upsert into `polymarket_price_snapshots` via staging-merge.
    4. Rate limiter applied across the entire batch — no parallel bursts.

#### Resolution backfill + daily delta (REQ-PMC-RES-001..003)

    Backfill (one-time):
    1. Fetch all resolved markets from Gamma `/markets?closed=true&resolved=true`.
    2. Paginate; on each page, upsert into `polymarket_resolutions` and `polymarket_markets` (the resolved markets are also markets).
    3. Save resume token between pages.

    Daily delta:
    1. Fetch resolved markets where `resolved_at >= sources.last_successful_fetch[polymarket_resolutions]`.
    2. Upsert as above.
    3. For each newly resolved market, mark `polymarket_markets.resolved = true` and `closed = true`.

#### Trades pull (REQ-PMC-TRADE-001..003)

    Triggered only for watched_markets:
    1. For each market_id in watched_markets, fetch trades since last successful pull.
    2. Upsert into `polymarket_trades` keyed by `(tx_hash, outcome_token_id)` for dedup against re-pulls.
    3. Skip markets with no new trades quickly.

#### Orderbook pull (REQ-PMC-OB-001..002)

On-demand only, invoked from CLI or by `mispricing_detector`. Returns in-memory result; persists only if `persist=True`.

### 3.5 Sector Heuristic Mapper

`mapping/sector_heuristic.py`:

    def map_sector(market: PolymarketMarket) -> SectorMapping:
        """Heuristic mapping. Returns SectorMapping with confidence='inferred' or razor_sector=None."""
        # Pass 1: exact category-name match against curated lookup.
        # Pass 2: keyword scan over question/description against per-sector keyword sets:
        #   public_health: ["pandemic", "outbreak", "WHO", "vaccine", "PHEIC", "disease", ...]
        #   geopolitical: ["election", "war", "ceasefire", "sanctions", "coup", "treaty", ...]
        #   regulatory:   ["FDA", "EPA", "Congress", "Supreme Court", "rule", "executive order", ...]
        #   commodity:    ["oil", "gas", "wheat", "copper", "OPEC", "BDI", ...]
        #   climate:      ["hurricane", "drought", "wildfire", "ENSO", "NOAA", ...]
        #   infrastructure_energy: ["grid", "blackout", "refinery", "pipeline", ...]
        # Pass 3: if multiple sectors hit, pick the highest-scoring; tie → return None (operator review).
        # Returns: razor_sector | None, secondary_sectors (other hits), confidence
        ...

The keyword sets live in `mapping/sector_keywords.yaml` so they can be tuned without code changes. The mapper logs every classification for later review, with the inputs that drove the decision.

Markets returning `razor_sector = None` show up in a `razor-rooster polymarket needs-review` CLI command for operator triage.

### 3.6 Rate Limiting

Token-bucket limiter (`client/rate_limit.py`):

- Bucket capacity: 50 (= 50% of Polymarket's 100 req/sec firm-wide cap, per REQ-PMC-RATE-001).
- Refill rate: 50 tokens/sec.
- All HTTP clients in `client/` go through a shared limiter instance — no parallel limiter pools that could collectively exceed cap.
- On 429 response: drain the bucket fully and apply `client/retry.py` exponential backoff with jitter (capped at 5 retries).

Concurrency: the connector uses `httpx.AsyncClient` for parallelism but the limiter is the choke point. Workers acquire a token before each request and block (with timeout) if the bucket is empty.

### 3.7 Geo-Restriction Gate

`gates/geo.py` runs at every connector startup:

    def check_jurisdiction() -> None:
        jurisdiction = (
            os.environ.get("OPERATOR_JURISDICTION")
            or load_yaml("config/operator.yaml").get("jurisdiction")
        )
        if jurisdiction is None:
            raise StartupRefusal(
                "OPERATOR_JURISDICTION not configured. Set the env var or "
                "config/operator.yaml jurisdiction field. polymarket_connector "
                "refuses to run without explicit jurisdiction declaration."
            )
        if jurisdiction.upper() in RESTRICTED_JURISDICTIONS:
            raise StartupRefusal(
                f"Jurisdiction '{jurisdiction}' is on Polymarket's restricted list. "
                "polymarket_connector cannot run from this jurisdiction. "
                "If you believe this is incorrect, see Polymarket's geographic "
                "restrictions documentation."
            )

`RESTRICTED_JURISDICTIONS` is a config constant, not a code constant — `config/restricted_jurisdictions.yaml` lists the jurisdictions Polymarket explicitly geoblocks. Updating the list does not require a code change.

The gate fails closed: missing config = refuse. There is no "I don't know my jurisdiction" code path.

### 3.8 ToS Acknowledgement Gate

`gates/tos.py`:

    def check_tos_acknowledged(store: DuckDBStore) -> None:
        current_hash = fetch_tos_version_hash()  # SHA-256 of canonical ToS text
        ack = store.query_one(
            "SELECT tos_version_hash, acknowledged_at FROM sources WHERE source_id = 'polymarket'"
        )
        if ack is None or ack["tos_version_hash"] != current_hash:
            raise ToSAcknowledgementRequired(
                tos_version_hash=current_hash,
                tos_url=POLYMARKET_TOS_URL,
                cli_command="razor-rooster polymarket ack-tos",
            )

`razor-rooster polymarket ack-tos` displays the current ToS URL, prompts for confirmation (interactive), and on operator confirmation writes the hash and timestamp to `sources`. No subsequent connector operation runs without this entry.

ToS hash is fetched from a stable canonical URL. If the URL is unreachable on startup, the gate falls back to the last-known hash recorded in the `tos_version_history` table. If that also doesn't match, the connector refuses to start.

### 3.9 Failure Isolation

Per design rule 2: a Polymarket-side failure cannot block `data_ingest`.

Implementation:
- The Polymarket sync registers as a connector in `data_ingest`'s scheduler with `source_id = 'polymarket'`.
- The scheduler's existing failure-isolation contract (REQ-SRC-004) applies: a failure in the Polymarket sync is logged structured, marked in the cycle report, and does not interrupt other sources.
- Polymarket-side reads do not hold cross-table locks; their writes are confined to `polymarket_*` tables and `sources`.
- A long Polymarket outage causes freshness-view staleness for the `polymarket` source row, which downstream consumers (`mispricing_detector`) check before producing stale-data analyses.

## 4. Sync Cadence Configuration

`config/polymarket.yaml`:

    version: 1
    sync:
      markets:
        cadence: daily
        time_of_day: "08:30"
      prices:
        default_cadence: hourly
        minimum_interval_seconds: 60
        watched_markets: []                # populated by mispricing_detector or operator
      resolutions:
        cadence: daily
        time_of_day: "08:45"
      trades:
        cadence: daily                     # only runs against watched_markets
        time_of_day: "09:00"
    rate_limit:
      bucket_capacity: 50
      refill_per_second: 50
      backoff_base_seconds: 1
      backoff_max_seconds: 60
      max_retries: 5
    freshness:
      markets_threshold_seconds: 172800    # 48h
      prices_threshold_seconds: 21600      # 6h
      resolutions_threshold_seconds: 172800
    sector_mapping:
      heuristic_version: 1
      keywords_file: "config/sector_keywords.yaml"

## 5. Logging

Every sync operation emits a structured JSON log entry:

    {
      "operation": "polymarket_sync_markets",
      "started_at": "...",
      "ended_at": "...",
      "duration_seconds": ...,
      "markets_total_seen": ...,
      "markets_inserted": ...,
      "markets_updated": ...,
      "markets_removed": ...,
      "rate_limit_throttle_events": ...,
      "errors": [...]
    }

The credential-redaction filter from `data_ingest` T-021 still applies — there are no Polymarket credentials in v1 to redact, but the filter remains in place as defense in depth (and to catch any accidental leak from operator-side identifiers).

## 6. Threat Model

Threat context: STANDARD (v1 read-only).

Principal risks for this subsystem:

1. **Geo-restriction violation.** Mitigation: REQ-PMC-GEO-001, REQ-PMC-GEO-002 + the gate in §3.7. Verification: gate test.
2. **ToS drift.** Mitigation: ToS hash check on every startup + version-history table. Verification: simulated version-change test re-prompts.
3. **Rate-budget exhaustion.** Mitigation: 50% headroom + token-bucket limiter + structured warnings on throttle events.
4. **Inadvertent trading code path.** Mitigation: code review checklist explicitly forbids importing any `*_l1.py` or `*_l2.py` style modules from Polymarket SDKs in v1; `pyproject.toml` does not include the Polymarket-trading SDK as a dependency. Only the public-data SDK paths (or hand-written REST clients) are used.
5. **Untrusted source content.** Polymarket question text and descriptions are user-generated content. They may contain instruction-like text. This subsystem does not interpret content; it stores. Downstream consumers must treat content as untrusted data per `data_ingest` threat-model rule 5.

When v2 adds trading, threat context for the affected paths returns to FULL. A separate spec amendment then specifies wallet handling, EIP-712 signing, key custody, and operator-side authorization gates.

## 7. Test Strategy

### 7.1 Unit Tests

Per-module tests using recorded fixtures:

- Gamma API parsing: markets, events, resolutions response shapes.
- CLOB public parsing: price, orderbook, trades response shapes.
- Sector heuristic mapper: representative inputs across the six sectors plus unmappable cases.
- Rate limiter: bucket drain/refill behavior, blocking under empty bucket, recovery after 429.
- Geo gate: refusal for restricted, pass for permitted, refusal for missing config.
- ToS gate: refusal without ack, pass after ack, re-prompt on hash change.
- NULL preservation: thin-orderbook fixture confirms NULLs and `liquidity_warning = TRUE`.

### 7.2 Integration Tests

Against in-memory DuckDB:

- Full daily sync against a mock Polymarket API state. Confirm `polymarket_markets`, `polymarket_price_snapshots`, `polymarket_resolutions` populated correctly.
- Re-run idempotency (no duplicate snapshots).
- Market disappearance: market in DB but absent from response → `removed_at` set.
- Resolution: market becomes resolved between syncs → `polymarket_resolutions` row + `polymarket_markets.resolved = true`.
- Backfill resume: interrupt mid-pagination, resume completes.
- Failure isolation: mock 5xx on Polymarket; confirm `data_ingest` cycle continues, Polymarket marked failed in cycle report.

### 7.3 Smoke Tests

`make smoke-polymarket` runs against the live Polymarket API:

- Fetch 5 active markets.
- Snapshot prices for those 5 markets.
- Fetch 1 resolved market.
- Fetch one orderbook (without persist).
- Confirm rate-limit headroom not exceeded.

Skipped in CI; run locally before any release that touches this subsystem.

### 7.4 Acceptance Test

On operator hardware:

- Fresh DuckDB → Polymarket migrations applied → ToS ack gate prompts → operator acks.
- Geo gate confirms permitted jurisdiction.
- Daily sync runs end-to-end inside NFR-PMC-PERF-001 (5 min).
- Resolution backfill runs inside NFR-PMC-PERF-002 (12 hr).
- Steady-state disk usage stays inside NFR-PMC-PERF-003 (5 GB).
- Simulated outage: disable network mid-cycle; confirm graceful degradation.

## 8. Operational Model

### 8.1 First-run

    razor-rooster polymarket ack-tos                       # one-time gate
    razor-rooster polymarket backfill-resolutions          # initial historical pull
    razor-rooster ingest cycle                             # includes Polymarket sync from now on

### 8.2 Steady-state

Polymarket sync runs as part of the daily `razor-rooster ingest cycle`. No separate cron entry needed.

### 8.3 Watched markets

    razor-rooster polymarket watch <condition_id> [--cadence 5min] [--orderbook] [--trades]
    razor-rooster polymarket unwatch <condition_id>
    razor-rooster polymarket list-watched

These edit `config/polymarket.yaml` `watched_markets` list and confirm.

### 8.4 Sector mapping triage

    razor-rooster polymarket needs-review                  # lists unmapped markets
    razor-rooster polymarket map <condition_id> <sector>   # operator override

### 8.5 ToS re-acknowledgement

When ToS hash changes, the next sync attempt fails with a clear message instructing the operator to re-run `razor-rooster polymarket ack-tos`. No automatic re-acknowledgement.

## 9. Performance Notes & Risks

- **Hourly snapshot scale.** Polymarket has thousands of active markets at any time. Each snapshot pulls one row per outcome_token. At ~2,000 active binary markets, that's ~4,000 price snapshots per hour. Inside the 50 req/sec rate budget, this completes in ~80 seconds plus latency. Comfortable.
- **Resolution backfill scale.** Polymarket has resolved tens of thousands of markets since 2020. At Gamma's pagination limits (typically 100/page), this is hundreds of API calls. Inside 12-hour budget, easily.
- **Sector mapper latency.** Heuristic mapper runs in-process; expected latency is sub-millisecond per market. Not a bottleneck.
- **Risk: sudden API surface change.** Polymarket has migrated APIs in the past (the docs explicitly tell new users to migrate to deposit wallets, hinting at architectural churn). The connector's defensive parsing tolerates unknown fields (preserved verbatim in `source_payload_json`); a breaking change in the documented surface causes the connector to fail loud rather than silently mis-parse.
- **Risk: ToS terms changing the read-access posture.** If Polymarket changes its ToS to require authentication for read access, the v1 connector breaks and the operator must add credentials. The ToS-hash gate catches the version change before the connector keeps running on stale assumptions.

## 10. Deferred to Implementation

- **DEFER-PMC-001:** Initial sector keyword sets — start with a small curated list per sector and expand based on `needs-review` triage feedback. The keyword files are config, so this is iterative.
- **DEFER-PMC-002:** Watched-markets cadence floor — REQ-PMC-PRICE-003 sets a 60-second minimum; in practice, validate this stays inside rate budget when multiple watched markets sync at the same cadence. Adjust if needed.
- **DEFER-PMC-003:** Resolution backfill exact pagination strategy — Gamma's pagination is documented but pagination-window edge cases (markets resolved during backfill) need empirical confirmation.

## 11. References

- Requirements spec: `POLYMARKET_CONNECTOR.md` v0.1.0
- `data_ingest` Requirements/Design/Tasks v0.1.0 — for shared infrastructure (DuckDBStore, staging-merge, freshness, scheduler).
- LOOM v0.5.0: `razorrooster.md`
- Polymarket public API documentation: `docs.polymarket.com` (Gamma API, CLOB public, RTDS, rate limits, geoblock).
- Polymarket geographic restrictions documentation.
- Open thread OT-001: resolved by §3 of requirements spec.
- Open thread OT-004: this design implements the recommendation-only/manual-execution disposition (no trading in v1).

Content drawn from external sources is paraphrased per licensing constraints.
