"""T-012 verification — DuckDB store wrapper.

Verifies:
- Connections can be acquired and released via the context manager.
- Multiple parallel acquisitions work up to ``max_connections``.
- Concurrent writes from multiple threads do not corrupt the store.
- Read-only mode rejects writes.
- ``close()`` is idempotent and prevents further acquisitions.
- Pool exhaustion raises a typed timeout error.
- Default on-disk path is created with ``ensure_parent_dir=True``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import (
    ConnectionAcquisitionTimeout,
    DuckDBStore,
    DuckDBStoreError,
)


def test_basic_acquisition_releases_to_pool() -> None:
    store = DuckDBStore(":memory:", max_connections=2)
    with store.connection() as conn:
        rows = conn.execute("SELECT 42").fetchall()
        assert rows == [(42,)]
    # Connection should be released; second acquisition reuses or creates.
    with store.connection() as conn:
        assert conn.execute("SELECT 7").fetchall() == [(7,)]
    store.close()


def test_in_memory_store_round_trips() -> None:
    store = DuckDBStore(":memory:")
    with store.connection() as conn:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (?), (?), (?)", [1, 2, 3])
    # Note: separate connections to ":memory:" do not share state in DuckDB.
    # Within the same store, the pool reuses the same set of connections so
    # the table is visible.
    with store.connection() as conn:
        rows = conn.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert rows == [(1,), (2,), (3,)]
    store.close()


def test_close_is_idempotent() -> None:
    store = DuckDBStore(":memory:")
    with store.connection() as conn:
        conn.execute("SELECT 1")
    store.close()
    store.close()  # second close is a no-op


def test_acquisition_after_close_raises() -> None:
    store = DuckDBStore(":memory:")
    store.close()
    with pytest.raises(DuckDBStoreError), store.connection():
        pass


def test_max_connections_must_be_positive() -> None:
    with pytest.raises(ValueError):
        DuckDBStore(":memory:", max_connections=0)


def test_pool_caps_at_max_connections(tmp_path: Path) -> None:
    """When ``max_connections`` is reached, additional acquirers wait for release."""
    db_path = tmp_path / "trough.duckdb"
    store = DuckDBStore(db_path, max_connections=2)

    # Hold both connections.
    cm1 = store.connection(timeout=5.0)
    cm2 = store.connection(timeout=5.0)
    cm1.__enter__()
    cm2.__enter__()
    try:
        # Third acquisition should time out.
        with pytest.raises(ConnectionAcquisitionTimeout), store.connection(timeout=0.1):
            pass
    finally:
        cm1.__exit__(None, None, None)
        cm2.__exit__(None, None, None)
    store.close()


def test_concurrent_writes_dont_corrupt_store(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.duckdb"
    store = DuckDBStore(db_path, max_connections=4)
    with store.connection() as conn:
        conn.execute("CREATE TABLE counts (worker INTEGER, n INTEGER)")

    n_workers = 4
    n_writes = 25

    def worker(worker_id: int) -> None:
        for i in range(n_writes):
            with store.connection(timeout=10.0) as conn:
                conn.execute("INSERT INTO counts VALUES (?, ?)", [worker_id, i])

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with store.connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM counts").fetchone()
        assert total is not None
        assert total[0] == n_workers * n_writes
        per_worker = conn.execute(
            "SELECT worker, COUNT(*) FROM counts GROUP BY worker ORDER BY worker"
        ).fetchall()
        assert per_worker == [(w, n_writes) for w in range(n_workers)]
    store.close()


def test_read_only_mode_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "ro.duckdb"
    # First populate.
    with DuckDBStore(db_path) as writer, writer.connection() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")

    # Then open read-only.
    reader = DuckDBStore(db_path, read_only=True)
    with reader.connection() as conn:
        rows = conn.execute("SELECT x FROM t").fetchall()
        assert rows == [(1,)]
        with pytest.raises(Exception):  # noqa: B017 - DuckDB raises a custom error class
            conn.execute("INSERT INTO t VALUES (2)")
    reader.close()


def test_default_path_ensures_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "trough.duckdb"
    assert not nested.parent.exists()
    store = DuckDBStore(nested, ensure_parent_dir=True)
    with store.connection() as conn:
        conn.execute("SELECT 1")
    assert nested.parent.is_dir()
    store.close()


def test_path_property_round_trips() -> None:
    in_mem = DuckDBStore(":memory:")
    assert in_mem.path == ":memory:"
    in_mem.close()


def test_context_manager_closes_on_exit() -> None:
    with DuckDBStore(":memory:") as store, store.connection() as conn:
        conn.execute("SELECT 1")
    # After exit, store is closed; further acquisitions fail.
    with pytest.raises(DuckDBStoreError), store.connection():
        pass


def test_release_on_exception_returns_connection_to_pool(tmp_path: Path) -> None:
    """If a caller's ``with store.connection()`` block raises, the connection still releases."""
    db_path = tmp_path / "exception.duckdb"
    store = DuckDBStore(db_path, max_connections=1)
    with pytest.raises(RuntimeError), store.connection() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        raise RuntimeError("simulated failure")
    # The single connection should be back in the pool.
    with store.connection(timeout=1.0) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchall()
        assert rows == [(0,)]
    store.close()


def test_serialized_writes_under_load(tmp_path: Path) -> None:
    """A regression test confirming the pool serializes a high-throughput burst.

    This is a smoke test: not a benchmark. It only confirms no deadlock and
    no record loss, which is the contract design §11 documents.
    """
    db_path = tmp_path / "burst.duckdb"
    store = DuckDBStore(db_path, max_connections=4)
    with store.connection() as conn:
        conn.execute("CREATE TABLE bursts (i INTEGER)")

    n_workers = 4
    n_inserts = 200
    start = time.monotonic()

    def worker() -> None:
        for i in range(n_inserts):
            with store.connection(timeout=30.0) as conn:
                conn.execute("INSERT INTO bursts VALUES (?)", [i])

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.monotonic() - start
    assert elapsed < 30.0  # generous bound; surface any pathological deadlock

    with store.connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM bursts").fetchone()
        assert total is not None
        assert total[0] == n_workers * n_inserts
    store.close()
