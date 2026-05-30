"""Shared fixtures for the GUI route tests.

Provides a populated DuckDB store and a FastAPI ``TestClient`` bound
to a freshly-built ``create_app`` instance per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.gui.app import create_app
from razor_rooster.position_engine.models import Analysis, BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    persist_analysis,
    write_bankroll_config,
)
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import persist_report


def _make_record(
    *,
    report_id: str,
    generated_at: datetime,
    sections_rendered: tuple[str, ...] = ("system_health", "surfaced"),
    sections_failed: tuple[dict[str, str], ...] = (),
    library_version: int = 1,
    disclaimer_hash: str = "abc123",
    terminal_text: str = "REPORT TEXT",
    rendered_html_text: str | None = None,
    markdown_path: str | None = None,
    html_path: str | None = None,
) -> ReportRecord:
    return ReportRecord(
        report_id=report_id,
        generated_at=generated_at,
        since_ts=generated_at - timedelta(days=1),
        until_ts=generated_at,
        sections_enabled=tuple(sections_rendered),
        sections_rendered=sections_rendered,
        sections_failed=sections_failed,
        library_version=library_version,
        disclaimer_version_hash=disclaimer_hash,
        rendered_terminal_text=terminal_text,
        rendered_html_text=rendered_html_text,
        markdown_path=markdown_path,
        html_path=html_path,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a DuckDB store with the report-generator and position-engine schemas applied."""
    path = tmp_path / "gui.duckdb"
    s = DuckDBStore(path)
    try:
        with s.connection() as c:
            run_pending_report_generator_migrations(c)
            run_pending_position_engine_migrations(c)
    finally:
        s.close()
    return path


def _make_analysis(
    *,
    analysis_id: str,
    class_id: str,
    venue: str = "polymarket",
    bankroll_config_id: str = "bk-1",
    cycle_id: str = "cycle-1",
    model_probability: float = 0.65,
    market_probability: float | None = 0.50,
    suggested_dollar_size: float = 50.0,
    computed_at: datetime | None = None,
) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=f"cmp-{analysis_id}",
        class_id=class_id,
        condition_id=f"cond-{analysis_id}",
        bankroll_config_id=bankroll_config_id,
        model_probability=model_probability,
        market_probability=market_probability,
        kelly_unclamped=0.05,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.05,
        suggested_dollar_size=suggested_dollar_size,
        ev_per_dollar=0.07,
        bankroll_after_1_loss_pct=0.95,
        bankroll_after_3_losses_pct=0.85,
        bankroll_after_5_losses_pct=0.75,
        suggested_pct_of_24h_volume=0.02,
        days_to_resolution=14,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis=None,
        computed_at=computed_at,
        venue=venue,  # type: ignore[arg-type]
    )


@pytest.fixture
def populated_db(db_path: Path) -> Path:
    """Seed the store with three reports plus four position-engine analyses
    spanning every watch state.
    """
    base = datetime.now(tz=UTC)
    s = DuckDBStore(db_path)
    try:
        with s.connection() as c:
            persist_report(
                c,
                _make_record(
                    report_id="r-newest",
                    generated_at=base,
                    sections_rendered=("system_health", "surfaced", "watched"),
                    terminal_text="NEWEST CYCLE\n",
                    rendered_html_text=("<!DOCTYPE html><html><body>NEWEST</body></html>"),
                    markdown_path="/tmp/r-newest.md",
                ),
            )
            persist_report(
                c,
                _make_record(
                    report_id="r-middle",
                    generated_at=base - timedelta(days=2),
                    sections_rendered=("system_health", "surfaced"),
                    sections_failed=({"section": "calibration", "error": "x"},),
                    terminal_text="MIDDLE CYCLE\n",
                ),
            )
            persist_report(
                c,
                _make_record(
                    report_id="r-oldest",
                    generated_at=base - timedelta(days=5),
                    sections_rendered=("system_health",),
                    terminal_text="OLDEST CYCLE\n",
                ),
            )

            # Seed bankroll config + analyses + watch states.
            write_bankroll_config(
                c,
                BankrollConfig(
                    config_id="bk-1",
                    analytical_bankroll_usd=1000.0,
                    max_single_position_pct=0.10,
                    kelly_fraction_default=0.5,
                    min_edge_threshold=0.03,
                    effective_at=base - timedelta(days=10),
                ),
            )
            for ai, cls, st in (
                ("a-watching", "election", "watching"),
                ("a-acted", "regulation", "acted_on"),
                ("a-dismissed", "weather", "dismissed"),
                ("a-expired", "commodity", "expired"),
            ):
                persist_analysis(
                    c,
                    _make_analysis(
                        analysis_id=ai,
                        class_id=cls,
                        computed_at=base - timedelta(hours=12),
                    ),
                )
                append_watch_state(
                    c,
                    analysis_id=ai,
                    state=st,  # type: ignore[arg-type]
                    notes=f"seed-{st}",
                    set_by="operator" if st != "expired" else "system",
                    when=base - timedelta(hours=6),
                )
            # Add a watch_states row referencing a missing analysis to
            # confirm the GUI degrades gracefully (Analysis is None).
            append_watch_state(
                c,
                analysis_id="a-orphaned",
                state="watching",
                notes="analysis row absent on purpose",
                set_by="operator",
                when=base - timedelta(hours=1),
            )
    finally:
        s.close()
    return db_path


@pytest.fixture
def client(populated_db: Path) -> Iterator[TestClient]:
    """FastAPI TestClient bound to a fresh app reading from populated_db."""
    app = create_app(db_path=populated_db)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def empty_client(db_path: Path) -> Iterator[TestClient]:
    """TestClient bound to a store with the schema but zero reports."""
    app = create_app(db_path=db_path)
    with TestClient(app) as test_client:
        yield test_client
