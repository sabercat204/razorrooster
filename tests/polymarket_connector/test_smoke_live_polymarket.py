"""T-PMC-071 — smoke tests against live Polymarket APIs.

These tests are gated behind the ``smoke`` pytest marker (``make smoke``).
They are operator-initiated, exercise real network calls, and skip
cleanly when:

- The geo gate refuses (e.g. CI runner in a restricted region).
- The ToS gate refuses (no acknowledgement on the smoke store).
- A transport error (DNS, connection refused, TLS) prevents the call.

The smoke harness writes to a separate ``data/trough_smoke.duckdb`` so
the operator's production store is untouched, matching the data_ingest
smoke convention.

What smoke does NOT do:

- It does not exhaustively test connector behavior. The unit tests
  cover that.
- It does not validate data quality.
- It does not record acknowledgements: the smoke run uses the
  pre-existing ack on the smoke store (or skips when absent).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.clob_public import (
    ClobPublicClient,
)
from razor_rooster.polymarket_connector.client.gamma import GammaClient
from razor_rooster.polymarket_connector.client.rate_limit import (
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.gates.geo import (
    StartupRefusal,
    check_jurisdiction,
)
from razor_rooster.polymarket_connector.gates.tos import (
    ToSGateError,
    check_tos_acknowledged,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)

pytestmark = pytest.mark.smoke


_SMOKE_DB_PATH = Path("data") / "trough_smoke.duckdb"


@pytest.fixture(scope="module")
def smoke_store() -> Iterator[DuckDBStore]:
    """Open the shared smoke DuckDB; create + apply migrations if absent."""
    _SMOKE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    store = DuckDBStore(_SMOKE_DB_PATH)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


def _check_gates_or_skip(store: DuckDBStore) -> None:
    """Run both gates; skip the test when either refuses."""
    try:
        check_jurisdiction()
    except StartupRefusal as exc:
        pytest.skip(f"polymarket smoke skipped: geo gate refused ({exc})")

    try:
        with store.connection() as conn:
            check_tos_acknowledged(conn)
    except ToSGateError as exc:
        pytest.skip(f"polymarket smoke skipped: ToS gate refused ({exc})")


def test_smoke_database_uses_separate_path() -> None:
    """The T-PMC-071 contract: smoke runs do not touch the production DuckDB."""
    smoke_path = _SMOKE_DB_PATH
    production_paths = [
        Path("data") / "trough.duckdb",
        Path.home() / "Projects" / "razor-rooster" / "data" / "trough.duckdb",
    ]
    for prod in production_paths:
        assert smoke_path.resolve() != prod.resolve(), (
            f"smoke path collides with production path: {prod}"
        )


def test_smoke_gamma_list_markets(smoke_store: DuckDBStore) -> None:
    """A small page of active markets returns from the live Gamma API."""
    _check_gates_or_skip(smoke_store)
    try:
        with GammaClient() as client:
            markets = client.list_markets(limit=5)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
        pytest.skip(f"polymarket smoke skipped: transport error ({exc})")

    assert isinstance(markets, list)
    # Polymarket nearly always has thousands of active markets; an empty
    # result here would be unusual but isn't a smoke failure.
    for market in markets:
        assert market.condition_id


def test_smoke_clob_orderbook_for_first_market(smoke_store: DuckDBStore) -> None:
    """Fetch the orderbook for the first outcome token of the first active market."""
    _check_gates_or_skip(smoke_store)

    try:
        with GammaClient() as gamma_client:
            markets = gamma_client.list_markets(limit=5)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
        pytest.skip(f"polymarket smoke skipped: gamma transport error ({exc})")

    if not markets:
        pytest.skip("polymarket smoke skipped: no active markets returned")

    target_token: str | None = None
    for market in markets:
        token_ids = market.raw.get("clobTokenIds")
        if isinstance(token_ids, str):
            import json

            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                token_ids = None
        if isinstance(token_ids, list) and token_ids:
            target_token = str(token_ids[0])
            break

    if target_token is None:
        pytest.skip("polymarket smoke skipped: no token id discoverable")

    try:
        with ClobPublicClient() as clob_client:
            orderbook = clob_client.get_orderbook(target_token)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
        pytest.skip(f"polymarket smoke skipped: clob transport error ({exc})")

    if orderbook is None:
        pytest.skip(f"polymarket smoke skipped: orderbook 404 for token {target_token}")

    assert orderbook.asset_id == target_token


def test_smoke_resolved_markets_first_page(smoke_store: DuckDBStore) -> None:
    """A small page of resolved markets returns from the live Gamma API."""
    _check_gates_or_skip(smoke_store)
    try:
        with GammaClient() as client:
            resolved = client.list_resolved(limit=5)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
        pytest.skip(f"polymarket smoke skipped: transport error ({exc})")

    assert isinstance(resolved, list)
