"""T-PMC-041 — hourly price snapshot sync tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.clob_public import (
    CLOB_BASE_URL,
    ClobPublicClient,
)
from razor_rooster.polymarket_connector.client.gamma import (
    GAMMA_BASE_URL,
    GammaClient,
)
from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.markets import sync_markets
from razor_rooster.polymarket_connector.sync.prices import (
    PriceSnapshotReport,
    snapshot_prices,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_prices.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


def _gamma_handler(
    active_payloads: list[dict[str, object]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    active = active_payloads or []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404)
        active_param = request.url.params.get("active")
        if active_param == "true":
            return httpx.Response(200, json=active)
        return httpx.Response(200, json=[])

    return handler


def _market_payload(
    condition_id: str,
    *,
    token_yes: str,
    token_no: str,
    outcomes: tuple[str, str] = ("Yes", "No"),
    active: bool = True,
    closed: bool = False,
) -> dict[str, object]:
    return {
        "conditionId": condition_id,
        "slug": f"market-{condition_id[2:6]}",
        "question": f"Will {condition_id[2:6]} happen?",
        "active": active,
        "closed": closed,
        "outcomes": list(outcomes),
        "clobTokenIds": [token_yes, token_no],
        "category": "Politics",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
    }


def _build_gamma_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=0)


def _build_clob_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ClobPublicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=CLOB_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return ClobPublicClient(http_client=http, bucket=bucket, max_retries=0)


def _seed_one_binary_market(
    store: DuckDBStore,
    condition_id: str,
    *,
    token_yes: str,
    token_no: str,
) -> None:
    """Run sync_markets once so polymarket_markets has a row to snapshot against."""
    payloads = [
        _market_payload(condition_id, token_yes=token_yes, token_no=token_no),
    ]
    handler = _gamma_handler(active_payloads=payloads)
    with _build_gamma_client(handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 14, tzinfo=UTC))


def _full_book_payload(
    *,
    market: str,
    asset_id: str,
    best_bid: float = 0.45,
    best_ask: float = 0.46,
    last_trade: float = 0.45,
) -> dict[str, object]:
    return {
        "market": market,
        "asset_id": asset_id,
        "timestamp": "1700000000",
        "hash": "abc",
        "bids": [
            {"price": str(best_bid), "size": "100"},
            {"price": str(best_bid - 0.01), "size": "200"},
        ],
        "asks": [
            {"price": str(best_ask), "size": "150"},
            {"price": str(best_ask + 0.01), "size": "250"},
        ],
        "min_order_size": "1",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": str(last_trade),
    }


def _empty_side_payload(
    *,
    market: str,
    asset_id: str,
    side_with_levels: str = "bids",
) -> dict[str, object]:
    bids = [{"price": "0.45", "size": "100"}] if side_with_levels == "bids" else []
    asks = [{"price": "0.46", "size": "150"}] if side_with_levels == "asks" else []
    return {
        "market": market,
        "asset_id": asset_id,
        "timestamp": "1700000000",
        "bids": bids,
        "asks": asks,
        "neg_risk": False,
    }


# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------
def test_snapshot_prices_writes_one_row_per_outcome(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(
            200,
            json=_full_book_payload(market="0xabc", asset_id=token),
        )

    explicit_now = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_clob_client(handler) as client:
        report = snapshot_prices(store, client=client, now=explicit_now)

    assert isinstance(report, PriceSnapshotReport)
    assert report.markets_evaluated == 1
    assert report.snapshots_inserted == 2

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT outcome_token_id, mid_price, best_bid, best_ask, "
            "spread_bps, liquidity_warning, snapshot_source "
            "FROM polymarket_price_snapshots "
            "ORDER BY outcome_token_id"
        ).fetchall()

    assert len(rows) == 2
    by_token = {r[0]: r for r in rows}
    yes = by_token["0xabc-yes"]
    assert yes[1] == pytest.approx(0.455)  # mid
    assert yes[2] == pytest.approx(0.45)
    assert yes[3] == pytest.approx(0.46)
    # spread = 0.01, mid = 0.455 → spread_bps ≈ 220 (220 bps > 200 default → warning).
    assert 215 <= yes[4] <= 225
    assert yes[5] is True
    assert yes[6] == "rest"
    # Both outcomes flagged for the same reason.
    assert report.snapshots_thin_book == 2


def test_snapshot_prices_tight_spread_no_warning(store: DuckDBStore) -> None:
    """A market with a tight spread (well below threshold) gets no warning."""
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        # Tight spread: 0.499 / 0.501, mid 0.500, bps = 0.002/0.5 * 10_000 = 40.
        return httpx.Response(
            200,
            json=_full_book_payload(
                market="0xabc",
                asset_id=token,
                best_bid=0.499,
                best_ask=0.501,
            ),
        )

    with _build_clob_client(handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.snapshots_inserted == 2
    assert report.snapshots_thin_book == 0

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT spread_bps, liquidity_warning FROM polymarket_price_snapshots"
        ).fetchall()
    for row in rows:
        assert row[0] == 40
        assert row[1] is False


def test_snapshot_prices_thin_book_sets_liquidity_warning(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(
            200,
            json=_empty_side_payload(market="0xabc", asset_id=token, side_with_levels="bids"),
        )

    with _build_clob_client(handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.snapshots_inserted == 2
    assert report.snapshots_thin_book == 2

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT mid_price, best_bid, best_ask, liquidity_warning "
            "FROM polymarket_price_snapshots"
        ).fetchall()

    for row in rows:
        assert row[0] is None  # mid_price NULL since one side missing
        assert row[2] is None  # ask side missing
        assert row[3] is True  # liquidity_warning


def test_snapshot_prices_handles_404_orderbook(store: DuckDBStore) -> None:
    """When a token's orderbook returns 404, the snapshot still records with NULLs."""
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such token"})

    with _build_clob_client(handler) as client:
        report = snapshot_prices(store, client=client)

    # Both tokens 404 → liquidity_warning, all NULLs.
    assert report.snapshots_inserted == 2
    assert report.snapshots_thin_book == 2

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT mid_price, best_bid, best_ask, liquidity_warning, source_payload_json "
            "FROM polymarket_price_snapshots"
        ).fetchall()
    for row in rows:
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None
        assert row[3] is True
        # Synthetic payload marker for orderbook-unavailable.
        payload = json.loads(row[4])
        assert payload.get("orderbook_unavailable") is True


def test_snapshot_prices_skips_multi_outcome(store: DuckDBStore) -> None:
    payloads = [
        _market_payload(
            "0xmulti",
            token_yes="t1",
            token_no="t2",
            outcomes=("A", "B"),
        ),
    ]
    # Override outcomes to 3-way to make it multi.
    payloads[0]["outcomes"] = ["A", "B", "C"]
    payloads[0]["clobTokenIds"] = ["t1", "t2", "t3"]
    handler = _gamma_handler(active_payloads=payloads)
    with _build_gamma_client(handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 14, tzinfo=UTC))

    def clob_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_full_book_payload(market="0xmulti", asset_id="t1"))

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.markets_evaluated == 1
    assert report.markets_skipped_non_binary == 1
    assert report.snapshots_inserted == 0


def test_snapshot_prices_filter_restricts_to_named_markets(store: DuckDBStore) -> None:
    """market_filter limits the iteration to the named condition_ids."""
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")
    # Seed a second market that should be excluded by the filter.
    payloads = [
        _market_payload("0xabc", token_yes="0xabc-yes", token_no="0xabc-no"),
        _market_payload("0xother", token_yes="0xother-yes", token_no="0xother-no"),
    ]
    handler = _gamma_handler(active_payloads=payloads)
    with _build_gamma_client(handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 15, tzinfo=UTC))

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        # Use the token id as both market and asset for simplicity in mock
        return httpx.Response(200, json=_full_book_payload(market="0xany", asset_id=token))

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(
            store,
            client=client,
            market_filter=["0xabc"],
        )

    assert report.markets_evaluated == 1
    assert report.snapshots_inserted == 2

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM polymarket_price_snapshots"
        ).fetchall()
    assert {r[0] for r in rows} == {"0xabc"}


def test_snapshot_prices_records_last_successful_fetch(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(200, json=_full_book_payload(market="0xabc", asset_id=token))

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_clob_client(clob_handler) as client:
        snapshot_prices(store, client=client, now=when)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_successful_fetch FROM sources WHERE source_id = 'polymarket'"
        ).fetchone()
    assert row is not None
    assert row[0] == when


def test_snapshot_prices_per_market_failure_does_not_stop_others(
    store: DuckDBStore,
) -> None:
    """One market's network error is captured; other markets still snapshot."""
    payloads = [
        _market_payload("0xok", token_yes="ok-yes", token_no="ok-no"),
        _market_payload("0xbad", token_yes="bad-yes", token_no="bad-no"),
    ]
    handler = _gamma_handler(active_payloads=payloads)
    with _build_gamma_client(handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 14, tzinfo=UTC))

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        if token.startswith("bad"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=_full_book_payload(market="0xany", asset_id=token))

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.markets_evaluated == 2
    assert len(report.market_errors) == 1
    failed_id, _ = report.market_errors[0]
    assert failed_id == "0xbad"
    # 0xok still snapshotted — 2 outcomes.
    assert report.snapshots_inserted == 2


def test_snapshot_prices_idempotent_on_same_timestamp(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(200, json=_full_book_payload(market="0xabc", asset_id=token))

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_clob_client(clob_handler) as client:
        first = snapshot_prices(store, client=client, now=when)
        second = snapshot_prices(store, client=client, now=when)

    assert first.snapshots_inserted == 2
    assert second.snapshots_inserted == 0
    assert second.snapshots_unchanged == 2
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_price_snapshots").fetchone()
    assert row is not None
    assert row[0] == 2  # no duplicates


def test_snapshot_prices_two_separate_timestamps_creates_two_snapshots_per_token(
    store: DuckDBStore,
) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(200, json=_full_book_payload(market="0xabc", asset_id=token))

    t1 = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    with _build_clob_client(clob_handler) as client:
        snapshot_prices(store, client=client, now=t1)
        snapshot_prices(store, client=client, now=t2)

    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_price_snapshots").fetchone()
    assert row is not None
    assert row[0] == 4  # 2 tokens * 2 timestamps


def test_snapshot_prices_skips_removed_markets(store: DuckDBStore) -> None:
    """A market with removed_at set is not snapshotted."""
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")
    # Trigger removal: empty next sync.
    with _build_gamma_client(_gamma_handler(active_payloads=[])) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 15, tzinfo=UTC))

    def clob_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_full_book_payload(market="0xany", asset_id="x"))

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.markets_evaluated == 0


def test_snapshot_prices_runs_against_empty_state(store: DuckDBStore) -> None:
    """Empty polymarket_markets → no snapshots, no errors."""

    def clob_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.markets_evaluated == 0
    assert report.snapshots_inserted == 0
    assert not report.errors


def test_wide_spread_above_threshold_sets_warning(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        # Wide spread: bid 0.40, ask 0.60, mid 0.50, bps = 4000.
        token = request.url.params.get("token_id", "")
        return httpx.Response(
            200,
            json=_full_book_payload(
                market="0xabc",
                asset_id=token,
                best_bid=0.40,
                best_ask=0.60,
            ),
        )

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client)

    assert report.snapshots_inserted == 2
    assert report.snapshots_thin_book == 2

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT spread_bps, liquidity_warning FROM polymarket_price_snapshots"
        ).fetchall()
    for row in rows:
        assert row[0] == 4000
        assert row[1] is True


def test_snapshot_prices_includes_timing(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(200, json=_full_book_payload(market="0xabc", asset_id=token))

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client, now=when)

    assert report.started_at == when
    assert report.completed_at is not None
    assert report.duration_seconds is not None
    assert report.duration_seconds >= 0


def test_market_filter_with_nonexistent_id_yields_no_work(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client, market_filter=["0xnotpresent"])

    assert report.markets_evaluated == 0
    assert report.snapshots_inserted == 0


def test_market_filter_empty_set_is_noop(store: DuckDBStore) -> None:
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        # Should never be hit.
        return httpx.Response(500, text="should not be called")

    with _build_clob_client(clob_handler) as client:
        report = snapshot_prices(store, client=client, market_filter=[])

    assert report.markets_evaluated == 0
    assert report.snapshots_inserted == 0


def test_source_record_id_makes_each_snapshot_unique(store: DuckDBStore) -> None:
    """Two snapshots at different timestamps have distinct source_record_ids."""
    _seed_one_binary_market(store, "0xabc", token_yes="0xabc-yes", token_no="0xabc-no")

    def clob_handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("token_id", "")
        return httpx.Response(200, json=_full_book_payload(market="0xabc", asset_id=token))

    t1 = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    with _build_clob_client(clob_handler) as client:
        snapshot_prices(store, client=client, now=t1)
        snapshot_prices(store, client=client, now=t2)

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT source_record_id FROM polymarket_price_snapshots"
        ).fetchall()
    assert len(rows) == 4
