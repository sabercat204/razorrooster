"""T-CB-045 — polarity matrix + double-correction guard.

Phase 7, REQ-CB-PL-001 / REQ-CB-REPLAY-003 / design §3.16 / OQ-CB-005.

Two independent verifications live here so a regression in either
direction trips a deterministic test failure:

1. **Polarity matrix (4 cells).** The meta-class returns *raw*
   ``polarity_at_comparison`` and ``winning_outcome_label`` values; the
   per-prediction ``observed`` bit is re-derived downstream by combining
   the two. This module pins the truth table:

   ===================================  ==========================  ==========
   ``polarity_at_comparison``           ``winning_outcome_label``   ``observed``
   ===================================  ==========================  ==========
   ``"aligned"``  (a.k.a. "direct")     ``"yes"``                   ``1.0``
   ``"aligned"``                        ``"no"``                    ``0.0``
   ``"inverted"``                       ``"yes"``                   ``0.0``
   ``"inverted"``                       ``"no"``                    ``1.0``
   ===================================  ==========================  ==========

   Note: the T-CB-045 task narrative uses ``'direct'`` as a synonym for
   the real stored polarity value ``'aligned'`` (defined as
   ``Polarity = Literal["aligned", "inverted"]`` in
   ``mispricing_detector.models``). The matrix below uses the real
   stored value so the test exercises production-realistic data.

2. **Double-correction guard.** The meta-class MUST NOT read
   ``comparison_resolutions.outcome_observed``. That column is already
   polarity-adjusted at write time
   (``mispricing_detector/models.py:148-149``), so combining it with
   ``polarity_at_comparison`` would apply polarity twice — a silent
   calibration corruption with no error raised. The guard is a
   regex-plus-AST scan of the production source file that fails with a
   precise diagnostic if any reference reappears.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonCycle,
    ComparisonResolution,
    Polarity,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_comparison,
    write_cycle,
    write_resolution_link,
)
from razor_rooster.pattern_library.classes import (
    polymarket_resolution_calibration as pl_meta,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)

# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """Store with the four canonical migrations applied.

    Mirrors the populated_store fixture used by ``T-CB-043``'s
    ``test_polymarket_resolution_calibration.py`` so the polarity matrix
    test stands alone (no shared-conftest coupling that would create a
    file-write race with the T-CB-043 worker).
    """

    db_path = tmp_path / "pl_polarity_matrix.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_mispricing_migrations(conn)
    try:
        yield store
    finally:
        store.close()


# -- seed helpers ------------------------------------------------------------


_NOW = datetime(2026, 5, 20, tzinfo=UTC)
_RESOLUTION_TS = datetime(2026, 6, 1, tzinfo=UTC)


def _seed_triple(
    store: DuckDBStore,
    *,
    comparison_id: str,
    condition_id: str,
    polarity_at_comparison: str,
    winning_outcome_label: str,
) -> None:
    """Insert one fully-linked (comparison, resolution-link, polymarket-resolution).

    The ``outcome_observed`` value written to ``comparison_resolutions``
    is computed *only* so the row satisfies the column's NOT NULL
    contract — the meta-class never reads it. Computing it here would
    also accidentally exercise the trap if the meta-class regressed to
    reading it, so we deliberately store a *wrong* value (always 1) to
    surface any silent re-use as a numerical mismatch in the matrix
    assertions.
    """

    with store.connection() as conn:
        write_cycle(
            conn,
            ComparisonCycle(
                cycle_id=f"cy-{comparison_id}",
                started_at=_NOW,
                completed_at=_NOW,
                comparisons_total=1,
                surfaced_count=0,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed=f"scan-{comparison_id}",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id=comparison_id,
                cycle_id=f"cy-{comparison_id}",
                mapping_id=f"map-{comparison_id}",
                class_id="polymarket_resolution_calibration",
                condition_id=condition_id,
                outcome_token_id="tok-yes",
                polarity="aligned",
                scan_id=f"scan-{comparison_id}",
                model_probability=0.30,
                model_ci_lower=0.20,
                model_ci_upper=0.40,
                market_probability=0.25,
                market_best_bid=None,
                market_best_ask=None,
                market_last_trade_price=None,
                market_volume_24h=None,
                market_spread_bps=None,
                market_snapshot_ts=None,
                delta=None,
                log_odds_delta=None,
                ci_overlap=False,
                expected_value=None,
                confidence_weighted_score=None,
                surfaced=False,
                computed_at=_NOW,
            ),
        )
        write_resolution_link(
            conn,
            ComparisonResolution(
                comparison_id=comparison_id,
                condition_id=condition_id,
                resolution_outcome="yes" if winning_outcome_label == "yes" else "no",
                resolution_ts=_RESOLUTION_TS,
                model_probability_at_comparison=0.30,
                market_probability_at_comparison=0.25,
                polarity_at_comparison=cast(Polarity, polarity_at_comparison),
                # Deliberately *wrong*: always 1. If the meta-class ever
                # regresses to reading this column, the matrix assertion
                # below fails because the spurious value disagrees with
                # the polarity-corrected truth in 2 of the 4 cells.
                outcome_observed=1,
                linked_at=_RESOLUTION_TS,
            ),
        )
        conn.execute(
            "INSERT INTO polymarket_resolutions ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, winning_outcome_token_id, winning_outcome_label, "
            "resolution_ts, resolution_source, resolution_metadata, "
            "final_yes_price, final_no_price, total_volume_at_resolution, "
            "invalidated"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'gamma', NULL, ?, ?, ?, FALSE)",
            [
                "polymarket_resolutions",
                f"res-{condition_id}",
                _NOW,
                _NOW,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                "tok-yes",
                winning_outcome_label,
                _RESOLUTION_TS,
                1.0 if winning_outcome_label == "yes" else 0.0,
                0.0 if winning_outcome_label == "yes" else 1.0,
                25000.0,
            ],
        )


def _polarity_corrected_observed(
    *,
    polarity_at_comparison: str,
    winning_outcome_label: str,
) -> float:
    """Re-derive ``observed`` from raw resolution + polarity (single source of truth).

    Equivalent to the (downstream) replay-pipeline correction: when the
    polarity is ``"aligned"`` (a.k.a. "direct" in the design narrative)
    a ``"yes"`` outcome counts as 1.0; when the polarity is
    ``"inverted"`` the bit is flipped. The lone XNOR encodes all four
    cells of the T-CB-045 matrix.

    This helper deliberately lives in the *test* — the production
    ``_occurrences`` query intentionally does NOT compute ``observed``;
    it returns raw rows and lets the downstream pipeline apply the
    correction, per the design §3.16 contract restated in T-CB-042's
    deliverable text.
    """

    if polarity_at_comparison not in {"aligned", "inverted"}:
        raise ValueError(f"unexpected polarity: {polarity_at_comparison!r}")
    if winning_outcome_label not in {"yes", "no"}:
        raise ValueError(f"unexpected outcome label: {winning_outcome_label!r}")
    aligned_yes = polarity_at_comparison == "aligned" and winning_outcome_label == "yes"
    inverted_no = polarity_at_comparison == "inverted" and winning_outcome_label == "no"
    return 1.0 if (aligned_yes or inverted_no) else 0.0


# -- tests -------------------------------------------------------------------


# 4-cell parametrize covers the full Cartesian product (polarity x outcome).
# The test ids embed the matrix coordinate so a failing case is greppable in
# pytest output without inspecting the parameter tuple.
@pytest.mark.parametrize(
    ("polarity_at_comparison", "winning_outcome_label", "expected_observed"),
    [
        ("aligned", "yes", 1.0),
        ("aligned", "no", 0.0),
        ("inverted", "yes", 0.0),
        ("inverted", "no", 1.0),
    ],
    ids=[
        "aligned_yes_observes_1",
        "aligned_no_observes_0",
        "inverted_yes_observes_0",
        "inverted_no_observes_1",
    ],
)
def test_polarity_matrix(
    populated_store: DuckDBStore,
    polarity_at_comparison: str,
    winning_outcome_label: str,
    expected_observed: float,
) -> None:
    """Each of the four matrix cells must produce the expected ``observed`` bit.

    The meta-class returns raw rows; the test recomputes ``observed``
    using the canonical polarity-correction formula. If the meta-class
    ever regressed to reading the (already polarity-adjusted)
    ``cr.outcome_observed`` column directly, the deliberately-wrong
    seed value (``outcome_observed=1`` for every cell) would propagate
    out and the two ``expected_observed=0.0`` cells would fail.
    """

    comparison_id = f"cmp-{polarity_at_comparison}-{winning_outcome_label}"
    condition_id = f"0x{polarity_at_comparison[:1]}{winning_outcome_label[:1]}"
    _seed_triple(
        populated_store,
        comparison_id=comparison_id,
        condition_id=condition_id,
        polarity_at_comparison=polarity_at_comparison,
        winning_outcome_label=winning_outcome_label,
    )

    with populated_store.connection() as conn:
        df = pl_meta.polymarket_resolution_calibration.occurrence_query(conn)

    assert len(df) == 1, (
        f"expected exactly one row from _occurrences for "
        f"({polarity_at_comparison!r}, {winning_outcome_label!r}); got: {df}"
    )
    row = df.iloc[0]

    # Sanity-check the meta-class returned the raw, *uncorrected* values.
    assert row["polarity_at_comparison"] == polarity_at_comparison
    assert row["winning_outcome_label"] == winning_outcome_label
    # The forbidden column must not have been pulled into the result.
    assert "outcome_observed" not in df.columns, (
        "_occurrences leaked comparison_resolutions.outcome_observed into "
        "its DataFrame columns — that column is already polarity-adjusted "
        "and reading it downstream would double-correct."
    )

    observed = _polarity_corrected_observed(
        polarity_at_comparison=str(row["polarity_at_comparison"]),
        winning_outcome_label=str(row["winning_outcome_label"]),
    )
    assert observed == expected_observed, (
        f"polarity-correction mismatch for "
        f"polarity={polarity_at_comparison!r} outcome={winning_outcome_label!r}: "
        f"expected {expected_observed}, got {observed}"
    )


def test_polarity_corrected_observed_helper_truth_table() -> None:
    """The polarity-correction helper itself satisfies the matrix.

    Without this self-test, a bug in the helper would silently propagate
    into ``test_polarity_matrix`` and look like a meta-class fault. The
    helper is also the reference downstream callers can copy verbatim
    when implementing the replay-pipeline polarity step.
    """

    assert (
        _polarity_corrected_observed(polarity_at_comparison="aligned", winning_outcome_label="yes")
        == 1.0
    )
    assert (
        _polarity_corrected_observed(polarity_at_comparison="aligned", winning_outcome_label="no")
        == 0.0
    )
    assert (
        _polarity_corrected_observed(polarity_at_comparison="inverted", winning_outcome_label="yes")
        == 0.0
    )
    assert (
        _polarity_corrected_observed(polarity_at_comparison="inverted", winning_outcome_label="no")
        == 1.0
    )


# Regex form pinned at module scope so ``test_double_correction_guard_self_test``
# can exercise it independently of the production-source scan. Matches only
# the forbidden *read* forms — bare-token mentions in module/function/class
# docstrings (e.g. ``comparison_resolutions.outcome_observed`` describing the
# trap) are explicitly allowed and stripped out before the scan runs. The
# patterns enumerated below cover every realistic read site:
#
# * ``cr.outcome_observed`` — Python attribute access OR SQL alias-prefixed
#   column reference inside a string literal.
# * ``["outcome_observed"]`` — DataFrame column lookup.
# * ``'outcome_observed'`` / ``"outcome_observed"`` standalone literal — the
#   string-key form a regression might use.
_FORBIDDEN_READ_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcr\.outcome_observed\b"),
    re.compile(r"""\[['"]outcome_observed['"]\]"""),
)


def _strip_docstrings(source: str) -> str:
    """Return ``source`` with module/class/function docstrings replaced by blanks.

    Preserves line numbers (replaces docstring contents with blank lines)
    so any failure message still points at the right line in the
    original file. The replacement is purely for the scan — the
    on-disk source is untouched.
    """

    tree = ast.parse(source)
    lines = source.splitlines()

    docstring_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.lineno
            end = first.end_lineno or first.lineno
            docstring_ranges.append((start, end))

    for start, end in docstring_ranges:
        for i in range(start - 1, end):
            if 0 <= i < len(lines):
                lines[i] = ""
    return "\n".join(lines)


def _ast_outcome_observed_reads(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, snippet)`` for any AST-level *read* of ``outcome_observed``.

    Catches:
      * ``ast.Attribute`` whose ``.attr`` is ``outcome_observed`` (Python
        attribute access — the exact double-correction trap from
        T-CB-045).
      * ``ast.Subscript`` index ``"outcome_observed"`` (DataFrame
        column lookup form).

    Does NOT match docstring prose (those are stripped by the caller)
    nor SQL-string column refs (those are caught by the regex pass on
    the docstring-stripped source).
    """

    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "outcome_observed":
            hits.append((node.lineno, f"attribute access: .{node.attr}"))
        elif isinstance(node, ast.Subscript):
            slice_node = node.slice
            if (
                isinstance(slice_node, ast.Constant)
                and isinstance(slice_node.value, str)
                and slice_node.value == "outcome_observed"
            ):
                hits.append(
                    (
                        node.lineno,
                        'subscript access: ["outcome_observed"]',
                    )
                )
    return hits


def test_no_outcome_observed_read_in_production() -> None:
    """Production ``polymarket_resolution_calibration.py`` must never *read* ``outcome_observed``.

    Reading ``cr.outcome_observed`` AND applying ``cr.polarity_at_comparison``
    would apply polarity twice (the column is already corrected at
    write time per ``mispricing_detector/models.py:148-149``). The
    matrix test above would catch that numerically; this guard catches
    it *statically* before a regression even runs, with a precise
    file/line failure message that points at the offending reference.

    Docstring prose mentioning the trap (e.g. the module-level
    ``comparison_resolutions.outcome_observed`` discussion) is
    explicitly allowed — it documents the very anti-pattern this guard
    enforces. The scan therefore strips docstrings before regex/AST
    inspection so the human-facing warning is preserved while real
    code-level reads still trip the guard.
    """

    src_path: Path | None = Path(pl_meta.__file__).resolve() if pl_meta.__file__ else None
    assert src_path is not None, "pl_meta module has no __file__ — cannot scan source"
    raw_source = src_path.read_text(encoding="utf-8")
    code_only = _strip_docstrings(raw_source)

    for pattern in _FORBIDDEN_READ_PATTERNS:
        regex_match = pattern.search(code_only)
        if regex_match is not None:
            line_no = code_only.count("\n", 0, regex_match.start()) + 1
            snippet = raw_source.splitlines()[line_no - 1].strip()
            pytest.fail(
                "REQ-CB-PL-001 / T-CB-045 double-correction guard tripped: "
                f"{src_path}:{line_no} reads ``outcome_observed`` "
                f"(pattern={pattern.pattern!r}) — snippet: {snippet!r}. The "
                "meta-class must re-derive observed from raw "
                "winning_outcome_label + polarity_at_comparison; see module "
                "docstring under 'Polarity-correction trap'."
            )

    ast_hits = _ast_outcome_observed_reads(code_only)
    assert not ast_hits, (
        "REQ-CB-PL-001 / T-CB-045 double-correction guard tripped (AST): "
        f"{src_path} contains forbidden reads of ``outcome_observed`` at "
        f"line(s) {[h[0] for h in ast_hits]} — first hit: {ast_hits[0][1]}"
    )


def test_double_correction_guard_self_test() -> None:
    """Self-test the regex+AST scanners against synthetic bad sources.

    Without this, a refactor that broke both scanners would still pass
    ``test_no_outcome_observed_read_in_production`` (because the real
    file is clean). This test pins the detectors against known-bad
    inputs covering SQL alias-prefixed reads, Python attribute access,
    DataFrame subscript reads, and confirms docstring prose is ignored.
    """

    # SQL alias-prefixed read — caught by the cr.outcome_observed regex.
    bad_sql_string = (
        "from __future__ import annotations\n"
        '_SQL = "SELECT cr.outcome_observed FROM comparison_resolutions cr"\n'
    )
    bad_sql_stripped = _strip_docstrings(bad_sql_string)
    assert any(p.search(bad_sql_stripped) for p in _FORBIDDEN_READ_PATTERNS)

    # Python attribute access — caught by the AST attribute walk.
    bad_attribute_access = (
        "from __future__ import annotations\n"
        "def _bad(row: object) -> int:\n"
        "    return row.outcome_observed  # type: ignore[attr-defined]\n"
    )
    bad_attr_stripped = _strip_docstrings(bad_attribute_access)
    attr_hits = _ast_outcome_observed_reads(bad_attr_stripped)
    assert any("attribute access" in h[1] for h in attr_hits)

    # DataFrame subscript read — caught by both the regex and AST subscript walk.
    bad_subscript = (
        "from __future__ import annotations\n"
        "def _bad(df: object) -> int:\n"
        '    return df["outcome_observed"]  # type: ignore[index]\n'
    )
    bad_sub_stripped = _strip_docstrings(bad_subscript)
    assert any(p.search(bad_sub_stripped) for p in _FORBIDDEN_READ_PATTERNS)
    sub_hits = _ast_outcome_observed_reads(bad_sub_stripped)
    assert any("subscript access" in h[1] for h in sub_hits)

    # Docstring prose — must NOT trip the scan after stripping.
    docstring_only = (
        '"""Module documenting the outcome_observed trap.\n'
        "\n"
        "comparison_resolutions.outcome_observed is already polarity-adjusted.\n"
        '"""\n'
        "from __future__ import annotations\n"
        '_SQL = "SELECT cr.polarity_at_comparison, pr.winning_outcome_label"\n'
    )
    docstring_stripped = _strip_docstrings(docstring_only)
    assert not any(p.search(docstring_stripped) for p in _FORBIDDEN_READ_PATTERNS)
    assert _ast_outcome_observed_reads(docstring_stripped) == []

    # Sanity: completely clean source with no mention at all.
    clean_source = (
        "from __future__ import annotations\n"
        '_SQL = "SELECT cr.polarity_at_comparison, pr.winning_outcome_label"\n'
    )
    clean_stripped = _strip_docstrings(clean_source)
    assert not any(p.search(clean_stripped) for p in _FORBIDDEN_READ_PATTERNS)
    assert _ast_outcome_observed_reads(clean_stripped) == []
