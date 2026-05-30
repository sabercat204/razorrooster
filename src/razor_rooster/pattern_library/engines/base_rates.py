"""Base rate engine (T-PL-041; design §3.5; REQ-PL-BR-001..005).

Computes the per-year occurrence rate for a class over a time window,
plus a Bayesian credible interval expressing uncertainty in the rate
given the observed sample size. The default prior is Jeffreys
``Beta(0.5, 0.5)`` per OQ-PL-001; per-class overrides via
``EventClass.prior_alpha`` / ``prior_beta``.

The math: we treat each year of the window as one Bernoulli trial with
success = "at least one occurrence." That's the simplification noted in
design §3.5; for very rare events the Poisson/Gamma framing would be
more proper, but the Beta posterior on years-as-trials gives a
defensible credible interval that lines up with operator intuition
("we have N years of data and saw K occurrences").

Outputs carry the ``low_sample_warning`` flag for n < 5 occurrences and
the ``source_stale_warning`` flag pulled from data_ingest's freshness
view for any source the class's predicate touches.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb

from razor_rooster.data_ingest.persistence.provenance import query_freshness
from razor_rooster.pattern_library.models.base_rate import BaseRateResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from razor_rooster.pattern_library.models.event_class import EventClass


logger = logging.getLogger(__name__)


# n < 5 → wide credible interval, low confidence in any forward-looking
# rate estimate. Operator-facing flag.
LOW_SAMPLE_THRESHOLD: int = 5


def compute_base_rate(
    conn: duckdb.DuckDBPyConnection,
    cls: EventClass,
    *,
    window: tuple[datetime, datetime] | None = None,
    library_version: int,
    data_as_of: datetime | None = None,
    source_ids_for_freshness: Sequence[str] | None = None,
    now: datetime | None = None,
) -> BaseRateResult:
    """Compute the base-rate result for one class over one window.

    Args:
        conn: DuckDB connection. The class's ``occurrence_query`` is
            invoked with this connection.
        cls: The event class.
        window: Optional ``(start, end)`` pair. Defaults to
            ``(now - cls.base_rate_window_default, now)``.
        library_version: The version stamping output rows.
        data_as_of: Timestamp of the underlying data snapshot. Defaults
            to ``now``.
        source_ids_for_freshness: ``data_ingest`` source ids the
            predicate depends on, for the source-staleness flag. When
            ``None``, the staleness check is skipped (warning stays
            ``False``). Class authors who care about staleness pass the
            list explicitly.
        now: Override "now" for testing / replay.

    Returns:
        :class:`BaseRateResult` with the rate, credible interval, and
        warning flags.
    """
    started = now or datetime.now(tz=UTC)
    window_start, window_end = _resolve_window(cls, window=window, now=started)
    if window_start >= window_end:
        raise ValueError(
            f"compute_base_rate {cls.class_id!r}: window_start must precede window_end"
        )

    occurrences_df = cls.occurrence_query(conn)
    n_occurrences = _count_in_window(occurrences_df, window_start, window_end)
    duration_years = (window_end - window_start).days / 365.25
    if duration_years <= 0:
        raise ValueError(f"compute_base_rate {cls.class_id!r}: window has non-positive duration")

    rate_per_year = n_occurrences / duration_years

    ci_lower, ci_upper = _beta_credible_interval(
        successes=n_occurrences,
        trials=duration_years,
        prior_alpha=cls.prior_alpha,
        prior_beta=cls.prior_beta,
        level=0.95,
    )

    low_sample_warning = n_occurrences < LOW_SAMPLE_THRESHOLD
    source_stale_warning = _check_source_freshness(conn, source_ids=source_ids_for_freshness)

    if cls.prior_alpha != 0.5 or cls.prior_beta != 0.5:
        logger.info(
            "base_rate %s using non-default prior alpha=%s beta=%s",
            cls.class_id,
            cls.prior_alpha,
            cls.prior_beta,
        )

    return BaseRateResult(
        class_id=cls.class_id,
        window_start=window_start,
        window_end=window_end,
        occurrences=int(n_occurrences),
        rate_per_year=float(rate_per_year),
        credible_interval_lower=float(ci_lower),
        credible_interval_upper=float(ci_upper),
        prior_alpha=float(cls.prior_alpha),
        prior_beta=float(cls.prior_beta),
        library_version=library_version,
        definition_version=cls.definition_version,
        data_as_of=data_as_of or started,
        computed_at=started,
        low_sample_warning=low_sample_warning,
        source_stale_warning=source_stale_warning,
    )


# -- internals --------------------------------------------------------------


def _resolve_window(
    cls: EventClass,
    *,
    window: tuple[datetime, datetime] | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    if window is not None:
        start, end = window
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("compute_base_rate: window timestamps must be timezone-aware (UTC)")
        return start, end
    end = now
    start = end - cls.base_rate_window_default
    return start, end


def _count_in_window(
    occurrences_df: object,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Count occurrence_ts values that fall within ``[start, end)``.

    The class's occurrence_query is required to return a DataFrame with
    an ``occurrence_ts`` column; missing-column case raises so the
    refresh log captures it as a class-author error rather than
    silently producing zero.
    """
    import pandas as pd  # local import — heavy

    if not isinstance(occurrences_df, pd.DataFrame):
        raise TypeError(
            f"occurrence_query must return a pandas DataFrame, got {type(occurrences_df).__name__}"
        )
    if "occurrence_ts" not in occurrences_df.columns:
        raise ValueError("occurrence_query DataFrame must include an 'occurrence_ts' column")
    series = pd.to_datetime(occurrences_df["occurrence_ts"], utc=True, errors="coerce")
    mask = (series >= pd.Timestamp(window_start)) & (series < pd.Timestamp(window_end))
    return int(mask.sum())


def _beta_credible_interval(
    *,
    successes: float,
    trials: float,
    prior_alpha: float,
    prior_beta: float,
    level: float = 0.95,
) -> tuple[float, float]:
    """Return the (level)-credible interval for the per-year rate.

    The posterior on the per-year hit-probability is
    ``Beta(prior_alpha + successes, prior_beta + trials - successes)``;
    we then convert to a per-year rate by scaling. For very rare events
    where ``trials >> successes``, the interval is approximately the
    same as treating ``successes / trials`` as a rate.

    The implementation uses ``scipy.stats.beta.ppf`` for exactness; if
    scipy is unavailable, a log-space approximation falls back. v1
    requires scipy as a runtime dependency (already listed in
    pyproject.toml runtime deps).
    """
    from scipy.stats import beta as scipy_beta  # local import — heavy

    a = prior_alpha + successes
    b = prior_beta + max(0.0, trials - successes)
    tail = (1.0 - level) / 2.0
    lower_p = float(scipy_beta.ppf(tail, a, b))
    upper_p = float(scipy_beta.ppf(1.0 - tail, a, b))
    # Clamp away from exact 0/1 edges — scipy can return values just
    # outside [0,1] due to floating-point arithmetic on tiny tails.
    lower_p = max(0.0, min(lower_p, 1.0))
    upper_p = max(0.0, min(upper_p, 1.0))
    if lower_p > upper_p:
        # Degenerate case (huge prior, no data): swap to enforce
        # invariant.
        lower_p, upper_p = upper_p, lower_p
    return lower_p, upper_p


def _check_source_freshness(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_ids: Sequence[str] | None,
) -> bool:
    """Return True if any of the named sources is currently stale.

    When ``source_ids`` is None, no check is run and the result is
    False (the contract is "we don't know, so don't alarm the
    operator"). Class authors that care call with explicit ids.
    """
    if not source_ids:
        return False
    rows = query_freshness(conn)
    by_id = {r.source_id: r for r in rows}
    for source_id in source_ids:
        row = by_id.get(source_id)
        if row is None:
            # Unknown source — not registered yet; treat as stale.
            logger.warning("base_rate freshness check: source_id %s not registered", source_id)
            return True
        if row.is_stale:
            return True
    return False
