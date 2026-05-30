"""T-PE-033 — invalidation-criteria extraction tests."""

from __future__ import annotations

from razor_rooster.position_engine.engines.invalidation import extract_criteria


def _scanner_trace() -> dict[str, object]:
    return {
        "warnings": [],
        "precursors": [
            {
                "variable_id": "v1",
                "title": "First precursor",
                "current_value": 8.0,
                "threshold": 5.0,
                "direction": "high_signals_event",
                "fired": True,
            },
            {
                "variable_id": "v2",
                "title": "Second precursor",
                "current_value": 1.0,
                "threshold": 3.0,
                "direction": "high_signals_event",
                "fired": False,
            },
        ],
    }


def test_extracts_precursor_shift_for_fired() -> None:
    criteria = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=0.10,
    )
    fired_v1 = [
        c for c in criteria if c.get("type") == "precursor_shift" and c.get("variable_id") == "v1"
    ]
    assert len(fired_v1) == 1
    assert "drops back below" in fired_v1[0]["description"]


def test_extracts_precursor_shift_for_not_fired() -> None:
    criteria = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=0.10,
    )
    not_fired_v2 = [
        c for c in criteria if c.get("type") == "precursor_shift" and c.get("variable_id") == "v2"
    ]
    assert len(not_fired_v2) == 1
    assert "crosses above" in not_fired_v2[0]["description"]


def test_extracts_market_move_two_sided() -> None:
    criteria = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=0.10,
    )
    market_moves = [c for c in criteria if c.get("type") == "market_move"]
    # Two-sided: fall + rise.
    assert len(market_moves) == 2
    directions = {c["direction"] for c in market_moves}
    assert "market_p_falls_to" in directions
    assert "market_p_rises_to" in directions


def test_no_market_criteria_when_market_p_missing() -> None:
    criteria = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=None,
    )
    market_moves = [c for c in criteria if c.get("type") == "market_move"]
    assert market_moves == []


def test_no_market_criteria_at_market_boundaries() -> None:
    """market_p = 0 or 1 means no useful market_move criterion."""
    criteria_zero = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=0.0,
    )
    criteria_one = extract_criteria(
        scanner_trace=_scanner_trace(),
        model_probability=0.30,
        market_probability=1.0,
    )
    assert all(c.get("type") != "market_move" for c in criteria_zero)
    assert all(c.get("type") != "market_move" for c in criteria_one)


def test_general_caveats_for_low_confidence() -> None:
    trace = _scanner_trace()
    trace["warnings"] = ["low_confidence_signatures"]
    criteria = extract_criteria(
        scanner_trace=trace,
        model_probability=0.30,
        market_probability=0.10,
    )
    caveats = [c for c in criteria if c.get("type") == "general_caveat"]
    assert any("confidence is low" in c["description"] for c in caveats)


def test_general_caveat_for_library_stale() -> None:
    trace = _scanner_trace()
    trace["warnings"] = ["library_stale_warning"]
    criteria = extract_criteria(
        scanner_trace=trace,
        model_probability=0.30,
        market_probability=0.10,
    )
    caveats = [c for c in criteria if c.get("type") == "general_caveat"]
    assert any("stale" in c["description"] for c in caveats)


def test_no_scanner_trace_returns_market_only() -> None:
    criteria = extract_criteria(
        scanner_trace=None,
        model_probability=0.30,
        market_probability=0.10,
    )
    # Only market_move criteria; no precursor_shift since no trace.
    assert all(c.get("type") != "precursor_shift" for c in criteria)
    assert any(c.get("type") == "market_move" for c in criteria)
