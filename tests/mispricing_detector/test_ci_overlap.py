"""T-MD-031 — CI overlap analysis tests."""

from __future__ import annotations

from razor_rooster.mispricing_detector.engines.ci_overlap import check_ci_overlap


def test_clear_overlap() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.30,
            model_ci_upper=0.50,
            market_bid=0.40,
            market_ask=0.45,
        )
        is True
    )


def test_no_overlap_model_above() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.50,
            model_ci_upper=0.70,
            market_bid=0.20,
            market_ask=0.30,
        )
        is False
    )


def test_no_overlap_model_below() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.10,
            model_ci_upper=0.20,
            market_bid=0.30,
            market_ask=0.40,
        )
        is False
    )


def test_touching_intervals() -> None:
    """Intervals that touch at an endpoint count as overlapping."""
    assert (
        check_ci_overlap(
            model_ci_lower=0.30,
            model_ci_upper=0.40,
            market_bid=0.40,
            market_ask=0.50,
        )
        is True
    )


def test_model_ci_inside_market_range() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.42,
            model_ci_upper=0.45,
            market_bid=0.40,
            market_ask=0.50,
        )
        is True
    )


def test_market_range_inside_model_ci() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.30,
            model_ci_upper=0.50,
            market_bid=0.40,
            market_ask=0.42,
        )
        is True
    )


def test_null_bid_returns_false() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.30,
            model_ci_upper=0.50,
            market_bid=None,
            market_ask=0.45,
        )
        is False
    )


def test_null_ask_returns_false() -> None:
    assert (
        check_ci_overlap(
            model_ci_lower=0.30,
            model_ci_upper=0.50,
            market_bid=0.40,
            market_ask=None,
        )
        is False
    )


def test_swapped_bid_ask_treated_as_range() -> None:
    """If bid > ask (degenerate), function treats them as a range."""
    assert (
        check_ci_overlap(
            model_ci_lower=0.42,
            model_ci_upper=0.45,
            market_bid=0.50,
            market_ask=0.40,
        )
        is True
    )


def test_degenerate_model_ci_returns_false() -> None:
    """Inverted model CI is treated as non-overlapping (defensive default)."""
    assert (
        check_ci_overlap(
            model_ci_lower=0.50,
            model_ci_upper=0.30,
            market_bid=0.40,
            market_ask=0.45,
        )
        is False
    )
