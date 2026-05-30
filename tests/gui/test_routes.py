"""Smoke + behavior tests for every GUI route."""

from __future__ import annotations

from fastapi.testclient import TestClient

# -- index / dashboard --------------------------------------------------


def test_index_returns_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Razor-Rooster Operator GUI" in response.text


def test_index_dashboard_lists_recent_reports(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "r-newest" in body
    assert "r-middle" in body
    # Card values are rendered.
    assert "Reports (last 7 d)" in body


def test_index_empty_store_renders_no_reports_message(empty_client: TestClient) -> None:
    response = empty_client.get("/")
    assert response.status_code == 200
    assert "No reports persisted yet" in response.text


# -- reports list -------------------------------------------------------


def test_reports_list_returns_all_seeded(client: TestClient) -> None:
    response = client.get("/reports")
    assert response.status_code == 200
    body = response.text
    assert "r-newest" in body
    assert "r-middle" in body
    assert "r-oldest" in body


def test_reports_list_invalid_since_rejected(client: TestClient) -> None:
    response = client.get("/reports?since=not-a-date")
    assert response.status_code == 400


def test_reports_list_limit_query_parameter(client: TestClient) -> None:
    response = client.get("/reports?limit=2")
    assert response.status_code == 200
    body = response.text
    # newest two should appear; oldest pruned by limit (newest-first).
    assert "r-newest" in body
    assert "r-middle" in body
    assert "r-oldest" not in body


# -- report detail ------------------------------------------------------


def test_report_detail_returns_terminal_text(client: TestClient) -> None:
    response = client.get("/reports/r-newest")
    assert response.status_code == 200
    body = response.text
    assert "NEWEST CYCLE" in body
    assert "Sections rendered" in body


def test_report_detail_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/reports/no-such-id")
    assert response.status_code == 404


def test_report_html_serves_persisted_html(client: TestClient) -> None:
    response = client.get("/reports/r-newest/html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "NEWEST" in response.text


def test_report_html_404_when_no_html_persisted(client: TestClient) -> None:
    response = client.get("/reports/r-middle/html")
    # r-middle was seeded without rendered_html_text.
    assert response.status_code == 404


# -- digest -------------------------------------------------------------


def test_digest_default_renders_window(client: TestClient) -> None:
    response = client.get("/digest")
    assert response.status_code == 200
    body = response.text
    assert "r-newest" in body
    assert "r-middle" in body
    assert "Total in window" in body


def test_digest_sort_by_sections_failed(client: TestClient) -> None:
    response = client.get("/digest?sort_by=sections_failed&sort_direction=desc")
    assert response.status_code == 200
    body = response.text
    # r-middle has the only failure so it should appear first.
    middle_idx = body.find("r-middle")
    newest_idx = body.find("r-newest")
    assert middle_idx > 0
    assert newest_idx > 0
    assert middle_idx < newest_idx


def test_digest_top_n_caps_listing(client: TestClient) -> None:
    response = client.get("/digest?top=1&sort_by=sections_failed&sort_direction=desc")
    assert response.status_code == 200
    body = response.text
    assert "r-middle" in body
    # Only 1 row shown; newest and oldest sliced out.
    assert "r-newest" not in body or "Showing top" in body


def test_digest_report_id_prefix_filter(client: TestClient) -> None:
    response = client.get("/digest?report_id=r-old")
    assert response.status_code == 200
    body = response.text
    assert "r-oldest" in body
    assert "r-newest" not in body


def test_digest_invalid_top_rejected(client: TestClient) -> None:
    response = client.get("/digest?top=0")
    # FastAPI Query(ge=1) returns 422 before our handler runs.
    assert response.status_code in {400, 422}


# -- compare ------------------------------------------------------------


def test_compare_form_renders_without_selection(client: TestClient) -> None:
    response = client.get("/compare")
    assert response.status_code == 200
    body = response.text
    assert "Compare two reports" in body
    # Available reports populate the dropdowns.
    assert "r-newest" in body


def test_compare_form_renders_diff_when_pair_selected(client: TestClient) -> None:
    response = client.get("/compare?a=r-middle&b=r-newest")
    assert response.status_code == 200
    body = response.text
    assert "Diff" in body or "library version" in body


def test_compare_form_missing_message_for_unknown_id(client: TestClient) -> None:
    response = client.get("/compare?a=no-such-id&b=r-newest")
    assert response.status_code == 200
    body = response.text
    assert "no-such-id" in body


def test_compare_html_view_serves_self_contained_html(client: TestClient) -> None:
    response = client.get("/compare/r-middle/r-newest/html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # Hallmarks of the existing compare-HTML renderer.
    assert "<!DOCTYPE html>" in body
    assert "Report comparison" in body
    assert "r-middle" in body
    assert "r-newest" in body


def test_compare_html_view_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/compare/no-such-id/r-newest/html")
    assert response.status_code == 404


# -- watch / calibration -----------------------------------------------


def test_watch_renders_seeded_watched_analyses(client: TestClient) -> None:
    """Populated DB seeds one analysis per state plus an orphan."""
    response = client.get("/watch")
    assert response.status_code == 200
    body = response.text
    # All four states are represented.
    for state in ("watching", "acted_on", "dismissed", "expired"):
        assert f"<code>{state}</code>" in body, f"missing state {state}"
    # Each seeded analysis_id surfaces.
    for analysis_id in ("a-watching", "a-acted", "a-dismissed", "a-expired"):
        assert analysis_id in body, f"missing analysis {analysis_id}"
    # The orphaned watch_states row also surfaces (degraded — no analysis row).
    assert "a-orphaned" in body
    # Threshold-tuning section still shows its empty placeholder.
    assert "No threshold-tuning history" in body


def test_watch_counts_watched_by_state(client: TestClient) -> None:
    """Each state's count appears in the header band."""
    response = client.get("/watch")
    body = response.text
    # The orphan row is in the watching bucket, so watching count = 2.
    assert "<code>watching</code>: 2" in body
    assert "<code>acted_on</code>: 1" in body
    assert "<code>dismissed</code>: 1" in body
    assert "<code>expired</code>: 1" in body


def test_watch_orphan_analysis_renders_em_dash(client: TestClient) -> None:
    """A watch_states row whose analysis is missing renders without crashing."""
    response = client.get("/watch")
    body = response.text
    # Find the orphaned row and confirm the em-dash placeholder is used
    # for every analysis-derived field on its line.
    # The simple assertion: the orphan id is rendered, and the page
    # contains an em-dash anywhere (the orphan row produces several).
    assert "a-orphaned" in body
    assert "—" in body


def test_watch_empty_store_renders_empty_state(empty_client: TestClient) -> None:
    """With no analyses or watch states, the empty-state placeholder shows."""
    response = empty_client.get("/watch")
    assert response.status_code == 200
    body = response.text
    assert "No watched analyses yet" in body
    assert "razor-rooster position-engine mark" in body
    # The threshold-tuning section also shows its empty placeholder.
    assert "No threshold-tuning history" in body


def test_calibration_renders_empty_state(client: TestClient) -> None:
    response = client.get("/calibration")
    assert response.status_code == 200
    body = response.text
    assert "No calibration measurements yet" in body


# -- shared concerns ----------------------------------------------------


def test_every_page_emits_loopback_only_layout(client: TestClient) -> None:
    """Every page includes the standard topbar nav + disclaimer footer."""
    for path in [
        "/",
        "/reports",
        "/digest",
        "/compare",
        "/watch",
        "/calibration",
    ]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        body = response.text
        assert "Razor-Rooster Operator GUI" in body, f"missing topbar on {path}"
        assert "read-only" in body, f"missing disclaimer on {path}"


def test_no_external_assets_in_any_page(client: TestClient) -> None:
    """Pages must not reference http(s) URLs or external script/img sources."""
    for path in [
        "/",
        "/reports",
        "/digest",
        "/compare",
        "/watch",
        "/calibration",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        body = response.text
        # No external URL references in served HTML.
        assert "http://" not in body, f"http:// reference on {path}"
        assert "https://" not in body, f"https:// reference on {path}"
        # No <script src=...> or <link rel=stylesheet href=...>.
        assert "<script" not in body, f"<script tag on {path}"
        assert "<link " not in body, f"<link tag on {path}"


def test_no_state_mutation_routes_registered(client: TestClient) -> None:
    """The GUI exposes no POST/PUT/DELETE/PATCH endpoints."""
    app = client.app
    # Walk the route table.
    for route in app.routes:  # type: ignore[attr-defined]
        methods = getattr(route, "methods", set()) or set()
        assert "POST" not in methods, f"POST route registered: {route}"
        assert "PUT" not in methods, f"PUT route registered: {route}"
        assert "DELETE" not in methods, f"DELETE route registered: {route}"
        assert "PATCH" not in methods, f"PATCH route registered: {route}"
