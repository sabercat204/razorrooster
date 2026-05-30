"""DuckDB store wrapper (T-012).

DuckDB allows only a single read-write process per file; within a process,
multiple connections to the same in-memory or on-disk database share state.
This wrapper provides:

- A typed entry point for all persistence operations.
- A configurable path (default ``~/Projects/razor-rooster/data/trough.duckdb``).
- Bounded-concurrency access via a connection pool with a fixed cap, so the
  cycle orchestrator's worker threads cannot exhaust DuckDB's connection
  budget or deadlock on contention.
- Context-manager support so connections close cleanly on exception.
- A read-only mode for downstream subsystems that should not mutate the store.

Design references:
- specs/DATA_INGEST_DESIGN.md §3.1 (module layout, ``DuckDBStore``).
- specs/DATA_INGEST_DESIGN.md §11 (single-connection serialization ceiling).
- REQ-PERSIST-001.

This module is intentionally kept small. It does not run migrations (T-013) or
implement the staging-merge upsert pattern (T-014). Those layers compose
``DuckDBStore``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, LifoQueue
from typing import Final

import duckdb

logger = logging.getLogger(__name__)


DEFAULT_STORE_PATH: Final[Path] = (
    Path.home() / "Projects" / "razor-rooster" / "data" / "trough.duckdb"
)
"""Default on-disk location for the ``data_ingest`` DuckDB store."""


DEFAULT_MAX_CONNECTIONS: Final[int] = 4
"""Default upper bound on simultaneous connections handed out by the pool.

Matches the ``max_workers`` default for the cycle orchestrator (design §3.3).
"""


class DuckDBStoreError(RuntimeError):
    """Base class for ``DuckDBStore`` failures."""


class ConnectionAcquisitionTimeout(DuckDBStoreError):
    """Raised when a worker waits too long for a connection slot."""


class DuckDBStore:
    """Thread-safe wrapper around a single DuckDB database file.

    Connections are pooled: callers acquire one via :meth:`connection` (a
    context manager), use it, and release it back to the pool on exit. The
    pool size caps total simultaneous connections; additional callers block
    with an optional timeout.

    DuckDB allows multiple connections within a process to share state on
    the same file, so the pool is purely an arbitration mechanism rather
    than a unique-handle gate.

    The store can run in read-only mode for downstream subsystems that
    should not mutate ``data_ingest``-owned tables.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        read_only: bool = False,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        ensure_parent_dir: bool = True,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be >= 1")

        if path is None:
            self._path: Path | str = DEFAULT_STORE_PATH
        elif path == ":memory:":
            self._path = ":memory:"
        else:
            self._path = Path(path)

        self._read_only = read_only
        self._max_connections = max_connections
        self._closed = False

        if ensure_parent_dir and isinstance(self._path, Path) and not read_only:
            self._path.parent.mkdir(parents=True, exist_ok=True)

        # The pool is a stack of available connections. We pre-create the
        # full set lazily on first acquisition so that initial open is cheap
        # and the typical case of "single-threaded operator usage" only
        # incurs one connection's worth of overhead.
        self._pool: LifoQueue[duckdb.DuckDBPyConnection] = LifoQueue(maxsize=max_connections)
        self._lock = threading.Lock()
        self._created_count = 0

    @property
    def path(self) -> Path | str:
        return self._path

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def max_connections(self) -> int:
        return self._max_connections

    def _create_connection(self) -> duckdb.DuckDBPyConnection:
        path_str = str(self._path) if isinstance(self._path, Path) else self._path
        conn = duckdb.connect(database=path_str, read_only=self._read_only)
        return conn

    @contextmanager
    def connection(self, timeout: float | None = 30.0) -> Iterator[duckdb.DuckDBPyConnection]:
        """Acquire a pooled connection.

        Returns a context manager that yields a :class:`duckdb.DuckDBPyConnection`
        and returns it to the pool on exit. Exceptions inside the ``with`` block
        are propagated; the connection is still returned to the pool unless
        it has already been closed externally.
        """
        if self._closed:
            raise DuckDBStoreError("DuckDBStore is closed")

        conn = self._acquire(timeout=timeout)
        try:
            yield conn
        finally:
            self._release(conn)

    def _acquire(self, timeout: float | None) -> duckdb.DuckDBPyConnection:
        # Fast path: a connection is already in the pool.
        try:
            return self._pool.get_nowait()
        except Empty:
            pass

        # Slow path: try to create a new connection up to the cap, otherwise
        # wait for one to be released.
        with self._lock:
            if self._created_count < self._max_connections:
                conn = self._create_connection()
                self._created_count += 1
                return conn

        try:
            return self._pool.get(timeout=timeout)
        except Empty as exc:
            raise ConnectionAcquisitionTimeout(
                f"No DuckDB connection available within {timeout}s "
                f"(max_connections={self._max_connections})"
            ) from exc

    def _release(self, conn: duckdb.DuckDBPyConnection) -> None:
        if self._closed:
            try:
                conn.close()
            except Exception:
                logger.exception("Error closing DuckDB connection during store shutdown")
            return
        self._pool.put_nowait(conn)

    def close(self) -> None:
        """Close all pooled connections.

        After ``close()`` the store cannot acquire new connections.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    conn = self._pool.get_nowait()
                except Empty:
                    break
                try:
                    conn.close()
                except Exception:
                    logger.exception("Error closing DuckDB connection during store shutdown")

    def __enter__(self) -> DuckDBStore:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
