"""T-KSI-051 — Kalshi sector mapping persistence + overrides acceptance tests.

Verifies:
- ``upsert_inferred_mapping`` inserts new rows.
- A second call updates existing inferred rows (idempotent for changes).
- A manual override is preserved across subsequent inferred upserts.
- ``set_override`` writes ``confidence='manual'``.
- ``set_override`` with ``razor_sector=None`` records an explicit
  operator decision (distinct from ambiguous heuristic).
- ``set_override`` accepts the Kalshi-specific ``'out_of_scope'`` value.
- ``needs_review`` returns inferred rows with NULL razor_sector.
- ``needs_review`` excludes manual NULL rows (operator-confirmed).
- ``mapping_stats`` aggregates by sector + confidence.
- ``get_mapping`` returns None for unknown ticker.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.mapping.sector_heuristic import (
    INFERRED_CONFIDENCE,
    SectorMapping,
)
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    get_mapping,
    mapping_stats,
    needs_review,
    set_override,
    upsert_inferred_mapping,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_sector_overrides.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
    yield s
    s.close()


def _make_mapping(
    *,
    razor_sector: str | None = "macroeconomic",
    secondary: tuple[str, ...] = (),
    confidence: str = INFERRED_CONFIDENCE,
) -> SectorMapping:
    return SectorMapping(
        razor_sector=razor_sector,
        secondary_sectors=secondary,
        confidence=confidence,
        scores={"macroeconomic": 1} if razor_sector == "macroeconomic" else {},
    )


def test_upsert_inferred_inserts_new_row(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 16, tzinfo=UTC)
    with store.connection() as conn:
        wrote = upsert_inferred_mapping(
            conn,
            ticker="KXFOO",
            mapping=_make_mapping(),
            when=when,
        )
        row = get_mapping(conn, "KXFOO")
    assert wrote is True
    assert row is not None
    assert row.razor_sector == "macroeconomic"
    assert row.confidence == "inferred"
    assert row.mapped_at == when


def test_upsert_inferred_updates_existing_row(store: DuckDBStore) -> None:
    """A second inferred upsert replaces the prior inferred row."""
    with store.connection() as conn:
        upsert_inferred_mapping(conn, ticker="KXFOO", mapping=_make_mapping())
        # Switch to commodity.
        upsert_inferred_mapping(
            conn,
            ticker="KXFOO",
            mapping=_make_mapping(razor_sector="commodity"),
        )
        row = get_mapping(conn, "KXFOO")
    assert row is not None
    assert row.razor_sector == "commodity"


def test_manual_override_preserved_against_inferred_upsert(store: DuckDBStore) -> None:
    """A subsequent heuristic upsert must NOT clobber a manual override."""
    with store.connection() as conn:
        set_override(
            conn,
            ticker="KXFOO",
            razor_sector="regulatory",
        )
        wrote = upsert_inferred_mapping(
            conn,
            ticker="KXFOO",
            mapping=_make_mapping(razor_sector="macroeconomic"),
        )
        row = get_mapping(conn, "KXFOO")
    assert wrote is False
    assert row is not None
    assert row.razor_sector == "regulatory"
    assert row.confidence == "manual"


def test_set_override_writes_manual_confidence(store: DuckDBStore) -> None:
    with store.connection() as conn:
        set_override(conn, ticker="KXFOO", razor_sector="commodity")
        row = get_mapping(conn, "KXFOO")
    assert row is not None
    assert row.confidence == "manual"


def test_set_override_can_clear_to_none(store: DuckDBStore) -> None:
    """Operator-confirmed null mapping is distinct from ambiguous."""
    with store.connection() as conn:
        upsert_inferred_mapping(conn, ticker="KXFOO", mapping=_make_mapping(razor_sector=None))
        set_override(conn, ticker="KXFOO", razor_sector=None)
        row = get_mapping(conn, "KXFOO")
    assert row is not None
    assert row.razor_sector is None
    assert row.confidence == "manual"


def test_set_override_accepts_out_of_scope(store: DuckDBStore) -> None:
    """Operator can manually mark a Kalshi-specific out_of_scope value."""
    with store.connection() as conn:
        set_override(conn, ticker="KXNFL", razor_sector="out_of_scope")
        row = get_mapping(conn, "KXNFL")
    assert row is not None
    assert row.razor_sector == "out_of_scope"
    assert row.confidence == "manual"


def test_set_override_can_revise_existing_manual(store: DuckDBStore) -> None:
    """A second set_override updates the existing manual row."""
    with store.connection() as conn:
        set_override(conn, ticker="KXFOO", razor_sector="commodity")
        set_override(conn, ticker="KXFOO", razor_sector="macroeconomic")
        row = get_mapping(conn, "KXFOO")
    assert row is not None
    assert row.razor_sector == "macroeconomic"
    assert row.confidence == "manual"


def test_needs_review_lists_inferred_nulls(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            ticker="KXAMBIG",
            mapping=_make_mapping(razor_sector=None, secondary=("a", "b")),
        )
        upsert_inferred_mapping(
            conn,
            ticker="KXOK",
            mapping=_make_mapping(razor_sector="macroeconomic"),
        )
        rows = needs_review(conn)
    tickers = [r.ticker for r in rows]
    assert "KXAMBIG" in tickers
    assert "KXOK" not in tickers


def test_needs_review_excludes_manual_null(store: DuckDBStore) -> None:
    """An operator-confirmed null mapping is not a pending review."""
    with store.connection() as conn:
        set_override(conn, ticker="KXNULL", razor_sector=None)
        rows = needs_review(conn)
    assert all(r.ticker != "KXNULL" for r in rows)


def test_needs_review_respects_limit(store: DuckDBStore) -> None:
    with store.connection() as conn:
        for i in range(5):
            upsert_inferred_mapping(
                conn,
                ticker=f"KX{i}",
                mapping=_make_mapping(razor_sector=None),
            )
        rows = needs_review(conn, limit=2)
    assert len(rows) == 2


def test_mapping_stats_aggregates(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn, ticker="KXA", mapping=_make_mapping(razor_sector="macroeconomic")
        )
        upsert_inferred_mapping(
            conn, ticker="KXB", mapping=_make_mapping(razor_sector="macroeconomic")
        )
        upsert_inferred_mapping(conn, ticker="KXC", mapping=_make_mapping(razor_sector="commodity"))
        upsert_inferred_mapping(conn, ticker="KXD", mapping=_make_mapping(razor_sector=None))
        set_override(conn, ticker="KXE", razor_sector="regulatory")
        stats = mapping_stats(conn)
    assert stats.by_sector.get("macroeconomic") == 2
    assert stats.by_sector.get("commodity") == 1
    assert stats.by_sector.get("regulatory") == 1
    assert stats.unmapped == 1
    assert stats.by_confidence.get("inferred", 0) >= 3
    assert stats.by_confidence.get("manual", 0) == 1


def test_get_mapping_returns_none_for_unknown_ticker(store: DuckDBStore) -> None:
    with store.connection() as conn:
        row = get_mapping(conn, "KX-DOES-NOT-EXIST")
    assert row is None


def test_secondary_sectors_round_trip(store: DuckDBStore) -> None:
    """secondary_sectors persists as JSON and reads back as a tuple."""
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            ticker="KXFOO",
            mapping=_make_mapping(
                razor_sector="macroeconomic",
                secondary=("commodity", "regulatory"),
            ),
        )
        row = get_mapping(conn, "KXFOO")
    assert row is not None
    assert row.secondary_sectors == ("commodity", "regulatory")
