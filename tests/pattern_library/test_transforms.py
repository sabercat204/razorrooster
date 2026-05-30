"""T-PL-040 — series transform tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from razor_rooster.pattern_library.transforms import (
    lag,
    percentile_rank,
    rolling_mean,
    zscore,
)

# -- zscore ---------------------------------------------------------------


def test_zscore_normalizes_series() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = zscore(series)
    assert result.mean() == pytest.approx(0.0, abs=1e-12)
    assert result.std(ddof=0) == pytest.approx(1.0, rel=1e-6)


def test_zscore_constant_series_returns_zeros() -> None:
    series = pd.Series([3.0, 3.0, 3.0])
    result = zscore(series)
    assert (result == 0.0).all()


def test_zscore_empty_series_returns_empty() -> None:
    series = pd.Series([], dtype=float)
    result = zscore(series)
    assert result.empty


def test_zscore_all_nan_returns_input_unchanged() -> None:
    series = pd.Series([np.nan, np.nan, np.nan])
    result = zscore(series)
    assert result.isna().all()


def test_zscore_handles_mixed_nan_and_values() -> None:
    series = pd.Series([1.0, 2.0, np.nan, 4.0])
    result = zscore(series)
    assert not result.isna().all()
    # Mean of valid values should be near zero.
    assert result.dropna().mean() == pytest.approx(0.0, abs=1e-10)


# -- percentile_rank ------------------------------------------------------


def test_percentile_rank_basic_ranks() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = percentile_rank(series, window=5)
    # Position 4 has the full window [1..5]; rank of 5 is 5/5 = 1.0
    assert result.iloc[4] == pytest.approx(1.0)
    # Earlier positions don't have a full window → NaN.
    assert result.iloc[0:4].isna().all()


def test_percentile_rank_identifies_top_value() -> None:
    series = pd.Series([3.0, 1.0, 4.0, 1.0, 5.0])
    result = percentile_rank(series, window=5)
    # 5 is the largest of 5 → rank 5/5 = 1.0
    assert result.iloc[4] == pytest.approx(1.0)


def test_percentile_rank_partial_window_returns_nan() -> None:
    series = pd.Series([1.0, 2.0, 3.0])
    result = percentile_rank(series, window=5)
    assert result.isna().all()


def test_percentile_rank_handles_nan_in_window() -> None:
    """A NaN inside the window still requires full count of valid values."""
    series = pd.Series([1.0, np.nan, 3.0, 4.0, 5.0])
    result = percentile_rank(series, window=5)
    # Only 4 valid values < window of 5 → all NaN.
    assert result.isna().all()


def test_percentile_rank_rejects_zero_window() -> None:
    series = pd.Series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="window"):
        percentile_rank(series, window=0)


def test_percentile_rank_empty_series_returns_empty() -> None:
    series = pd.Series([], dtype=float)
    result = percentile_rank(series, window=3)
    assert result.empty


# -- lag ------------------------------------------------------------------


def test_lag_one_step() -> None:
    series = pd.Series([10.0, 20.0, 30.0])
    result = lag(series, 1)
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 10.0
    assert result.iloc[2] == 20.0


def test_lag_zero_returns_copy() -> None:
    series = pd.Series([10.0, 20.0])
    result = lag(series, 0)
    assert (result == series).all()
    # Modifying the returned series should not affect the input.
    result.iloc[0] = 999.0
    assert series.iloc[0] == 10.0


def test_lag_rejects_negative_n() -> None:
    series = pd.Series([1.0])
    with pytest.raises(ValueError, match="lag"):
        lag(series, -1)


def test_lag_empty_series_returns_empty() -> None:
    series = pd.Series([], dtype=float)
    result = lag(series, 1)
    assert result.empty


# -- rolling_mean ---------------------------------------------------------


def test_rolling_mean_basic() -> None:
    series = pd.Series([1.0, 2.0, 3.0, 4.0])
    result = rolling_mean(series, window=2)
    # Position 0 has only one value < window → NaN
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == pytest.approx(1.5)
    assert result.iloc[2] == pytest.approx(2.5)
    assert result.iloc[3] == pytest.approx(3.5)


def test_rolling_mean_full_window_gives_full_mean() -> None:
    series = pd.Series([1.0, 1.0, 1.0])
    result = rolling_mean(series, window=3)
    assert result.iloc[2] == pytest.approx(1.0)


def test_rolling_mean_rejects_zero_window() -> None:
    series = pd.Series([1.0])
    with pytest.raises(ValueError, match="window"):
        rolling_mean(series, window=0)


def test_rolling_mean_partial_data_returns_nan() -> None:
    series = pd.Series([1.0, 2.0])
    result = rolling_mean(series, window=5)
    assert result.isna().all()


def test_rolling_mean_empty_returns_empty() -> None:
    series = pd.Series([], dtype=float)
    result = rolling_mean(series, window=3)
    assert result.empty
