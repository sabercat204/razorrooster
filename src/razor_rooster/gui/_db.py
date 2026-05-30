"""Shared DuckDB-store dependency for GUI routes.

Each request opens its own DuckDBStore and closes it when the
request finishes. This avoids any thread/connection-pool concerns
when uvicorn dispatches requests across worker threads.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore


@contextmanager
def open_store(db_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a DuckDB connection for the duration of a request.

    The store is closed even if the route handler raises.
    """
    store = DuckDBStore(db_path)
    try:
        with store.connection() as conn:
            yield conn
    finally:
        store.close()


__all__ = ["open_store"]
