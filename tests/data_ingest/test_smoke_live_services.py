"""T-071 — smoke tests against live services.

These tests are gated behind the ``smoke`` pytest marker (``make smoke``).
They are operator-initiated, exercise real network calls, and skip cleanly
when credentials for an authenticated source are absent.

Each smoke test:

- Spins up an isolated DuckDB at ``data/trough_smoke.duckdb`` (the
  T-071 deliverable: smoke must not write to the production store).
- Runs one connector's incremental fetch with a tight time bound.
- Asserts the connector either persisted at least one record or
  returned cleanly with zero records (not all sources publish in any
  given window — empty-but-clean is success).
- Tolerates transient network failures by treating timeout/transport
  errors as ``pytest.skip`` rather than hard failures: smoke is a
  health probe, not a functional regression check. Per-connector unit
  tests (the 414 in the main suite) are the regression bar.

What smoke does NOT do:

- It does not exhaustively test connector behavior. The 414 unit tests
  do that with recorded fixtures.
- It does not validate data quality. That is downstream subsystems'
  job (pattern_library validators).
- It does not run against authenticated sources unless the
  corresponding environment variables are present.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    ConnectorOutcome,
    CredentialMissingError,
    run_incremental,
)
from razor_rooster.data_ingest.credentials import load_credentials_for
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import register_source
from razor_rooster.data_ingest.scheduler import build_persister

if TYPE_CHECKING:
    from razor_rooster.data_ingest.connectors.base import Connector


pytestmark = pytest.mark.smoke


# Authenticated sources skip cleanly when credentials are absent.
_AUTHENTICATED_SOURCES = frozenset(
    {
        "acled",
        "eia",
        "noaa",
        "nrc_adams",
        "regulations_gov",
        "fred",
    }
)


# Source ids the smoke harness exercises — declared as data, not by
# importing connector modules at collection time. Connector modules are
# imported lazily inside each test so their ``@register`` decorators do
# not pollute the registry for other test files in a full ``pytest`` run.
_SMOKE_SOURCE_IDS: tuple[str, ...] = (
    "acled",
    "eia",
    "federal_register",
    "fred",
    "gdelt_events",
    "noaa",
    "nrc_adams",
    "regulations_gov",
    "usgs_minerals",
    "who_don",
    "worldbank",
)


def _import_and_get_connector_class(source_id: str) -> type[Connector]:
    """Import the connector module for ``source_id`` and return its class.

    Done lazily so the import (and its ``@register`` side-effect) only
    fires when the smoke test for that source is actually invoked.
    """
    from razor_rooster.data_ingest.registry import get as registry_get

    if source_id == "acled":
        from razor_rooster.data_ingest.connectors import acled  # noqa: F401
    elif source_id == "eia":
        from razor_rooster.data_ingest.connectors import eia  # noqa: F401
    elif source_id == "federal_register":
        from razor_rooster.data_ingest.connectors import federal_register  # noqa: F401
    elif source_id == "fred":
        from razor_rooster.data_ingest.connectors import fred  # noqa: F401
    elif source_id == "gdelt_events":
        from razor_rooster.data_ingest.connectors import gdelt_events  # noqa: F401
    elif source_id == "noaa":
        from razor_rooster.data_ingest.connectors import noaa  # noqa: F401
    elif source_id == "nrc_adams":
        from razor_rooster.data_ingest.connectors import nrc_adams  # noqa: F401
    elif source_id == "regulations_gov":
        from razor_rooster.data_ingest.connectors import regulations_gov  # noqa: F401
    elif source_id == "usgs_minerals":
        from razor_rooster.data_ingest.connectors import usgs_minerals  # noqa: F401
    elif source_id == "who_don":
        from razor_rooster.data_ingest.connectors import who_don  # noqa: F401
    elif source_id == "worldbank":
        from razor_rooster.data_ingest.connectors import worldbank  # noqa: F401
    else:
        raise ValueError(f"unknown source_id {source_id!r}")
    return registry_get(source_id)


@pytest.fixture(scope="module")
def smoke_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[DuckDBStore]:
    """A separate smoke DuckDB at the standard relative path for smoke runs.

    The file lives under the workspace's ``data/`` directory, not the
    operator's production store. ``data/trough_smoke.duckdb`` is the
    T-071 contract.
    """
    smoke_dir = Path("data")
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_path = smoke_dir / "trough_smoke.duckdb"
    if smoke_path.exists():
        smoke_path.unlink()
    wal = smoke_path.with_suffix(smoke_path.suffix + ".wal")
    if wal.exists():
        wal.unlink()
    store = DuckDBStore(smoke_path)
    with store.connection() as conn:
        run_pending_migrations(conn)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("source_id", _SMOKE_SOURCE_IDS)
def test_connector_smoke_fetch(
    source_id: str,
    smoke_store: DuckDBStore,
) -> None:
    """One-record incremental fetch against the live service.

    Pass conditions:
    - Connector instantiates with whatever credentials are present.
    - ``run_incremental`` returns a typed ConnectorOutcome.
    - Outcome is either ``ok`` (>=0 records persisted) or ``partial``
      (some records, some recoverable errors).

    Skip conditions:
    - Authenticated source with no credentials in environment.
    - Network transport error (DNS, connection refused, TLS handshake
      failure, server 5xx storm). These are operator-environment
      problems, not regressions.

    Hard failure conditions:
    - Connector raises an unhandled exception that ``run_incremental``
      could not classify (would mean a contract bug).
    - Outcome status is ``failed`` *and* the error is not a recognized
      transient class.
    """
    credentials = load_credentials_for(source_id)

    if source_id in _AUTHENTICATED_SOURCES and credentials is None:
        pytest.skip(f"smoke skipped: {source_id} requires credentials not in env")

    connector_class = _import_and_get_connector_class(source_id)

    with smoke_store.connection() as conn:
        register_source(
            conn,
            source_id=source_id,
            source_type=connector_class.canonical_schema.value,
            cadence="daily",
            freshness_threshold_seconds=172_800,
            license=connector_class.license.value,
        )

    try:
        connector = connector_class(smoke_store, credentials=credentials)
    except CredentialMissingError as exc:
        pytest.skip(f"smoke skipped: {source_id} credentials incomplete ({exc})")

    persister = build_persister(connector, smoke_store, batch_size=10)

    from datetime import UTC, datetime

    since = datetime.now(tz=UTC).replace(year=datetime.now(tz=UTC).year - 1)

    try:
        outcome: ConnectorOutcome = run_incremental(connector, since=since, persister=persister)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
        pytest.skip(f"smoke skipped: {source_id} transport error ({exc})")

    assert isinstance(outcome, ConnectorOutcome)
    assert outcome.source_id == source_id

    if outcome.status == "failed":
        error_messages = " | ".join(str(e.get("message", "")) for e in outcome.errors)
        if any(
            indicator in error_messages.lower()
            for indicator in (
                "timeout",
                "connect",
                "ssl",
                "network",
                "503",
                "502",
                "504",
            )
        ):
            pytest.skip(f"smoke skipped: {source_id} transient failure ({error_messages})")
        pytest.fail(f"smoke failed for {source_id}: {error_messages}")

    assert outcome.status in ("ok", "partial", "skipped"), (
        f"unexpected status {outcome.status!r} for {source_id}"
    )


def test_smoke_database_uses_separate_path() -> None:
    """The T-071 contract: smoke runs do not write to the production DuckDB."""
    smoke_path = Path("data") / "trough_smoke.duckdb"
    production_paths = [
        Path("data") / "trough.duckdb",
        Path.home() / "Projects" / "razor-rooster" / "data" / "trough.duckdb",
    ]
    for prod_path in production_paths:
        assert smoke_path.resolve() != prod_path.resolve(), (
            f"smoke path collides with production path: {prod_path}"
        )
