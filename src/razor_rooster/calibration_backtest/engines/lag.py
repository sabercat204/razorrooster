"""Lag enforcement and prediction-timestamp derivation (T-CB-016).

Implements REQ-CB-FREEZE-002 and design §3.5: every replayed prediction
would simulate a decision instant ``lag_days`` before the market
resolution settles. The two helpers in this module are pure, dependency
free, and intentionally housed apart from
:mod:`razor_rooster.calibration_backtest.engines.freezer` (the
``source_publication_ts`` freezer) so the lag rule can be exercised in
isolation by the replay loop and unit tests.

* :func:`derive_prediction_ts` — given a market resolution timestamp and
  the configured ``lag_days`` floor, returns the simulated decision
  instant ``resolution_ts - timedelta(days=lag_days)`` (design §3.5
  "Prediction timestamp derivation"). The replay loop calls this once
  per resolution before invoking the freezer.
* :func:`validate_lag` — returns ``True`` when the prediction-to-
  resolution gap honours the floor (``>= lag_days``). Used by the
  replay loop's lag-validation gate (design §3.13): when the predicate
  returns ``False``, the prediction is skipped with
  ``skip_reason='insufficient_lag'``. Boundary equality is admitted —
  an exact ``lag_days``-day gap passes; only strictly shorter gaps are
  rejected.

Both helpers operate on timezone-aware :class:`datetime.datetime`
instances; mixing aware and naive timestamps raises
:class:`TypeError` from the underlying ``datetime`` arithmetic per the
standard library, surfacing the configuration error to the caller.

The ``lag_days`` argument flows from the CLI through
:class:`razor_rooster.calibration_backtest.models.RunParameters` into
the replay loop (design §3.5); validation of the ``>= 1`` floor is
enforced at parameter-construction time, so this module assumes a
positive integer and raises
:class:`razor_rooster.calibration_backtest.errors.BacktestConfigError`
defensively if the contract is violated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from razor_rooster.calibration_backtest.errors import BacktestConfigError


def derive_prediction_ts(resolution_ts: datetime, lag_days: int) -> datetime:
    """Return the simulated decision instant for a market resolution.

    Per design §3.5 "Prediction timestamp derivation" (REQ-CB-FREEZE-002),
    the replay loop simulates a decision exactly ``lag_days`` before the
    market resolution settles: ``prediction_ts = resolution_ts -
    timedelta(days=lag_days)``. Precursor data is then frozen at that
    instant via :func:`freezer.freeze`; data published exactly at
    ``prediction_ts`` is admitted (boundary equality), data at
    ``prediction_ts + 1ns`` is excluded.

    :param resolution_ts: Timezone-aware market resolution timestamp.
    :param lag_days: Lag floor in days; must be ``>= 1`` per the
        ``minimum_lag_days`` floor (design §3.5 config-defaults).
    :returns: ``resolution_ts - timedelta(days=lag_days)``.
    :raises BacktestConfigError: If ``lag_days < 1``.
    """
    if lag_days < 1:
        raise BacktestConfigError(f"derive_prediction_ts: lag_days must be >= 1, got {lag_days!r}")
    return resolution_ts - timedelta(days=lag_days)


def validate_lag(resolution_ts: datetime, prediction_ts: datetime, lag_days: int) -> bool:
    """Return ``True`` when the prediction-to-resolution gap honours the lag floor.

    Implements the lag-validation gate from design §3.5: the replay loop
    skips with ``skip_reason='insufficient_lag'`` when this predicate
    returns ``False``. Boundary equality is admitted — a gap of exactly
    ``lag_days`` days passes; only strictly shorter gaps are rejected
    (REQ-CB-FREEZE-002).

    :param resolution_ts: Timezone-aware market resolution timestamp.
    :param prediction_ts: Timezone-aware simulated decision instant.
    :param lag_days: Lag floor in days; must be ``>= 1``.
    :returns: ``True`` if ``(resolution_ts - prediction_ts).days >=
        lag_days``, otherwise ``False``.
    :raises BacktestConfigError: If ``lag_days < 1``.
    """
    if lag_days < 1:
        raise BacktestConfigError(f"validate_lag: lag_days must be >= 1, got {lag_days!r}")
    return (resolution_ts - prediction_ts).days >= lag_days


__all__ = [
    "derive_prediction_ts",
    "validate_lag",
]
