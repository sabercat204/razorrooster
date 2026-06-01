"""Route tests for the calibration-backtest GUI surface (T-CB-036, T-CB-037).

Exercises the list view and run-detail view against a seeded DuckDB
store. Each test uses the FastAPI ``TestClient`` wrapping
``create_app(db_path=...)`` so the global ``LinterMiddleware`` stays in
the loop and any imperative-language regression surfaces as a 500.

The ``populated_backtest_db`` fixture lives here for v1 — Phase D moves
it into ``tests/gui/conftest.py``. Until then, the local fixture is the
single seed source for both list and detail tests so they share row
shapes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    ScoreSummary,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.gui.app import create_app

# ---------------------------------------------------------------------------
# Seeded data shape used by every test in this module.
# ---------------------------------------------------------------------------

_BASE_STARTED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2025, 12, 1, tzinfo=UTC)


def _make_diagram() -> ReliabilityDiagram:
    """A small, valid two-bin reliability diagram."""

    return ReliabilityDiagram(
        bin_count=2,
        bins=(
            ReliabilityBin(
                lower_p=0.0,
                upper_p=0.5,
                count=2,
                mean_predicted_p=0.25,
                empirical_rate=0.0,
            ),
            ReliabilityBin(
                lower_p=0.5,
                upper_p=1.0,
                count=2,
                mean_predicted_p=0.75,
                empirical_rate=1.0,
            ),
        ),
    )


def _make_summary(*, fallback_count: int, scored: int) -> ScoreSummary:
    """Build a :class:`ScoreSummary` with the row's counts factored in.

    The fallback rate is computed against ``scored`` so the same helper
    seeds both the "low fallback" and "high fallback" runs the tests
    inspect.
    """

    return ScoreSummary(
        overall_brier=0.16,
        per_sector_brier={"public_health": 0.16},
        per_class_brier={"flu_h2h": 0.16},
        reliability_diagrams={"public_health": _make_diagram()},
        zero_resolutions_sectors=(),
        zero_resolutions_classes=(),
        fallback_polarity_count=fallback_count,
        fallback_polarity_rate=(fallback_count / scored) if scored > 0 else 0.0,
    )


def _make_run(
    *,
    run_id: str,
    started_at: datetime,
    status: BacktestStatus,
    predictions_total: int,
    predictions_scored: int,
    predictions_skipped: int,
    fallback_count: int,
    overall_brier: float | None,
    summary: ScoreSummary | None,
    completed_at: datetime | None,
) -> BacktestRun:
    summary_json = summary.as_mapping() if summary is not None else None
    return BacktestRun(
        run_id=run_id,
        since_ts=_SINCE,
        until_ts=_UNTIL,
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=("public_health",),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeef" * 4,  # 32 chars, exercises the 16-char prefix
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        error_summary=None,
        predictions_total=predictions_total,
        predictions_scored=predictions_scored,
        predictions_skipped=predictions_skipped,
        overall_brier=overall_brier,
        summary_json=summary_json,
        bin_count_global=10,
        bin_count_per_sector={"public_health": 5},
        fallback_polarity_count=fallback_count,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _make_prediction(*, run_id: str, prediction_id: str) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_SINCE + timedelta(days=1),
        resolution_ts=_SINCE + timedelta(days=8),
        model_p=0.4,
        observed=1.0,
        polarity=PolarityValue.FORWARD,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=0.36,
    )


def _make_skipped_prediction(
    *, run_id: str, prediction_id: str, skip_reason: SkipReason
) -> BacktestPrediction:
    """A prediction skipped for *skip_reason* (no model_p / observed / brier)."""

    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_SINCE + timedelta(days=1),
        resolution_ts=_SINCE + timedelta(days=8),
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.NO_POLARITY,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=skip_reason,
        brier_contribution=None,
    )


@pytest.fixture
def populated_backtest_db(tmp_path: Path) -> Path:
    """Seed a DuckDB at ``tmp_path`` with three calibration-backtest runs.

    The runs cover (a) a healthy ``complete`` row, (b) a high-fallback
    ``complete`` row exercising the >5% banner path, and (c) an
    ``in_progress`` row used by the status-badge assertions.
    """

    path = tmp_path / "backtest.duckdb"
    store = DuckDBStore(path)
    try:
        with store.connection() as conn:
            run_pending_calibration_backtest_migrations(conn)
            # Newest: low fallback (2/100 = 2%).
            healthy = _make_run(
                run_id="run-healthy-aaaaaaaaaaaaaaa",
                started_at=_BASE_STARTED,
                status=BacktestStatus.COMPLETE,
                predictions_total=100,
                predictions_scored=100,
                predictions_skipped=0,
                fallback_count=2,
                overall_brier=0.16,
                summary=_make_summary(fallback_count=2, scored=100),
                completed_at=_BASE_STARTED + timedelta(minutes=5),
            )
            # Middle: high fallback (15/50 = 30%).
            high_fallback = _make_run(
                run_id="run-fallback-bbbbbbbbbbbbbb",
                started_at=_BASE_STARTED - timedelta(hours=2),
                status=BacktestStatus.COMPLETE,
                predictions_total=50,
                predictions_scored=50,
                predictions_skipped=0,
                fallback_count=15,
                overall_brier=0.20,
                summary=_make_summary(fallback_count=15, scored=50),
                completed_at=_BASE_STARTED - timedelta(hours=2) + timedelta(minutes=5),
            )
            # Oldest: in-progress, no summary.
            in_progress = _make_run(
                run_id="run-progress-ccccccccccccc",
                started_at=_BASE_STARTED - timedelta(hours=4),
                status=BacktestStatus.IN_PROGRESS,
                predictions_total=0,
                predictions_scored=0,
                predictions_skipped=0,
                fallback_count=0,
                overall_brier=None,
                summary=None,
                completed_at=None,
            )
            for run in (healthy, high_fallback, in_progress):
                operations.insert_run(conn, run)
            # Seed a couple of predictions on the healthy run so the
            # detail view has rows to point its scoring tables at.
            operations.insert_prediction(
                conn,
                _make_prediction(run_id=healthy.run_id, prediction_id="pred-001"),
            )
            operations.insert_prediction(
                conn,
                _make_prediction(run_id=healthy.run_id, prediction_id="pred-002"),
            )
    finally:
        store.close()
    return path


@pytest.fixture
def backtest_client(populated_backtest_db: Path) -> Iterator[TestClient]:
    """TestClient bound to the calibration-backtest seed DB."""

    app = create_app(db_path=populated_backtest_db)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# T-CB-038 — predictions-table fixture
# ---------------------------------------------------------------------------


# 30 scored + 6 mapping_not_found + 6 invalid_resolution = 42 (>= 2 * limit=20).
_PREDICTIONS_RUN_ID = "run-preds-ddddddddddddddddd"
_PREDICTIONS_SCORED_COUNT = 30
_PREDICTIONS_SKIP_MAPPING_COUNT = 6
_PREDICTIONS_SKIP_INVALID_COUNT = 6
_PREDICTIONS_TOTAL = (
    _PREDICTIONS_SCORED_COUNT + _PREDICTIONS_SKIP_MAPPING_COUNT + _PREDICTIONS_SKIP_INVALID_COUNT
)


def _seed_predictions_run(conn: duckdb.DuckDBPyConnection) -> None:
    """Seed a calibration-backtest run loaded for the T-CB-038 tests.

    The run carries 30 scored predictions, 6 ``MAPPING_NOT_FOUND``
    skips, and 6 ``INVALID_RESOLUTION`` skips — 42 total — so the
    pagination boundary tests have ``>= 2 * limit`` rows to exercise.
    Prediction IDs are zero-padded so ``ORDER BY prediction_id ASC``
    sorts deterministically.
    """

    run = _make_run(
        run_id=_PREDICTIONS_RUN_ID,
        started_at=_BASE_STARTED + timedelta(hours=1),
        status=BacktestStatus.COMPLETE,
        predictions_total=_PREDICTIONS_TOTAL,
        predictions_scored=_PREDICTIONS_SCORED_COUNT,
        predictions_skipped=(_PREDICTIONS_SKIP_MAPPING_COUNT + _PREDICTIONS_SKIP_INVALID_COUNT),
        fallback_count=0,
        overall_brier=0.16,
        summary=_make_summary(fallback_count=0, scored=_PREDICTIONS_SCORED_COUNT),
        completed_at=_BASE_STARTED + timedelta(hours=1, minutes=5),
    )
    operations.insert_run(conn, run)
    for idx in range(_PREDICTIONS_SCORED_COUNT):
        operations.insert_prediction(
            conn,
            _make_prediction(
                run_id=_PREDICTIONS_RUN_ID,
                prediction_id=f"pred-scored-{idx:03d}",
            ),
        )
    for idx in range(_PREDICTIONS_SKIP_MAPPING_COUNT):
        operations.insert_prediction(
            conn,
            _make_skipped_prediction(
                run_id=_PREDICTIONS_RUN_ID,
                prediction_id=f"pred-skip-mapping-{idx:03d}",
                skip_reason=SkipReason.MAPPING_NOT_FOUND,
            ),
        )
    for idx in range(_PREDICTIONS_SKIP_INVALID_COUNT):
        operations.insert_prediction(
            conn,
            _make_skipped_prediction(
                run_id=_PREDICTIONS_RUN_ID,
                prediction_id=f"pred-skip-invalid-{idx:03d}",
                skip_reason=SkipReason.INVALID_RESOLUTION,
            ),
        )


@pytest.fixture
def predictions_db(tmp_path: Path) -> Path:
    """Seed a DuckDB containing one run with 42 predictions.

    Used by T-CB-038's pagination, filtering, and "filter dropdown
    includes all reasons" tests.
    """

    path = tmp_path / "predictions.duckdb"
    store = DuckDBStore(path)
    try:
        with store.connection() as conn:
            run_pending_calibration_backtest_migrations(conn)
            _seed_predictions_run(conn)
    finally:
        store.close()
    return path


@pytest.fixture
def predictions_client(predictions_db: Path) -> Iterator[TestClient]:
    """TestClient bound to the loaded-predictions seed DB."""

    app = create_app(db_path=predictions_db)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# T-CB-036 — list view
# ---------------------------------------------------------------------------


def test_list_route_200_with_seeded_runs(backtest_client: TestClient) -> None:
    response = backtest_client.get("/calibration-backtest")
    assert response.status_code == 200
    body = response.text
    # All three seeded run_id prefixes surface (12-char display).
    assert "run-healthy-" in body
    assert "run-fallback" in body
    assert "run-progress" in body


def test_list_run_id_link_format(backtest_client: TestClient) -> None:
    response = backtest_client.get("/calibration-backtest")
    body = response.text
    # The full run_id sits in the href; the display text is the 12-char prefix.
    assert 'href="/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa"' in body
    # Display text uses only the 12-char prefix, never the full id.
    assert "<code>run-healthy-</code>" in body
    assert "<code>run-healthy-aaaaaaaaaaaaaaa</code>" not in body


def test_list_pagination(backtest_client: TestClient) -> None:
    page_one = backtest_client.get("/calibration-backtest?limit=2")
    assert page_one.status_code == 200
    body_one = page_one.text
    assert "run-healthy-" in body_one
    assert "run-fallback" in body_one
    # Oldest row pruned by limit.
    assert "run-progress" not in body_one

    page_two = backtest_client.get("/calibration-backtest?limit=2&offset=2")
    assert page_two.status_code == 200
    body_two = page_two.text
    assert "run-progress" in body_two
    assert "run-healthy-" not in body_two
    assert "run-fallback" not in body_two


# ---------------------------------------------------------------------------
# T-CB-037 — detail view
# ---------------------------------------------------------------------------


def test_detail_route_200(backtest_client: TestClient) -> None:
    response = backtest_client.get("/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa")
    assert response.status_code == 200
    body = response.text
    # Run metadata renders (full run_id and full system_revision).
    assert "run-healthy-aaaaaaaaaaaaaaa" in body
    assert "deadbeef" in body
    # Per-sector / per-class Brier tables surface their labels.
    assert "public_health" in body
    assert "flu_h2h" in body
    # Overall Brier renders with 4-decimal formatting.
    assert "0.1600" in body


def test_detail_missing_run_404(backtest_client: TestClient) -> None:
    response = backtest_client.get("/calibration-backtest/no-such-run")
    assert response.status_code == 404


def test_detail_fallback_banner(backtest_client: TestClient) -> None:
    """The >5% fallback rate run surfaces a warning section."""

    response = backtest_client.get("/calibration-backtest/run-fallback-bbbbbbbbbbbbbb")
    assert response.status_code == 200
    body = response.text
    assert 'class="warning"' in body
    # The healthy run (2% fallback) does NOT surface the banner.
    healthy = backtest_client.get("/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa")
    assert healthy.status_code == 200
    assert 'class="warning"' not in healthy.text


def test_detail_reliability_svgs_present(backtest_client: TestClient) -> None:
    response = backtest_client.get("/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa")
    body = response.text
    # Inline SVG markup present with the expected attributes.
    assert "<svg" in body
    assert 'xmlns="http://www.w3.org/2000/svg"' in body
    assert "viewBox=" in body
    assert "</svg>" in body


# ---------------------------------------------------------------------------
# Shared invariants — no external assets, no state-mutation routes
# ---------------------------------------------------------------------------


def test_no_external_assets_calibration_backtest_routes(
    backtest_client: TestClient,
) -> None:
    """Calibration-backtest pages must not pull in external assets."""

    paths = [
        "/calibration-backtest",
        "/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa",
    ]
    for path in paths:
        response = backtest_client.get(path)
        assert response.status_code == 200
        body = response.text
        # The inline SVG legitimately carries the SVG xmlns URL; strip it
        # before checking for non-SVG external references.
        scrubbed = body.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "http://" not in scrubbed, f"http:// reference on {path}"
        assert "https://" not in scrubbed, f"https:// reference on {path}"
        assert "<script" not in body, f"<script tag on {path}"
        assert "<link " not in body, f"<link tag on {path}"


def test_no_state_mutation_calibration_backtest_routes(
    backtest_client: TestClient,
) -> None:
    """Every calibration-backtest route is GET-only."""

    app = backtest_client.app
    for route in app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", "")
        if not isinstance(path, str) or not path.startswith("/calibration-backtest"):
            continue
        methods: set[str] = getattr(route, "methods", set()) or set()
        assert "POST" not in methods, f"POST route registered: {route}"
        assert "PUT" not in methods, f"PUT route registered: {route}"
        assert "DELETE" not in methods, f"DELETE route registered: {route}"
        assert "PATCH" not in methods, f"PATCH route registered: {route}"


# ---------------------------------------------------------------------------
# T-CB-038 — predictions table pagination + filtering
# ---------------------------------------------------------------------------


_PREDICTIONS_DETAIL_PATH = f"/calibration-backtest/{_PREDICTIONS_RUN_ID}"


def _extract_prediction_ids(body: str) -> list[str]:
    """Pull the full prediction IDs out of the rendered detail HTML.

    Each row's first ``<code>`` cell carries ``title="<full-id>"`` so
    the tests can assert ordering / membership without parsing the
    truncated 12-char display value.
    """

    ids: list[str] = []
    needle = '<code title="pred-'
    cursor = 0
    while True:
        start = body.find(needle, cursor)
        if start == -1:
            break
        value_start = start + len('<code title="')
        value_end = body.find('"', value_start)
        if value_end == -1:
            break
        ids.append(body[value_start:value_end])
        cursor = value_end
    return ids


def test_predictions_pagination(predictions_client: TestClient) -> None:
    """Page 1 + page 2 union equals the full set with no overlap."""

    page_one = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=1&limit=20")
    assert page_one.status_code == 200
    page_two = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=2&limit=20")
    assert page_two.status_code == 200
    page_three = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=3&limit=20")
    assert page_three.status_code == 200

    ids_page_one = _extract_prediction_ids(page_one.text)
    ids_page_two = _extract_prediction_ids(page_two.text)
    ids_page_three = _extract_prediction_ids(page_three.text)
    assert len(ids_page_one) == 20
    assert len(ids_page_two) == 20
    # Page 3 carries the remainder (42 - 40 = 2).
    assert len(ids_page_three) == 2

    set_one = set(ids_page_one)
    set_two = set(ids_page_two)
    set_three = set(ids_page_three)
    # No overlap between consecutive pages.
    assert set_one.isdisjoint(set_two)
    assert set_two.isdisjoint(set_three)
    assert set_one.isdisjoint(set_three)
    # Union covers every seeded prediction.
    assert len(set_one | set_two | set_three) == _PREDICTIONS_TOTAL


def test_predictions_filter_status(predictions_client: TestClient) -> None:
    """``?status=skipped`` returns only skipped rows."""

    response = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?status=skipped&limit=200")
    assert response.status_code == 200
    ids = _extract_prediction_ids(response.text)
    expected = _PREDICTIONS_SKIP_MAPPING_COUNT + _PREDICTIONS_SKIP_INVALID_COUNT
    assert len(ids) == expected
    for prediction_id in ids:
        assert prediction_id.startswith("pred-skip-"), prediction_id

    scored_response = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?status=scored&limit=200")
    assert scored_response.status_code == 200
    scored_ids = _extract_prediction_ids(scored_response.text)
    assert len(scored_ids) == _PREDICTIONS_SCORED_COUNT
    for prediction_id in scored_ids:
        assert prediction_id.startswith("pred-scored-"), prediction_id


def test_predictions_filter_skip_reason(predictions_client: TestClient) -> None:
    """``?skip_reason=mapping_not_found`` returns only matching rows."""

    response = predictions_client.get(
        f"{_PREDICTIONS_DETAIL_PATH}?skip_reason=mapping_not_found&limit=200"
    )
    assert response.status_code == 200
    ids = _extract_prediction_ids(response.text)
    assert len(ids) == _PREDICTIONS_SKIP_MAPPING_COUNT
    for prediction_id in ids:
        assert prediction_id.startswith("pred-skip-mapping-"), prediction_id


def test_predictions_filter_combined(predictions_client: TestClient) -> None:
    """Combined ``status=skipped&skip_reason=invalid_resolution`` is intersected."""

    response = predictions_client.get(
        f"{_PREDICTIONS_DETAIL_PATH}?status=skipped&skip_reason=invalid_resolution&limit=200"
    )
    assert response.status_code == 200
    ids = _extract_prediction_ids(response.text)
    assert len(ids) == _PREDICTIONS_SKIP_INVALID_COUNT
    for prediction_id in ids:
        assert prediction_id.startswith("pred-skip-invalid-"), prediction_id


def test_predictions_invalid_status_400(predictions_client: TestClient) -> None:
    """An unknown ``?status=`` value surfaces as a 400 (not silent zero rows)."""

    response = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?status=not-a-real-status")
    assert response.status_code == 400


def test_predictions_total_pages_correct(predictions_client: TestClient) -> None:
    """``Page N of M`` reflects ``ceil(total / limit)`` for the active filter."""

    # 42 rows, limit=20 -> ceil(42/20) = 3 pages.
    response = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=1&limit=20")
    assert response.status_code == 200
    body = response.text
    assert "Page 1 of 3" in body, body[-2000:]

    # 42 rows, limit=10 -> ceil(42/10) = 5 pages.
    response_ten = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=1&limit=10")
    assert response_ten.status_code == 200
    assert "Page 1 of 5" in response_ten.text

    # Filtered: 6 mapping_not_found rows, limit=4 -> 2 pages.
    response_filtered = predictions_client.get(
        f"{_PREDICTIONS_DETAIL_PATH}?skip_reason=mapping_not_found&limit=4"
    )
    assert response_filtered.status_code == 200
    assert "Page 1 of 2" in response_filtered.text


def test_predictions_filter_dropdown_includes_all_reasons(
    predictions_client: TestClient,
) -> None:
    """The filter tabs surface every skip_reason actually present in the run."""

    response = predictions_client.get(_PREDICTIONS_DETAIL_PATH)
    assert response.status_code == 200
    body = response.text
    # Both reasons present in the filter chrome alongside their counts.
    assert "mapping_not_found" in body
    assert "invalid_resolution" in body
    assert f"({_PREDICTIONS_SKIP_MAPPING_COUNT})" in body
    assert f"({_PREDICTIONS_SKIP_INVALID_COUNT})" in body
    # Aggregate counts surface on the All / Scored / Skipped tabs.
    assert f"All ({_PREDICTIONS_TOTAL})" in body
    assert f"Scored ({_PREDICTIONS_SCORED_COUNT})" in body
    assert f"Skipped ({_PREDICTIONS_SKIP_MAPPING_COUNT + _PREDICTIONS_SKIP_INVALID_COUNT})" in body


def _extract_pagination_hrefs(body: str) -> list[str]:
    """Pull href values from anchors that carry a ``page=`` query param.

    Used by the regression test for the double-HTML-escape bug to inspect
    the rendered links the operator's browser would actually follow.
    """

    hrefs: list[str] = []
    cursor = 0
    while True:
        start = body.find('href="', cursor)
        if start == -1:
            break
        value_start = start + len('href="')
        value_end = body.find('"', value_start)
        if value_end == -1:
            break
        candidate = body[value_start:value_end]
        if "page=" in candidate:
            hrefs.append(candidate)
        cursor = value_end
    return hrefs


def test_predictions_links_round_trip_through_browser_unescape(
    predictions_client: TestClient,
) -> None:
    """Rendered pagination/filter links survive a browser-equivalent unescape.

    Regression for the double-HTML-escape bug where _build_query_string
    pre-encoded ``&`` as ``&amp;`` and Jinja autoescape re-escaped to
    ``&amp;amp;``. The browser would unescape one level back to ``&amp;``
    and the server would receive ``?status=scored&amp;page=1&amp;limit=20``;
    parse_qs then yields ``{'amp;page': ['1']}`` instead of ``{'page': ['1']}``,
    so pagination/filter overrides silently no-op on every click.
    """

    import html
    from urllib.parse import parse_qs, urlparse

    response = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?status=scored&page=2&limit=20")
    assert response.status_code == 200

    hrefs = _extract_pagination_hrefs(response.text)
    assert hrefs, "expected at least one paginated href in the rendered detail page"

    # No href should contain a literal ``amp;`` token after a single
    # browser-equivalent unescape — that would mean the server re-escaped
    # an already-escaped ``&amp;``.
    for href in hrefs:
        unescaped_once = html.unescape(href)
        params = parse_qs(urlparse(unescaped_once).query)
        assert "amp;page" not in params, (
            f"href {href!r} round-trips to params with 'amp;page' key — "
            f"double-HTML-escape regression"
        )
        assert "amp;limit" not in params, (
            f"href {href!r} round-trips to params with 'amp;limit' key — "
            f"double-HTML-escape regression"
        )
        # Page or limit must survive as a usable key.
        assert "page" in params or "limit" in params


def test_predictions_clicking_rendered_next_link_advances_page(
    predictions_client: TestClient,
) -> None:
    """Following the rendered next link should land on the next page.

    The double-HTML-escape bug silently dropped pagination params on
    click; this test exercises the click path end-to-end.
    """

    import html
    from urllib.parse import parse_qs, urlparse

    page_one = predictions_client.get(f"{_PREDICTIONS_DETAIL_PATH}?page=1&limit=20")
    assert page_one.status_code == 200

    next_link = None
    for href in _extract_pagination_hrefs(page_one.text):
        unescaped = html.unescape(href)
        params = parse_qs(urlparse(unescaped).query)
        if params.get("page") == ["2"]:
            next_link = unescaped
            break
    assert next_link is not None, "expected a next link pointing to page=2"

    page_two = predictions_client.get(next_link)
    assert page_two.status_code == 200
    page_one_ids = set(_extract_prediction_ids(page_one.text))
    page_two_ids = set(_extract_prediction_ids(page_two.text))
    assert page_one_ids
    assert page_two_ids
    assert page_one_ids.isdisjoint(page_two_ids), (
        "page 2 (followed via rendered link) returned page 1's rows — double-escape regression"
    )
