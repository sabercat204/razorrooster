"""Cap enforcement during backfill (T-035, REQ-BACKFILL-003).

Provides :func:`build_cap_check`, which constructs the callable that
:func:`run_backfill` consults before each batch commit. The check returns
``None`` when the source is free to keep going, or a
:class:`CapCheckResult` with the pause reason when either:

- The global corpus has crossed the configured pause-backfill threshold
  (default 95% of 100 GB per design §5.3 / NFR-PERF-002).
- The per-source byte cap (from ``config/source_caps.yaml``) has been
  reached for the current source.

The byte estimate per source is approximate — DuckDB doesn't expose exact
per-row byte counts cheaply. We use ``per_source_row_counts`` (T-016)
multiplied by an average row-size estimate as the gate; for v1 this is
adequate because the per-source caps are coarse (gigabytes, not bytes).
The exact byte budget is enforced at the global-corpus level via
``Path.stat()``.

Concurrency: the cap-check callable is invoked from a single backfill
worker (T-034 runs one connector at a time). It reads from the store but
does not hold the connection across calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from razor_rooster.data_ingest.backfill import CapCheckResult
from razor_rooster.data_ingest.config.loader import (
    PerSourceCaps,
    SourceCapsConfig,
)
from razor_rooster.data_ingest.persistence.disk_budget import (
    DiskBudgetConfig,
    current_status,
    per_source_row_counts,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)


# Conservative average row size (bytes) used to estimate per-source disk
# usage from row counts. For v1 the gate is coarse — per-source caps are
# in gigabytes — so a uniform estimate is acceptable. T-072 (first
# real backfill) measures actual per-source row sizes and we revise.
DEFAULT_AVERAGE_ROW_BYTES: int = 1024


def estimate_source_bytes(
    store: DuckDBStore,
    source_id: str,
    *,
    average_row_bytes: int = DEFAULT_AVERAGE_ROW_BYTES,
) -> int:
    """Estimate on-disk bytes used by a single source.

    Computes ``row_count * average_row_bytes``. The estimate is approximate
    by design — see module docstring.
    """
    with store.connection() as conn:
        counts = per_source_row_counts(conn)
    rows = counts.get(source_id, 0)
    return rows * average_row_bytes


def build_cap_check(
    store: DuckDBStore,
    *,
    caps: SourceCapsConfig,
    file_path: Path | str | None = None,
    average_row_bytes: int = DEFAULT_AVERAGE_ROW_BYTES,
) -> Callable[[str], CapCheckResult | None]:
    """Construct the cap-check callable used by :func:`run_backfill`.

    ``caps`` is the loaded ``source_caps.yaml`` model from T-022.
    ``file_path`` is the DuckDB store's on-disk path; it's used by the
    global cap measurement (the global cap is enforced via real file size
    rather than row-count approximation).
    """
    global_cfg = DiskBudgetConfig(
        global_cap_bytes=caps.global_caps.max_corpus_bytes,
        warn_at_pct=caps.global_caps.warn_at_pct,
        pause_backfill_at_pct=caps.global_caps.pause_backfill_at_pct,
    )

    def check(source_id: str) -> CapCheckResult | None:
        # Global-corpus cap takes precedence — if the whole store is
        # near-full, no source's backfill should make it fuller.
        with store.connection() as conn:
            status = current_status(conn, file_path=file_path, config=global_cfg)
        if status.should_pause_backfill:
            return CapCheckResult(
                status="GLOBAL_CAP_REACHED",
                reason=(
                    f"global corpus at {status.pct_of_cap:.1f}% of cap "
                    f"({status.bytes_used:,} / {status.cap_bytes:,} bytes); "
                    "backfill paused"
                ),
            )
        if status.should_warn:
            logger.warning(
                "global corpus at %.1f%% of cap; backfill continuing under warning",
                status.pct_of_cap,
            )

        # Per-source byte cap.
        per_source = caps.per_source.get(source_id)
        if per_source is not None and per_source.max_bytes is not None:
            estimated = estimate_source_bytes(store, source_id, average_row_bytes=average_row_bytes)
            if estimated >= per_source.max_bytes:
                return CapCheckResult(
                    status="CAP_REACHED",
                    reason=(
                        f"source {source_id!r} estimated at "
                        f"{estimated:,} bytes; per-source cap is "
                        f"{per_source.max_bytes:,} bytes"
                    ),
                )
        return None

    return check


def per_source_cap_for(caps: SourceCapsConfig, source_id: str) -> PerSourceCaps | None:
    """Convenience accessor: return the per-source caps row, or ``None``."""
    return caps.per_source.get(source_id)
