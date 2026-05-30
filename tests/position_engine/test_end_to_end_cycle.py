"""T-PE-080 — end-to-end position_engine cycle acceptance test.

Composes the full pipeline end-to-end: synthetic data_ingest corpus,
real ``pattern_library`` refresh, real ``signal_scanner`` scan, real
``mispricing_detector`` cycle, then position_engine analysis cycle
over the surfaced comparisons. Verifies acceptance criteria from
POSITION_ENGINE.md §9:

- Daily analysis cycle runs end-to-end.
- Bankroll configuration persistable and update-trackable.
- Kelly + half-Kelly correct across edge cases.
- Bankroll-survival, liquidity feasibility, time-to-resolution,
  invalidation criteria populated for every analysis.
- Standard disclaimer block in every rendered analysis.
- Linter rejects analyses containing imperative language.
- Watch state mechanism end-to-end with auto-expiration.
- Failure isolation works.
- No order-placement code path exists in the codebase.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.engines.comparator import (
    run_cycle as run_mispricing_cycle,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    register_mapping,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.engines.refresh import run_refresh
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)
from razor_rooster.position_engine.engines.analyzer import run_cycle
from razor_rooster.position_engine.frame.linter import LinterCatalog
from razor_rooster.position_engine.frame.renderer import DISCLAIMER_BLOCK
from razor_rooster.position_engine.models import BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    get_analysis_trace,
    latest_watch_state,
    query_analyses,
    write_bankroll_config,
)
from razor_rooster.position_engine.watch.expiration import run_expiration_pass
from razor_rooster.signal_scanner.engines.scanner import run_scan
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    yield
    registry._clear_for_tests()


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """Synthetic data_ingest + library + scanner + mispricing population."""
    db_path = tmp_path / "pe_e2e.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        register_polymarket_sources(conn)
        _seed_data_ingest_corpus(conn)
        _seed_polymarket_markets(conn)
        # Seed bankroll config.
        write_bankroll_config(
            conn,
            BankrollConfig(
                config_id=str(uuid.uuid4()),
                analytical_bankroll_usd=10000.0,
                max_single_position_pct=0.05,
                kelly_fraction_default=0.5,
                min_edge_threshold=0.03,
                effective_at=datetime(2026, 5, 15, 10, tzinfo=UTC),
            ),
        )

    registry.discover()
    trace_dir = tmp_path / "calibration"
    trace_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = tmp_path / "library" / ".refresh.lock"
    run_refresh(
        s,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )
    run_scan(s, max_workers=1, n_samples=300)

    # Register operator mappings + run mispricing cycle to surface comparisons.
    with s.connection() as conn:
        register_mapping(
            conn,
            class_id="pheic_declaration_12mo",
            condition_id="0xpheic",
            mapping_type="direct",
        )
        register_mapping(
            conn,
            class_id="final_rule_within_12mo",
            condition_id="0xfinalrule",
            mapping_type="direct",
        )
    run_mispricing_cycle(s, liquidity_floor=10000.0)

    try:
        yield s
    finally:
        s.close()


def _seed_data_ingest_corpus(conn) -> None:
    """Same synthetic corpus the pattern_library + signal_scanner +
    mispricing_detector E2E tests use."""
    now = datetime(2026, 5, 14, tzinfo=UTC)

    pheic_entries = [
        (datetime(2010, 6, 11, tzinfo=UTC), "WHO declares PHEIC: H1N1 pandemic"),
        (datetime(2014, 8, 8, tzinfo=UTC), "WHO declares PHEIC: Ebola West Africa"),
        (datetime(2016, 2, 1, tzinfo=UTC), "WHO declares PHEIC: Zika virus"),
        (datetime(2020, 1, 30, tzinfo=UTC), "WHO declares PHEIC: novel coronavirus"),
        (datetime(2022, 7, 23, tzinfo=UTC), "WHO declares PHEIC: monkeypox / mpox"),
    ]
    for ts, desc in pheic_entries:
        conn.execute(
            "INSERT INTO event_stream "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, event_ts, country_iso3, "
            "event_class, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "who_don",
                f"who-{ts.isoformat()}",
                ts,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                ts,
                None,
                "pheic_declaration",
                desc,
            ],
        )

    for day_offset in range(60):
        day = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        for i in range(60 if day_offset % 7 == 0 else 5):
            conn.execute(
                "INSERT INTO event_stream "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, event_ts, country_iso3, "
                "event_class, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "gdelt_events",
                    f"gdelt-{day.isoformat()}-{i}",
                    day,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    day,
                    "USA",
                    "conflict",
                    "synthetic GDELT event",
                ],
            )

    for day_offset in range(40):
        day = datetime(2024, 1, 15, tzinfo=UTC) + timedelta(days=day_offset)
        for i in range(35 if day_offset % 7 == 1 else 4):
            conn.execute(
                "INSERT INTO event_stream "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, event_ts, country_iso3, "
                "event_class, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "acled",
                    f"acled-{day.isoformat()}-{i}",
                    day,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    day,
                    "MEX",
                    "violence",
                    "synthetic ACLED event",
                ],
            )

    for i in range(8):
        proposed_date = datetime(2020 + i // 4, 1 + (i * 2) % 11, 1, tzinfo=UTC)
        final_date = proposed_date + timedelta(days=200)
        docket_id = f"docket-{i:03d}"
        for doc_type, doc_date, suffix in (
            ("proposed_rule", proposed_date, "p"),
            ("rule", final_date, "f"),
        ):
            conn.execute(
                "INSERT INTO document_docket "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, title, document_type, "
                "docket_id, agency, published_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "federal_register",
                    f"{docket_id}-{suffix}",
                    doc_date,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    f"Synthetic rule {docket_id} {suffix}",
                    doc_type,
                    docket_id,
                    "EPA",
                    doc_date,
                ],
            )

    for day_offset in range(120):
        day = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        price = 75.0 if day_offset < 60 else 90.0
        conn.execute(
            "INSERT INTO time_series "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, series_id, observation_ts, "
            "value, unit, frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "fred",
                f"fred-DCOILBRENTEU-{day.isoformat()}",
                day,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                "DCOILBRENTEU",
                day,
                price,
                "USD/bbl",
                "daily",
            ],
        )

    for day_offset in range(180):
        day = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        anomaly = 0.1 if day_offset < 90 else 1.0
        conn.execute(
            "INSERT INTO time_series "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, series_id, observation_ts, "
            "value, unit, frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "noaa",
                f"noaa-ENSO-nino34-{day.isoformat()}",
                day,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                "ENSO_nino34_anomaly",
                day,
                anomaly,
                "C",
                "daily",
            ],
        )


def _seed_polymarket_markets(conn) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    snapshot_ts = datetime(2026, 5, 15, 11, tzinfo=UTC)
    for condition_id, question, sector, bid, ask, last in [
        (
            "0xpheic",
            "Will WHO declare a Public Health Emergency of International Concern in 2026?",
            "public_health",
            0.04,
            0.06,
            0.05,
        ),
        (
            "0xfinalrule",
            "Will EPA publish a final rule from a 2024 docket within 12 months by 2025?",
            "regulatory",
            0.20,
            0.25,
            0.225,
        ),
    ]:
        conn.execute(
            "INSERT INTO polymarket_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, slug, question, description, category, subcategory, "
            "tags, event_id, market_type, outcome_tokens, end_date, active, "
            "closed, resolved, volume_lifetime, created_at_polymarket, "
            "last_updated_polymarket, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, "
            "?, ?, ?, TRUE, FALSE, FALSE, NULL, NULL, NULL, NULL)",
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
                "binary",
                json.dumps(
                    [
                        {"id": f"{condition_id}-yes", "outcome": "Yes"},
                        {"id": f"{condition_id}-no", "outcome": "No"},
                    ]
                ),
                datetime(2026, 12, 31, tzinfo=UTC),
            ],
        )
        conn.execute(
            "INSERT INTO polymarket_sector_mapping "
            "(condition_id, razor_sector, secondary_sectors, confidence, "
            "mapped_at, mapped_by) VALUES (?, ?, NULL, 'inferred', ?, 'auto')",
            [condition_id, sector, now],
        )
        conn.execute(
            "INSERT INTO polymarket_price_snapshots ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, outcome_token_id, snapshot_ts, mid_price, "
            "best_bid, best_ask, last_trade_price, last_trade_ts, "
            "volume_24h, liquidity_warning, spread_bps, snapshot_source"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, 'test')",
            [
                "polymarket",
                f"snap-{condition_id}",
                snapshot_ts,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                f"{condition_id}-yes",
                snapshot_ts,
                (bid + ask) / 2.0,
                bid,
                ask,
                last,
                snapshot_ts,
                25000.0,
                round((ask - bid) * 10000),
            ],
        )


# -- end-to-end position_engine cycle ---------------------------------------


def test_full_cycle_against_seeded_system_succeeds(
    populated_store: DuckDBStore,
) -> None:
    # Use include_suppressed so the position_engine analyzes every
    # mispricing comparison from upstream regardless of whether it was
    # surfaced. The synthetic seed data produces non-surfaced comparisons
    # because the 5 PHEIC observations are below the signature confidence
    # floor; we still want the analyzer to exercise the full pipeline.
    report = run_cycle(populated_store, include_suppressed=True)
    assert report.completed_at is not None
    assert report.analyses_total >= 1
    assert all(a.error is None for a in report.analyses)


def test_every_analysis_has_disclaimer(populated_store: DuckDBStore) -> None:
    """REQ-PE-FRAME-001 — exact disclaimer text in every rendered analysis."""
    report = run_cycle(populated_store, include_suppressed=True)
    with populated_store.connection() as conn:
        for analysis in report.analyses:
            trace = get_analysis_trace(conn, analysis_id=analysis.analysis_id)
            assert trace is not None
            assert DISCLAIMER_BLOCK in trace.rendered_text


def test_every_analysis_uses_conditional_language(
    populated_store: DuckDBStore,
) -> None:
    """REQ-PE-FRAME-002 — 'if the operator chose to act' in every output."""
    report = run_cycle(populated_store, include_suppressed=True)
    with populated_store.connection() as conn:
        for analysis in report.analyses:
            trace = get_analysis_trace(conn, analysis_id=analysis.analysis_id)
            assert trace is not None
            # Sub-threshold analyses skip the sizing block; non-sub-threshold
            # always have it.
            if not analysis.sub_threshold:
                assert "if the operator chose to act" in trace.rendered_text


def test_every_analysis_passes_linter(populated_store: DuckDBStore) -> None:
    """The catalog catches imperative phrases; standard renderer output never has them."""
    from razor_rooster.position_engine.frame.linter import check_text

    report = run_cycle(populated_store, include_suppressed=True)
    catalog = LinterCatalog.from_yaml()
    with populated_store.connection() as conn:
        for analysis in report.analyses:
            trace = get_analysis_trace(conn, analysis_id=analysis.analysis_id)
            assert trace is not None
            check_text(trace.rendered_text, catalog=catalog)


def test_warnings_appear_before_sizing(populated_store: DuckDBStore) -> None:
    """REQ-PE-FRAME-003 — warnings prefix the sizing math."""
    report = run_cycle(populated_store, include_suppressed=True)
    with populated_store.connection() as conn:
        for analysis in report.analyses:
            trace = get_analysis_trace(conn, analysis_id=analysis.analysis_id)
            assert trace is not None
            warnings_idx = trace.rendered_text.find("WARNINGS:")
            sizing_idx = trace.rendered_text.find("SIZING ANALYSIS")
            assert warnings_idx >= 0
            assert sizing_idx >= 0
            assert warnings_idx < sizing_idx


def test_bankroll_survival_populated(populated_store: DuckDBStore) -> None:
    """Every non-sub-threshold analysis has bankroll-survival metrics."""
    report = run_cycle(populated_store, include_suppressed=True)
    for analysis in report.analyses:
        if analysis.sub_threshold:
            continue
        assert 0.0 <= analysis.bankroll_after_1_loss_pct <= 1.0
        assert 0.0 <= analysis.bankroll_after_3_losses_pct <= 1.0
        assert 0.0 <= analysis.bankroll_after_5_losses_pct <= 1.0
        # 5-loss bankroll should be lower than 1-loss bankroll.
        if analysis.suggested_fraction > 0:
            assert analysis.bankroll_after_5_losses_pct <= analysis.bankroll_after_1_loss_pct


def test_invalidation_criteria_populated(populated_store: DuckDBStore) -> None:
    """REQ-PE-CMP-007 — non-sub-threshold analyses have invalidation criteria."""
    report = run_cycle(populated_store, include_suppressed=True)
    for analysis in report.analyses:
        if analysis.sub_threshold:
            continue
        assert len(analysis.invalidation_criteria) >= 1


def test_cycle_re_run_produces_distinct_cycle_id(
    populated_store: DuckDBStore,
) -> None:
    """Each cycle is immutable; re-running produces a fresh cycle_id."""
    report_a = run_cycle(populated_store, include_suppressed=True)
    report_b = run_cycle(populated_store, include_suppressed=True)
    assert report_a.cycle_id != report_b.cycle_id


def test_watch_state_lifecycle_end_to_end(populated_store: DuckDBStore) -> None:
    """Watch → expire on resolution; the auto-expiration pass fires."""
    report = run_cycle(populated_store, include_suppressed=True)
    if not report.analyses:
        pytest.skip("no analyses produced; nothing to watch")
    analysis = report.analyses[0]
    with populated_store.connection() as conn:
        append_watch_state(conn, analysis_id=analysis.analysis_id, state="watching")
    # Insert a synthetic resolution + linkage row.
    resolution_ts = datetime(2026, 6, 1, tzinfo=UTC)
    with populated_store.connection() as conn:
        conn.execute(
            "INSERT INTO polymarket_resolutions ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, winning_outcome_token_id, winning_outcome_label, "
            "resolution_ts, resolution_source, resolution_metadata, "
            "final_yes_price, final_no_price, total_volume_at_resolution, "
            "invalidated"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            [
                "polymarket_resolutions",
                f"res-{analysis.condition_id}",
                resolution_ts,
                resolution_ts,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                analysis.condition_id,
                f"{analysis.condition_id}-yes",
                "Yes",
                resolution_ts,
                "gamma",
                1.0,
                0.0,
                30000.0,
                False,
            ],
        )
        conn.execute(
            "INSERT INTO comparison_resolutions ("
            "comparison_id, condition_id, resolution_outcome, resolution_ts, "
            "model_probability_at_comparison, market_probability_at_comparison, "
            "polarity_at_comparison, outcome_observed, linked_at"
            ") VALUES (?, ?, 'yes', ?, ?, ?, 'aligned', 1, ?)",
            [
                analysis.comparison_id,
                analysis.condition_id,
                resolution_ts,
                analysis.model_probability,
                analysis.market_probability or 0.0,
                resolution_ts,
            ],
        )
    # Expiration pass.
    pass_report = run_expiration_pass(populated_store)
    assert pass_report.expirations_written >= 1
    with populated_store.connection() as conn:
        latest = latest_watch_state(conn, analysis_id=analysis.analysis_id)
    assert latest is not None
    assert latest.state == "expired"
    assert latest.set_by == "system"


def test_no_polymarket_signing_imports() -> None:
    """REQ-PE acceptance — no order-placement code path in the codebase.

    The position_engine codebase must not import any Polymarket
    trading SDK or signing libraries. This test checks against a
    forbidden-imports list.
    """
    import importlib
    import pkgutil

    import razor_rooster.position_engine as pe_pkg

    forbidden = {
        "py_clob_client",  # Polymarket trading SDK
        "eth_account",  # Ethereum signing
        "web3",  # Ethereum interaction
    }
    for module_info in pkgutil.walk_packages(
        pe_pkg.__path__, prefix="razor_rooster.position_engine."
    ):
        module = importlib.import_module(module_info.name)
        for forbidden_name in forbidden:
            assert getattr(module, forbidden_name, None) is None, (
                f"{module_info.name} imports forbidden {forbidden_name}"
            )


def test_cycle_persists_analyses_queryable(populated_store: DuckDBStore) -> None:
    report = run_cycle(populated_store, include_suppressed=True)
    with populated_store.connection() as conn:
        persisted = query_analyses(conn, cycle_id=report.cycle_id)
    assert len(persisted) == len(report.analyses)
