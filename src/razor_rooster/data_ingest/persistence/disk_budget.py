"""Disk budget tracker for ``data_ingest`` (T-016).

Tracks the DuckDB store's on-disk size against a configurable global cap
(default 100 GB per NFR-PERF-002) and exposes typed thresholds:

- ``warn_at_pct`` (default 80%) — log a warning.
- ``pause_backfill_at_pct`` (default 95%) — backfill should pause; new
  incremental fetches still run.

Per-source byte estimation uses row-count multiplied by an approximate per-row size
from ``pragma_storage_info``. This is an estimate, not an exact measure.
DuckDB doesn't expose a precise per-row byte count, and we don't need
exact accuracy for budget enforcement; we need "is this source taking up
roughly its share of the corpus."

This module reads configuration but does not enforce action. The cycle
orchestrator (T-035) consumes the typed thresholds and decides whether
to pause backfill or alert. This separation keeps the budget code
testable in isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

logger = logging.getLogger(__name__)


# Defaults from design §5.3 / NFR-PERF-002.
DEFAULT_GLOBAL_CAP_BYTES: Final[int] = 100 * 1024 * 1024 * 1024  # 100 GB
DEFAULT_WARN_PCT: Final[float] = 80.0
DEFAULT_PAUSE_BACKFILL_PCT: Final[float] = 95.0


@dataclass(frozen=True, slots=True)
class DiskBudgetConfig:
    """Disk budget thresholds, mirroring ``config/source_caps.yaml`` global section."""

    global_cap_bytes: int = DEFAULT_GLOBAL_CAP_BYTES
    warn_at_pct: float = DEFAULT_WARN_PCT
    pause_backfill_at_pct: float = DEFAULT_PAUSE_BACKFILL_PCT

    def __post_init__(self) -> None:
        if self.global_cap_bytes < 1:
            raise ValueError("global_cap_bytes must be >= 1")
        if not 0.0 < self.warn_at_pct <= 100.0:
            raise ValueError("warn_at_pct must be in (0, 100]")
        if not 0.0 < self.pause_backfill_at_pct <= 100.0:
            raise ValueError("pause_backfill_at_pct must be in (0, 100]")
        if self.warn_at_pct >= self.pause_backfill_at_pct:
            raise ValueError("warn_at_pct must be strictly less than pause_backfill_at_pct")


@dataclass(frozen=True, slots=True)
class DiskBudgetStatus:
    """Current usage relative to the configured cap.

    Attributes:
        bytes_used: best estimate of DuckDB store on-disk size.
        cap_bytes: configured global cap.
        pct_of_cap: ``bytes_used / cap_bytes * 100``.
        should_warn: ``True`` when ``pct_of_cap`` >= warn threshold.
        should_pause_backfill: ``True`` when ``pct_of_cap`` >= pause threshold.
    """

    bytes_used: int
    cap_bytes: int
    pct_of_cap: float
    should_warn: bool
    should_pause_backfill: bool


def _file_size_bytes(path: Path | str) -> int:
    """Return the size of an on-disk DuckDB file, or 0 for in-memory."""
    if path == ":memory:":
        return 0
    p = Path(path) if not isinstance(path, Path) else path
    if not p.exists():
        return 0
    # DuckDB writes a WAL file alongside the database; both count toward
    # actual disk usage. ``-wal`` and ``-shm`` are SQLite conventions, but
    # DuckDB uses ``.wal`` next to the file.
    total = p.stat().st_size
    wal = p.with_suffix(p.suffix + ".wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def database_size_bytes(
    conn: duckdb.DuckDBPyConnection,
    *,
    file_path: Path | str | None = None,
) -> int:
    """Best-effort total bytes used by the DuckDB store.

    Prefers on-disk ``stat()`` when ``file_path`` is provided and not
    ``":memory:"``; otherwise falls back to ``PRAGMA database_size``.
    """
    if file_path is not None and file_path != ":memory:":
        return _file_size_bytes(file_path)

    rows = conn.execute("PRAGMA database_size").fetchall()
    if not rows:
        return 0
    # Row format (DuckDB 1.5):
    #   (database_name, database_size, block_size, total_blocks,
    #    used_blocks, free_blocks, wal_size, memory_usage, memory_limit)
    # database_size is a human-readable string; used_blocks * block_size is
    # the canonical measure.
    row = rows[0]
    block_size = int(row[2]) if row[2] is not None else 0
    used_blocks = int(row[4]) if row[4] is not None else 0
    return block_size * used_blocks


def current_status(
    conn: duckdb.DuckDBPyConnection,
    *,
    file_path: Path | str | None = None,
    config: DiskBudgetConfig | None = None,
) -> DiskBudgetStatus:
    """Compute the current disk-budget status."""
    cfg = config or DiskBudgetConfig()
    used = database_size_bytes(conn, file_path=file_path)
    pct = (used / cfg.global_cap_bytes) * 100.0 if cfg.global_cap_bytes > 0 else 0.0
    return DiskBudgetStatus(
        bytes_used=used,
        cap_bytes=cfg.global_cap_bytes,
        pct_of_cap=pct,
        should_warn=pct >= cfg.warn_at_pct,
        should_pause_backfill=pct >= cfg.pause_backfill_at_pct,
    )


def per_source_row_counts(
    conn: duckdb.DuckDBPyConnection,
    *,
    canonical_tables: tuple[str, ...] = (
        "event_stream",
        "time_series",
        "document_docket",
        "geospatial_indicator",
    ),
) -> dict[str, int]:
    """Return per-source row counts across the canonical tables.

    Used by per-source cap enforcement (REQ-BACKFILL-003). The byte-equivalent
    is approximated downstream by row counts; we don't try to measure exact
    bytes per source because DuckDB doesn't expose that cleanly.
    """
    totals: dict[str, int] = {}
    for table in canonical_tables:
        try:
            rows = conn.execute(
                f"SELECT source_id, COUNT(*) FROM {table} GROUP BY source_id"
            ).fetchall()
        except duckdb.CatalogException:
            # Table doesn't exist yet; skip.
            continue
        for source_id, count in rows:
            totals[str(source_id)] = totals.get(str(source_id), 0) + int(count)
    return totals
