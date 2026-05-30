"""T-MD-022 — mapping resolver acceptance tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.engines.mapping_resolver import (
    derive_auto_mappings,
    resolve,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    register_mapping,
    remove_mapping,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    registry._set_discovered_for_tests(True)
    yield
    registry._clear_for_tests()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "md_resolver.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_class(class_id: str, sector: Sector = Sector.PUBLIC_HEALTH) -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"Test class {class_id} title",
        description=f"Test class {class_id} description with health emergency content",
        domain_sector=sector,
        occurrence_query=_occurrences,
    )


def _seed_market(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    sector: str = "public_health",
    question: str = "Will WHO declare emergency in 2026?",
    active: bool = True,
    closed: bool = False,
) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    conn.execute(
        "INSERT INTO polymarket_markets ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, slug, question, description, category, subcategory, tags, "
        "event_id, market_type, outcome_tokens, end_date, active, closed, resolved, "
        "volume_lifetime, created_at_polymarket, last_updated_polymarket, removed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, NULL, "
        "?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, ?, ?, FALSE, "
        "NULL, NULL, NULL, NULL)",
        [
            "polymarket",
            f"market-{condition_id}",
            now,
            now,
            "test@1",
            json.dumps({"raw": "synthetic"}),
            condition_id,
            f"slug-{condition_id}",
            question,
            "Resolves YES if event happens by year-end.",
            "binary",
            json.dumps([{"id": "tok-yes", "outcome": "Yes"}, {"id": "tok-no", "outcome": "No"}]),
            active,
            closed,
        ],
    )
    conn.execute(
        "INSERT INTO polymarket_sector_mapping ("
        "condition_id, razor_sector, secondary_sectors, confidence, mapped_at, mapped_by"
        ") VALUES (?, ?, NULL, 'inferred', ?, 'auto')",
        [condition_id, sector, now],
    )


def test_resolve_returns_only_operator_when_no_markets(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    register_mapping(conn, class_id="c_a", condition_id="0xabc", mapping_type="direct")
    mappings = resolve(conn)
    assert len(mappings) == 1
    assert mappings[0].mapped_by == "operator"


def test_auto_mappings_skip_already_operator_mapped_pairs(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    register_mapping(conn, class_id="c_a", condition_id="0xabc", mapping_type="direct")
    _seed_market(
        conn,
        condition_id="0xabc",
        question="Will WHO declare a Public Health Emergency in 2026?",
    )
    mappings = resolve(conn)
    # Only the operator mapping; auto skipped because pair exists.
    assert len(mappings) == 1
    assert mappings[0].mapped_by == "operator"


def test_auto_mappings_skip_tombstoned_pairs(conn: duckdb.DuckDBPyConnection) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    m = register_mapping(conn, class_id="c_a", condition_id="0xabc", mapping_type="direct")
    remove_mapping(conn, mapping_id=m.mapping_id)
    _seed_market(
        conn,
        condition_id="0xabc",
        question="Will WHO declare a Public Health Emergency in 2026?",
    )
    mappings = resolve(conn)
    # No active mapping (tombstoned) and no auto-mapping (pair tombstoned).
    assert mappings == ()


def test_auto_mappings_produced_for_unblocked_pair(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    _seed_market(
        conn,
        condition_id="0xnew",
        question=("Will WHO declare a Public Health Emergency of International Concern in 2026?"),
    )
    mappings = resolve(conn)
    assert len(mappings) == 1
    assert mappings[0].mapped_by == "auto"
    assert mappings[0].mapping_confidence in {"inferred", "low"}


def test_filter_by_class_id(conn: duckdb.DuckDBPyConnection) -> None:
    cls_a = _make_class("c_a")
    cls_b = _make_class("c_b", sector=Sector.GEOPOLITICAL)
    registry.register(cls_a)
    registry.register(cls_b)
    register_mapping(conn, class_id="c_a", condition_id="0xabc", mapping_type="direct")
    register_mapping(conn, class_id="c_b", condition_id="0xdef", mapping_type="direct")
    only_a = resolve(conn, class_id_filter="c_a")
    assert {m.class_id for m in only_a} == {"c_a"}


def test_inactive_or_closed_market_does_not_auto_map(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    _seed_market(
        conn,
        condition_id="0xinactive",
        active=False,
        question="Will WHO declare PHEIC in 2026?",
    )
    _seed_market(
        conn,
        condition_id="0xclosed",
        active=True,
        closed=True,
        question="Will WHO declare PHEIC in 2026?",
    )
    mappings = derive_auto_mappings(conn, when=datetime(2026, 5, 15, tzinfo=UTC))
    # Both filtered out by the SQL active+closed predicates.
    assert mappings == ()


def test_auto_mappings_not_persisted_to_table(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    cls = _make_class("c_a")
    registry.register(cls)
    _seed_market(
        conn,
        condition_id="0xnew",
        question="Will WHO declare a Public Health Emergency in 2026?",
    )
    mappings = resolve(conn)
    assert any(m.mapped_by == "auto" for m in mappings)
    rows = conn.execute(
        "SELECT COUNT(*) FROM class_market_mappings WHERE mapped_by = 'auto'"
    ).fetchone()
    # Auto mappings are computed in-memory only; not persisted.
    assert rows is not None and rows[0] == 0
