"""Series-transformation helpers for class authors (T-PL-040; OQ-PL-004).

Per OQ-PL-004, feature engineering lives inside class-author queries.
This module provides a small set of pure transforms that class queries
can layer on top of raw ``pd.Series`` inputs:

- :func:`zscore` — population z-score normalization.
- :func:`percentile_rank` — rolling-window percentile rank.
- :func:`lag` — shift values forward by ``n`` positions.
- :func:`rolling_mean` — rolling-window mean.

Every transform is a pure function returning a fresh ``pd.Series``;
NaN handling is explicit and conservative (insufficient data → NaN at
the corresponding index, never a silent zero).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def zscore(series: pd.Series) -> pd.Series:
    """Return ``(series - mean) / std`` using the population's stats.

    All-NaN or empty input returns the input unchanged. A constant
    series (std == 0) returns all zeros to avoid division by zero — the
    z-score of a constant relative to itself is well-defined as 0 and
    callers downstream treat 0 as "at the mean."
    """
    if series.empty:
        return series.copy()
    arr = series.to_numpy(dtype=float, na_value=np.nan)
    mask = ~np.isnan(arr)
    if not mask.any():
        return series.copy()
    mean = float(np.nanmean(arr))
    std = float(np.nanstd(arr))
    if std == 0.0:
        return pd.Series(np.zeros_like(arr), index=series.index, dtype=float)
    out = (arr - mean) / std
    return pd.Series(out, index=series.index, dtype=float)


def percentile_rank(series: pd.Series, window: int) -> pd.Series:
    """Return the rolling-window percentile rank of each value.

    The rank at position ``i`` is the fraction of values in
    ``series[i - window + 1 : i + 1]`` that are <= ``series[i]``,
    expressed in [0, 1]. Positions with insufficient history return
    NaN.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window!r}")
    if series.empty:
        return series.copy()

    arr = series.to_numpy(dtype=float, na_value=np.nan)
    out = np.full_like(arr, np.nan, dtype=float)
    n = arr.shape[0]
    for i in range(n):
        start = max(0, i - window + 1)
        window_slice = arr[start : i + 1]
        valid = window_slice[~np.isnan(window_slice)]
        # Need a full window's worth of valid data to emit a rank.
        if valid.size < window or np.isnan(arr[i]):
            continue
        # Rank of arr[i] among the window's valid values.
        rank = float((valid <= arr[i]).sum())
        out[i] = rank / float(window)
    return pd.Series(out, index=series.index, dtype=float)


def lag(series: pd.Series, n: int) -> pd.Series:
    """Shift the series forward by ``n`` positions.

    ``lag(s, 1)`` returns a series where index ``i`` holds the value
    that was at index ``i - 1`` of ``s``; the first ``n`` positions
    become NaN. Negative ``n`` is rejected; use a different transform
    if you need lookahead.
    """
    if n < 0:
        raise ValueError(f"lag(n) requires n >= 0, got {n!r}")
    if series.empty or n == 0:
        return series.copy()
    return series.shift(n)


def rolling_mean(series: pd.Series, window: int) -> pd.Series:
    """Return the rolling-window mean of ``series``.

    Implementation uses ``pd.Series.rolling`` with
    ``min_periods=window`` so positions with fewer than ``window``
    valid values yield NaN. This matches ``percentile_rank``'s
    convention of refusing to emit a value when the window isn't
    yet full.
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window!r}")
    if series.empty:
        return series.copy()
    return series.rolling(window=window, min_periods=window).mean()
