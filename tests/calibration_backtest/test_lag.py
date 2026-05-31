"""T-CB-016 — lag-enforcement helpers tests.

Covers :func:`derive_prediction_ts` and :func:`validate_lag` from
:mod:`razor_rooster.calibration_backtest.engines.lag`:

* Default 7-day floor rejects a 3-day gap (insufficient lag).
* ``--lag-days 1`` admits the same 3-day gap.
* Boundary: an exact ``lag_days``-day gap is admitted (``>=`` not ``>``).
* :func:`derive_prediction_ts` subtracts the day delta correctly across
  timezone-aware datetimes (and preserves the originating tzinfo).
* Defensive validation: ``lag_days < 1`` raises
  :class:`BacktestConfigError` from both helpers.
* Module re-exports its public helpers via ``__all__``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from razor_rooster.calibration_backtest.engines import lag as lag_module
from razor_rooster.calibration_backtest.engines.lag import (
    derive_prediction_ts,
    validate_lag,
)
from razor_rooster.calibration_backtest.errors import BacktestConfigError

# -- helpers ---------------------------------------------------------------

_RESOLUTION_TS = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_DEFAULT_LAG_DAYS = 7


# -- validate_lag ---------------------------------------------------------


def test_validate_lag_rejects_three_day_gap_at_default_seven_day_floor() -> None:
    """3-day lag is rejected at the default 7-day setting (REQ-CB-FREEZE-002)."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=3)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, _DEFAULT_LAG_DAYS) is False


def test_validate_lag_admits_three_day_gap_when_lag_days_is_one() -> None:
    """3-day lag is admitted when ``--lag-days 1`` is passed (REQ-CB-FREEZE-002)."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=3)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, 1) is True


def test_validate_lag_admits_exact_boundary_equality() -> None:
    """Boundary: ``(resolution_ts - prediction_ts).days == lag_days`` is admitted."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=_DEFAULT_LAG_DAYS)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, _DEFAULT_LAG_DAYS) is True


def test_validate_lag_rejects_one_second_under_boundary() -> None:
    """A gap of ``lag_days`` days minus one second falls one ``.days`` short."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=_DEFAULT_LAG_DAYS) + timedelta(seconds=1)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, _DEFAULT_LAG_DAYS) is False


def test_validate_lag_admits_gap_strictly_greater_than_floor() -> None:
    """A gap strictly greater than ``lag_days`` is admitted."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=_DEFAULT_LAG_DAYS + 5)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, _DEFAULT_LAG_DAYS) is True


@pytest.mark.parametrize("invalid_lag_days", [0, -1, -7])
def test_validate_lag_raises_when_lag_days_below_floor(invalid_lag_days: int) -> None:
    """``lag_days < 1`` defensively raises :class:`BacktestConfigError`."""
    prediction_ts = _RESOLUTION_TS - timedelta(days=10)
    with pytest.raises(BacktestConfigError, match="lag_days must be >= 1"):
        validate_lag(_RESOLUTION_TS, prediction_ts, invalid_lag_days)


# -- derive_prediction_ts -------------------------------------------------


def test_derive_prediction_ts_subtracts_correctly_with_utc_aware_datetime() -> None:
    """Default 7-day floor yields ``resolution_ts - 7 days`` exactly."""
    prediction_ts = derive_prediction_ts(_RESOLUTION_TS, _DEFAULT_LAG_DAYS)
    assert prediction_ts == datetime(2024, 6, 8, 12, 0, 0, tzinfo=UTC)
    assert prediction_ts.tzinfo == UTC


def test_derive_prediction_ts_preserves_non_utc_tzinfo() -> None:
    """Subtraction preserves the originating tzinfo across day boundaries."""
    pacific = timezone(timedelta(hours=-8), name="PST")
    resolution_ts = datetime(2024, 6, 15, 23, 30, 0, tzinfo=pacific)
    prediction_ts = derive_prediction_ts(resolution_ts, 7)
    assert prediction_ts == datetime(2024, 6, 8, 23, 30, 0, tzinfo=pacific)
    assert prediction_ts.tzinfo == pacific


def test_derive_prediction_ts_with_lag_days_one_returns_one_day_earlier() -> None:
    """``lag_days=1`` minimum honours the ``minimum_lag_days`` floor (design §3.5)."""
    prediction_ts = derive_prediction_ts(_RESOLUTION_TS, 1)
    assert prediction_ts == datetime(2024, 6, 14, 12, 0, 0, tzinfo=UTC)


def test_derive_prediction_ts_round_trip_with_validate_lag() -> None:
    """``derive_prediction_ts`` output passes ``validate_lag`` at the same floor."""
    prediction_ts = derive_prediction_ts(_RESOLUTION_TS, _DEFAULT_LAG_DAYS)
    assert validate_lag(_RESOLUTION_TS, prediction_ts, _DEFAULT_LAG_DAYS) is True


@pytest.mark.parametrize("invalid_lag_days", [0, -1, -7])
def test_derive_prediction_ts_raises_when_lag_days_below_floor(
    invalid_lag_days: int,
) -> None:
    """``lag_days < 1`` defensively raises :class:`BacktestConfigError`."""
    with pytest.raises(BacktestConfigError, match="lag_days must be >= 1"):
        derive_prediction_ts(_RESOLUTION_TS, invalid_lag_days)


# -- module surface --------------------------------------------------------


def test_lag_module_all_lists_public_helpers() -> None:
    """Module ``__all__`` advertises the two public helpers."""
    assert set(lag_module.__all__) == {"derive_prediction_ts", "validate_lag"}
