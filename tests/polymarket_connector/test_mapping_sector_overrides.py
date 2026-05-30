"""T-PMC-051 — sector overrides + persistence + needs-review tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.mapping.sector_heuristic import (
    HEURISTIC_TAG,
    INFERRED_CONFIDENCE,
    SectorMapping,
)
from razor_rooster.polymarket_connector.mapping.sector_overrides import (
    MANUAL_CONFIDENCE,
    OPERATOR_TAG,
    get_mapping,
    mapping_stats,
    needs_review,
    set_override,
    upsert_inferred_mapping,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_mapping.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _heuristic_mapping(
    sector: str | None,
    *,
    secondary: tuple[str, ...] = (),
) -> SectorMapping:
    return SectorMapping(
        razor_sector=sector,
        secondary_sectors=secondary,
        confidence=INFERRED_CONFIDENCE,
        scores={"public_health": 1} if sector == "public_health" else {},
        mapped_by=HEURISTIC_TAG,
    )


# -- upsert_inferred_mapping ----------------------------------------------
def test_upsert_inferred_inserts_first_time(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upserted = upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping("public_health"),
        )
        row = get_mapping(conn, "0xabc")

    assert upserted is True
    assert row is not None
    assert row.razor_sector == "public_health"
    assert row.confidence == INFERRED_CONFIDENCE
    assert row.mapped_by == HEURISTIC_TAG


def test_upsert_inferred_updates_existing_inferred(store: DuckDBStore) -> None:
    """A subsequent inferred upsert overwrites the prior inferred row."""
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping("public_health"),
        )
        upserted = upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping("commodity"),
        )
        row = get_mapping(conn, "0xabc")

    assert upserted is True
    assert row is not None
    assert row.razor_sector == "commodity"


def test_upsert_inferred_preserves_manual_override(store: DuckDBStore) -> None:
    """A manual override is never clobbered by a heuristic upsert."""
    with store.connection() as conn:
        set_override(conn, condition_id="0xabc", razor_sector="regulatory")
        upserted = upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping("public_health"),
        )
        row = get_mapping(conn, "0xabc")

    assert upserted is False
    assert row is not None
    assert row.razor_sector == "regulatory"
    assert row.confidence == MANUAL_CONFIDENCE
    assert row.mapped_by == OPERATOR_TAG


def test_upsert_inferred_persists_secondary_sectors(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping(
                "public_health",
                secondary=("commodity", "regulatory"),
            ),
        )
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.secondary_sectors == ("commodity", "regulatory")


def test_upsert_inferred_handles_none_sector(store: DuckDBStore) -> None:
    """The heuristic's ambiguous result (sector=None) is still recorded."""
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping(None, secondary=("commodity", "public_health")),
        )
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector is None
    assert row.confidence == INFERRED_CONFIDENCE
    assert "commodity" in row.secondary_sectors


# -- set_override ---------------------------------------------------------
def test_set_override_inserts_when_no_prior_row(store: DuckDBStore) -> None:
    with store.connection() as conn:
        set_override(
            conn,
            condition_id="0xabc",
            razor_sector="climate",
            secondary=["geopolitical"],
        )
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector == "climate"
    assert row.secondary_sectors == ("geopolitical",)
    assert row.confidence == MANUAL_CONFIDENCE
    assert row.mapped_by == OPERATOR_TAG


def test_set_override_replaces_inferred(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping("public_health"),
        )
        set_override(conn, condition_id="0xabc", razor_sector="climate")
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector == "climate"
    assert row.confidence == MANUAL_CONFIDENCE


def test_set_override_with_null_sector_is_explicit_decision(
    store: DuckDBStore,
) -> None:
    """Operator can confirm 'no sector' as a manual decision."""
    with store.connection() as conn:
        set_override(conn, condition_id="0xabc", razor_sector=None)
        row = get_mapping(conn, "0xabc")
        review = needs_review(conn)

    assert row is not None
    assert row.razor_sector is None
    assert row.confidence == MANUAL_CONFIDENCE
    # Manual null is NOT pending review; only inferred-null is.
    assert all(r.condition_id != "0xabc" for r in review)


def test_set_override_explicit_when(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    with store.connection() as conn:
        set_override(
            conn,
            condition_id="0xabc",
            razor_sector="climate",
            when=when,
        )
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.mapped_at == when


def test_set_override_replaces_prior_manual(store: DuckDBStore) -> None:
    """A manual override can be revised by the operator."""
    with store.connection() as conn:
        set_override(conn, condition_id="0xabc", razor_sector="climate")
        set_override(conn, condition_id="0xabc", razor_sector="geopolitical")
        row = get_mapping(conn, "0xabc")

    assert row is not None
    assert row.razor_sector == "geopolitical"
    assert row.confidence == MANUAL_CONFIDENCE


# -- needs_review --------------------------------------------------------
def test_needs_review_lists_inferred_nulls(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping(None),
        )
        upsert_inferred_mapping(
            conn,
            condition_id="0xdef",
            mapping=_heuristic_mapping("commodity"),
        )
        review = needs_review(conn)

    assert {r.condition_id for r in review} == {"0xabc"}


def test_needs_review_excludes_manual_nulls(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0xabc",
            mapping=_heuristic_mapping(None),
        )
        set_override(conn, condition_id="0xdef", razor_sector=None)
        review = needs_review(conn)

    ids = {r.condition_id for r in review}
    assert "0xabc" in ids
    assert "0xdef" not in ids  # manual null is not pending


def test_needs_review_respects_limit(store: DuckDBStore) -> None:
    with store.connection() as conn:
        for i in range(5):
            upsert_inferred_mapping(
                conn,
                condition_id=f"0x{i}",
                mapping=_heuristic_mapping(None),
            )
        review = needs_review(conn, limit=3)

    assert len(review) == 3


# -- mapping_stats -------------------------------------------------------
def test_mapping_stats_counts_by_sector(store: DuckDBStore) -> None:
    with store.connection() as conn:
        upsert_inferred_mapping(
            conn,
            condition_id="0x1",
            mapping=_heuristic_mapping("public_health"),
        )
        upsert_inferred_mapping(
            conn,
            condition_id="0x2",
            mapping=_heuristic_mapping("public_health"),
        )
        upsert_inferred_mapping(
            conn,
            condition_id="0x3",
            mapping=_heuristic_mapping("commodity"),
        )
        upsert_inferred_mapping(
            conn,
            condition_id="0x4",
            mapping=_heuristic_mapping(None),
        )
        set_override(conn, condition_id="0x5", razor_sector="climate")
        stats = mapping_stats(conn)

    assert stats.by_sector["public_health"] == 2
    assert stats.by_sector["commodity"] == 1
    assert stats.by_sector["climate"] == 1
    assert stats.unmapped == 1
    assert stats.by_confidence[INFERRED_CONFIDENCE] == 4
    assert stats.by_confidence[MANUAL_CONFIDENCE] == 1


def test_mapping_stats_empty_table(store: DuckDBStore) -> None:
    with store.connection() as conn:
        stats = mapping_stats(conn)

    assert stats.by_sector == {}
    assert stats.by_confidence == {}
    assert stats.unmapped == 0


def test_get_mapping_returns_none_for_unknown(store: DuckDBStore) -> None:
    with store.connection() as conn:
        assert get_mapping(conn, "0xnotpresent") is None
