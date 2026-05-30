"""Staging-merge upsert pattern (T-014, OQ-005 resolution).

DuckDB's native upsert is slow when input is unsorted by key (issue #11275).
Single-row upserts in a loop are pathological at scale. The staging-merge
pattern works around both:

1. Stage: bulk-insert the batch into a ``_staging_<table>`` table via a single
   ``INSERT INTO _staging_<table> SELECT * FROM batch`` from a registered
   Arrow table.
2. Sort: order the staging rows by the dedup keys.
3. Classify and merge: for each staging row, look up the matching active
   target row and decide whether the row is an insert, a revision, or a
   no-op.

For canonical-schema tables, dedup is keyed on ``(source_id, source_record_id)``
where ``superseded_at IS NULL`` (the "active row" rule). Re-ingestion of an
identical payload is a no-op; re-ingestion of a different payload supersedes
the prior active row and inserts the new one.

REQ-PERSIST-003 (idempotent writes) and REQ-PERSIST-004 (source revision
semantics) are both implemented here.

Concurrency: callers are responsible for serializing concurrent merges into
the same target table. Each merge call uses a uniquely-named staging table
(suffixed with a UUID hex) so multiple parallel merges into the same target
do not collide on the staging table itself, but write conflicts at the
target are still possible and require external serialization (the cycle
scheduler in T-033 ensures one connector at a time writes a given table).

Why we hash payloads in Python rather than in SQL:
DuckDB has ``sha256()`` and ``encode()`` but the canonical-form serialization
of arbitrary JSON in DuckDB is not the same as Python's ``json.dumps`` with
sorted keys. We need a hash that reliably tells us "is this exactly the
same payload as last time," and Python-side hashing on a single canonical
serialization is the simplest way to keep the answer reproducible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb
import pyarrow as pa

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Per-merge counts.

    Attributes:
        inserted: rows that were newly added to the target (no prior matching key).
        revised: rows whose prior active row was superseded and a new row inserted.
        unchanged: rows whose payload was identical to the existing active row.
    """

    inserted: int
    revised: int
    unchanged: int

    @property
    def total(self) -> int:
        return self.inserted + self.revised + self.unchanged


def _payload_hash(payload: Any) -> str:
    """Stable hash of a JSON payload for change detection.

    Accepts either a Python dict/list or a pre-serialized JSON string.
    Returns the hex digest of the SHA-256 of the canonical (sorted-key)
    JSON serialization. ``default=str`` handles non-JSON-native types like
    ``datetime`` by stringifying them; this is fine because identical
    inputs produce identical outputs and that is the only contract we need.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def staging_merge(
    conn: duckdb.DuckDBPyConnection,
    target_table: str,
    batch: pa.Table,
    *,
    dedup_keys: tuple[str, ...] = ("source_id", "source_record_id"),
    payload_column: str = "source_payload_json",
    superseded_column: str = "superseded_at",
) -> MergeResult:
    """Idempotently merge an Arrow batch into a target table.

    The target table must follow the canonical-schema shape: it has the
    given dedup keys, a JSON payload column, and a nullable
    ``superseded_at`` column that marks rows replaced by a later revision.

    Returns the per-bucket counts.
    """
    if batch.num_rows == 0:
        return MergeResult(inserted=0, revised=0, unchanged=0)

    if payload_column not in batch.column_names:
        raise ValueError(
            f"batch is missing required payload column {payload_column!r}; "
            f"columns present: {batch.column_names}"
        )
    for key in dedup_keys:
        if key not in batch.column_names:
            raise ValueError(
                f"batch is missing required dedup key column {key!r}; "
                f"columns present: {batch.column_names}"
            )

    staging_table = f"_staging_{target_table}_{uuid.uuid4().hex[:12]}"
    now = datetime.now(tz=UTC)
    sort_cols = ", ".join(dedup_keys)

    # Pre-compute payload hashes per staging row.
    payload_col_idx = batch.column_names.index(payload_column)
    payload_col = batch.column(payload_col_idx).to_pylist()
    hashes = pa.array([_payload_hash(p) for p in payload_col], type=pa.string())
    augmented = batch.append_column("_payload_hash", hashes)

    conn.register("_merge_batch_arrow", augmented)
    try:
        conn.execute(f"CREATE TEMPORARY TABLE {staging_table} AS SELECT * FROM _merge_batch_arrow")
        # Sort the staging table by dedup keys for OQ-005's throughput
        # benefit during the subsequent inserts. We rebuild sorted in-place.
        conn.execute(
            f"CREATE TEMPORARY TABLE {staging_table}_sorted AS "
            f"SELECT * FROM {staging_table} ORDER BY {sort_cols}"
        )
        conn.execute(f"DROP TABLE {staging_table}")
        conn.execute(f"ALTER TABLE {staging_table}_sorted RENAME TO {staging_table}")

        # Build a per-key lookup of currently-active target rows so we can
        # classify each staging row.
        target_keys_to_hash: dict[tuple[Any, ...], str] = {}
        target_count_row = conn.execute(
            f"SELECT COUNT(*) FROM {target_table} WHERE {superseded_column} IS NULL"
        ).fetchone()
        if target_count_row is not None and target_count_row[0] > 0:
            target_rows = conn.execute(
                f"SELECT {sort_cols}, {payload_column} "
                f"FROM {target_table} WHERE {superseded_column} IS NULL"
            ).fetchall()
            for target_row in target_rows:
                target_key: tuple[Any, ...] = tuple(target_row[: len(dedup_keys)])
                target_payload = target_row[len(dedup_keys)]
                target_keys_to_hash[target_key] = _payload_hash(target_payload)

        # Classify staging rows.
        staging_rows = conn.execute(
            f"SELECT {sort_cols}, _payload_hash FROM {staging_table}"
        ).fetchall()
        inserts: list[tuple[Any, ...]] = []
        revisions: list[tuple[Any, ...]] = []
        unchanged: list[tuple[Any, ...]] = []
        for staging_row in staging_rows:
            staging_key: tuple[Any, ...] = tuple(staging_row[: len(dedup_keys)])
            staging_hash = staging_row[len(dedup_keys)]
            existing_hash = target_keys_to_hash.get(staging_key)
            if existing_hash is None:
                inserts.append(staging_key)
            elif existing_hash == staging_hash:
                unchanged.append(staging_key)
            else:
                revisions.append(staging_key)

        # Supersede prior active rows for revisions.
        if revisions:
            placeholders_per_key = " AND ".join(f"{k} = ?" for k in dedup_keys)
            for revision_key in revisions:
                conn.execute(
                    f"UPDATE {target_table} SET {superseded_column} = ? "
                    f"WHERE {placeholders_per_key} AND {superseded_column} IS NULL",
                    [now, *revision_key],
                )

        # Insert new and revised rows. Skip the synthetic _payload_hash column
        # so the insert matches the target's column shape.
        keys_to_insert: list[tuple[Any, ...]] = inserts + revisions
        if keys_to_insert:
            target_column_rows = conn.execute(f"DESCRIBE {target_table}").fetchall()
            target_column_names = [r[0] for r in target_column_rows]
            staging_select_cols = ", ".join(target_column_names)

            if len(dedup_keys) == 1:
                key_values = [k[0] for k in keys_to_insert]
                conn.execute(
                    f"INSERT INTO {target_table} ({staging_select_cols}) "
                    f"SELECT {staging_select_cols} FROM {staging_table} "
                    f"WHERE {dedup_keys[0]} IN (SELECT unnest(?))",
                    [key_values],
                )
            else:
                # Multi-column key: build a tuple-IN equivalent. For batch
                # sizes in practice (<=10k) this stays well below DuckDB's
                # query-text limits.
                conditions: list[str] = []
                params: list[Any] = []
                for insert_key in keys_to_insert:
                    conditions.append("(" + " AND ".join(f"{k} = ?" for k in dedup_keys) + ")")
                    params.extend(insert_key)
                where_clause = " OR ".join(conditions)
                conn.execute(
                    f"INSERT INTO {target_table} ({staging_select_cols}) "
                    f"SELECT {staging_select_cols} FROM {staging_table} "
                    f"WHERE {where_clause}",
                    params,
                )
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {staging_table}")
        except duckdb.Error:
            logger.exception("Failed to drop staging table %s", staging_table)
        conn.unregister("_merge_batch_arrow")

    return MergeResult(
        inserted=len(inserts),
        revised=len(revisions),
        unchanged=len(unchanged),
    )
