"""T-CB-017 — evaluate_class_at_frozen_time orchestration wrapper tests.

The wrapper threads ``prediction_ts`` into
``signal_scanner.engines.posterior.evaluate_precursors_at_time`` as
``as_of_ts`` (verifying the time-honesty contract is preserved across
the calibration_backtest -> signal_scanner boundary), and forwards
``current_values`` plus the loaded ``base_rate``/``signatures`` into
``posterior_with_ci`` to produce the ``model_p`` scalar plus a JSON-
serialisable trace dict.

These tests use mocks (rather than a live DuckDB store) for two
reasons:

* The wrapper's job is **routing** — passing the correct kwargs to the
  scanner's public posterior pipeline. A live store would test
  signal_scanner's internals (already covered in
  ``tests/signal_scanner/test_evaluate_precursors_at_time.py``).
* The contract test that locks in non-divergence between the live-scan
  and backtest paths lives in the signal_scanner test module too, so
  we don't duplicate that coverage here.

The mocks replace the symbols **bound inside the replay module** (the
``from ... import ...`` rebinding pattern means patching the source
modules would not intercept the wrapper's calls; monkeypatching the
replay module's namespace is the only correct seam).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_MIN_SUPPORT,
    evaluate_class_at_frozen_time,
)
from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    InsufficientPrecursorData,
)
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
    ThresholdMethod,
)
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines.posterior import PosteriorResult

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_PREDICTION_TS = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
_DATA_AS_OF = datetime(2025, 6, 14, 0, 0, 0, tzinfo=UTC)


def _frozen_state(prediction_ts: datetime = _PREDICTION_TS) -> FrozenState:
    """Construct a successfully-frozen state for the wrapper's freezer arg."""
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _occurrence_query_stub(_conn: Any) -> Any:
    """Placeholder occurrence_query for synthesised event classes.

    The wrapper never calls this — :func:`evaluate_class_at_frozen_time`
    only reads metadata and forwards to the scanner — but EventClass's
    ``__post_init__`` requires the field to be a callable.
    """
    raise AssertionError("occurrence_query should not be called by the wrapper")


def _make_event_class(
    *,
    class_id: str = "test_class",
    precursors: tuple[PrecursorVariable, ...] = (),
) -> EventClass:
    return EventClass(
        class_id=class_id,
        title="Test Class",
        description="Synthesised class for replay-wrapper tests.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrence_query_stub,
        precursors=precursors,
    )


def _make_precursor(variable_id: str) -> PrecursorVariable:
    return PrecursorVariable(
        variable_id=variable_id,
        title=f"Title for {variable_id}",
        query=_occurrence_query_stub,
        direction="high_signals_event",
        threshold_method=ThresholdMethod.MANUAL,
        manual_threshold=1.0,
    )


def _make_base_rate(
    *,
    class_id: str = "test_class",
    library_version: int = 7,
    definition_version: int = 1,
) -> BaseRateResult:
    window_end = datetime(2025, 6, 1, tzinfo=UTC)
    return BaseRateResult(
        class_id=class_id,
        window_start=window_end - timedelta(days=365),
        window_end=window_end,
        occurrences=10,
        rate_per_year=0.2,
        credible_interval_lower=0.1,
        credible_interval_upper=0.3,
        prior_alpha=0.5,
        prior_beta=0.5,
        library_version=library_version,
        definition_version=definition_version,
        data_as_of=_DATA_AS_OF,
        computed_at=_DATA_AS_OF,
    )


def _make_signature(
    variable_id: str,
    *,
    class_id: str = "test_class",
    library_version: int = 7,
    definition_version: int = 1,
) -> SignatureResult:
    return SignatureResult(
        class_id=class_id,
        variable_id=variable_id,
        library_version=library_version,
        definition_version=definition_version,
        threshold_method=ThresholdMethod.MANUAL.value,
        threshold_value=1.0,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=2.0,
        pre_event_p25=1.5,
        pre_event_p50=2.0,
        pre_event_p75=2.5,
        baseline_mean=0.5,
        baseline_p25=0.2,
        baseline_p50=0.5,
        baseline_p75=0.8,
        hit_rate=0.6,
        false_positive_rate=0.1,
        sample_size_events=20,
        sample_size_baseline=200,
        confidence_score=0.8,
        computed_at=_DATA_AS_OF,
        low_confidence_warning=False,
    )


def _make_posterior(point: float = 0.42) -> PosteriorResult:
    return PosteriorResult(
        posterior=point,
        posterior_ci_lower=max(point - 0.1, 0.0),
        posterior_ci_upper=min(point + 0.1, 1.0),
        log_odds_shift=0.5,
        n_samples=1000,
        fired_count=1,
        likelihood_ratios=(6.0,),
        co_occurrence_correction=0.0,
    )


@dataclass
class _CallRecorder:
    """Container for captured kwargs on the patched scanner entry points."""

    evaluate_calls: list[dict[str, Any]] = field(default_factory=list)
    posterior_calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def recorder() -> _CallRecorder:
    return _CallRecorder()


@pytest.fixture
def store() -> object:
    """Sentinel store object — the wrapper passes it through unchanged."""
    return object()


@pytest.fixture
def patched_pattern_library(monkeypatch: pytest.MonkeyPatch) -> EventClass:
    """Patch registry + library facade to return synthesised artefacts."""
    precursor = _make_precursor("var_a")
    cls = _make_event_class(precursors=(precursor,))
    base_rate = _make_base_rate()
    signatures: tuple[SignatureResult, ...] = (_make_signature("var_a"),)

    monkeypatch.setattr(
        replay_module.registry,
        "is_registered",
        lambda class_id: class_id == cls.class_id,
    )
    monkeypatch.setattr(
        replay_module.registry,
        "get",
        lambda class_id: cls,
    )
    monkeypatch.setattr(
        replay_module.library,
        "base_rate",
        lambda store, class_id, *, library_version=None: base_rate,
    )
    monkeypatch.setattr(
        replay_module.library,
        "signature",
        lambda store, class_id, *, library_version=None: signatures,
    )
    return cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_wrapper_passes_prediction_ts_as_as_of_ts(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _CallRecorder,
    store: object,
    patched_pattern_library: EventClass,
) -> None:
    """The replay wrapper must forward ``prediction_ts`` as the scanner's ``as_of_ts``.

    This is the central time-honesty contract: a backtest replay at
    ``prediction_ts`` must instruct the scanner to read precursor data
    only up to that instant. Verifying the kwarg passes through is the
    smallest faithful unit test of T-CB-017's freeze contract.
    """

    def fake_evaluate(
        store_arg: object,
        cls_arg: EventClass,
        signatures_arg: Sequence[SignatureResult],
        as_of_ts: datetime,
    ) -> tuple[Mapping[str, float | None], bool]:
        recorder.evaluate_calls.append(
            {
                "store": store_arg,
                "cls": cls_arg,
                "signatures": tuple(signatures_arg),
                "as_of_ts": as_of_ts,
            }
        )
        return {"var_a": 1.5}, False

    def fake_posterior(
        base_rate: BaseRateResult,
        signatures: Sequence[SignatureResult],
        *,
        current_values: Mapping[str, float | None],
        co_occurrence_correction: float = 0.0,
        n_samples: int | None = None,
    ) -> PosteriorResult:
        recorder.posterior_calls.append(
            {
                "base_rate": base_rate,
                "signatures": tuple(signatures),
                "current_values": dict(current_values),
                "co_occurrence_correction": co_occurrence_correction,
                "n_samples": n_samples,
            }
        )
        return _make_posterior(point=0.55)

    monkeypatch.setattr(replay_module, "_evaluate_precursors_at_time", fake_evaluate)
    monkeypatch.setattr(replay_module, "posterior_with_ci", fake_posterior)

    model_p, trace = evaluate_class_at_frozen_time(
        patched_pattern_library.class_id,
        _PREDICTION_TS,
        _frozen_state(),
        store=store,
    )

    assert len(recorder.evaluate_calls) == 1
    call = recorder.evaluate_calls[0]
    assert call["store"] is store
    assert call["cls"] is patched_pattern_library
    assert call["as_of_ts"] == _PREDICTION_TS

    assert len(recorder.posterior_calls) == 1
    posterior_call = recorder.posterior_calls[0]
    assert posterior_call["current_values"] == {"var_a": 1.5}
    assert posterior_call["co_occurrence_correction"] == 0.0

    assert model_p == pytest.approx(0.55)
    assert isinstance(trace, dict)
    assert trace["class_id"] == patched_pattern_library.class_id
    assert trace["data_as_of"] == _PREDICTION_TS.isoformat()
    assert trace["library_version"] == 7
    assert trace["posterior"]["point"] == pytest.approx(0.55)


def test_wrapper_returns_model_p_and_trace_shape(
    monkeypatch: pytest.MonkeyPatch,
    store: object,
    patched_pattern_library: EventClass,
) -> None:
    """Return shape is ``(float, dict)`` with the scanner trace schema."""
    monkeypatch.setattr(
        replay_module,
        "_evaluate_precursors_at_time",
        lambda *_args, **_kwargs: ({"var_a": 0.9}, False),
    )
    monkeypatch.setattr(
        replay_module,
        "posterior_with_ci",
        lambda *_args, **_kwargs: _make_posterior(point=0.31),
    )

    model_p, trace = evaluate_class_at_frozen_time(
        patched_pattern_library.class_id,
        _PREDICTION_TS,
        _frozen_state(),
        store=store,
    )

    assert isinstance(model_p, float)
    assert model_p == pytest.approx(0.31)

    # Spot-check the scanner trace schema fields the calibration scoring
    # path consumes downstream (design §3.6).
    expected_keys = {
        "class_id",
        "class_definition_version",
        "library_version",
        "data_as_of",
        "prior",
        "precursors",
        "co_occurrence_correction",
        "posterior",
        "log_odds_shift",
        "is_candidate",
        "candidate_direction",
        "warnings",
        "no_update_applied",
        "no_update_reason",
        "ci_method",
    }
    assert expected_keys.issubset(trace.keys())
    assert trace["is_candidate"] is False
    assert trace["candidate_direction"] is None
    assert trace["no_update_applied"] is False
    assert trace["warnings"] == []


def test_wrapper_raises_insufficient_precursor_data_when_below_min_support(
    monkeypatch: pytest.MonkeyPatch,
    store: object,
    patched_pattern_library: EventClass,
) -> None:
    """Below-floor ``current_values`` must raise :class:`InsufficientPrecursorData`."""
    monkeypatch.setattr(
        replay_module,
        "_evaluate_precursors_at_time",
        lambda *_args, **_kwargs: ({"var_a": None}, True),
    )

    def explode_posterior(*_args: Any, **_kwargs: Any) -> PosteriorResult:
        raise AssertionError("posterior_with_ci should not be called when min_support is unmet")

    monkeypatch.setattr(replay_module, "posterior_with_ci", explode_posterior)

    with pytest.raises(InsufficientPrecursorData) as exc_info:
        evaluate_class_at_frozen_time(
            patched_pattern_library.class_id,
            _PREDICTION_TS,
            _frozen_state(),
            store=store,
            min_support=DEFAULT_MIN_SUPPORT,
        )

    message = str(exc_info.value)
    assert patched_pattern_library.class_id in message
    assert "observed=0" in message
    assert "required=1" in message


def test_wrapper_admits_zero_valued_precursors_toward_support(
    monkeypatch: pytest.MonkeyPatch,
    store: object,
    patched_pattern_library: EventClass,
) -> None:
    """A zero observation is a valid measurement and counts toward support.

    Only ``None`` indicates absence; ``0.0`` is a real value the
    posterior engine will use.
    """
    monkeypatch.setattr(
        replay_module,
        "_evaluate_precursors_at_time",
        lambda *_args, **_kwargs: ({"var_a": 0.0}, False),
    )
    monkeypatch.setattr(
        replay_module,
        "posterior_with_ci",
        lambda *_args, **_kwargs: _make_posterior(point=0.2),
    )

    model_p, _trace = evaluate_class_at_frozen_time(
        patched_pattern_library.class_id,
        _PREDICTION_TS,
        _frozen_state(),
        store=store,
        min_support=1,
    )
    assert model_p == pytest.approx(0.2)


def test_wrapper_rejects_unregistered_class_with_config_error(
    store: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``class_id`` raises :class:`BacktestConfigError`."""
    monkeypatch.setattr(replay_module.registry, "is_registered", lambda class_id: False)

    with pytest.raises(BacktestConfigError) as exc_info:
        evaluate_class_at_frozen_time(
            "ghost_class",
            _PREDICTION_TS,
            _frozen_state(),
            store=store,
        )
    assert "ghost_class" in str(exc_info.value)


def test_wrapper_rejects_min_support_zero(
    store: object,
) -> None:
    """``min_support < 1`` is a configuration bug, not a recoverable failure."""
    with pytest.raises(BacktestConfigError):
        evaluate_class_at_frozen_time(
            "any",
            _PREDICTION_TS,
            _frozen_state(),
            store=store,
            min_support=0,
        )


def test_wrapper_raises_config_error_when_no_base_rate(
    monkeypatch: pytest.MonkeyPatch,
    store: object,
) -> None:
    """Missing base rate is reported as :class:`BacktestConfigError`.

    The replay loop's ``try/except Exception`` catch-all maps this to an
    ``exception`` skip row, keeping ``insufficient_data`` reserved for
    its narrow precursor-count meaning (design §3.13 closed enumeration).
    """
    cls = _make_event_class(precursors=(_make_precursor("var_a"),))
    monkeypatch.setattr(
        replay_module.registry,
        "is_registered",
        lambda class_id: class_id == cls.class_id,
    )
    monkeypatch.setattr(replay_module.registry, "get", lambda class_id: cls)
    monkeypatch.setattr(
        replay_module.library,
        "base_rate",
        lambda store, class_id, *, library_version=None: None,
    )

    with pytest.raises(BacktestConfigError) as exc_info:
        evaluate_class_at_frozen_time(
            cls.class_id,
            _PREDICTION_TS,
            _frozen_state(),
            store=store,
        )
    assert "no persisted base rate" in str(exc_info.value)


def test_wrapper_falls_back_to_latest_when_pinned_version_missing(
    monkeypatch: pytest.MonkeyPatch,
    store: object,
) -> None:
    """Version-pin miss falls back to latest-row lookup, mirroring the live scanner.

    The scanner's :func:`evaluate_class` performs the same fallback when
    a class has not been re-evaluated at the requested library version;
    keeping the wrapper aligned avoids gratuitous skip rows for a
    benign pin/refresh race.
    """
    cls = _make_event_class(precursors=(_make_precursor("var_a"),))
    base_rate_latest = _make_base_rate(library_version=11)
    signatures_latest: tuple[SignatureResult, ...] = (_make_signature("var_a", library_version=11),)

    base_rate_calls: list[int | None] = []
    signature_calls: list[int | None] = []

    def fake_base_rate(
        _store: object,
        class_id: str,
        *,
        library_version: int | None = None,
    ) -> BaseRateResult | None:
        base_rate_calls.append(library_version)
        # First call (pinned) misses, second call (no pin) returns the row.
        if library_version is None:
            return base_rate_latest
        return None

    def fake_signature(
        _store: object,
        class_id: str,
        *,
        library_version: int | None = None,
    ) -> tuple[SignatureResult, ...]:
        signature_calls.append(library_version)
        return signatures_latest

    monkeypatch.setattr(
        replay_module.registry,
        "is_registered",
        lambda class_id: class_id == cls.class_id,
    )
    monkeypatch.setattr(replay_module.registry, "get", lambda class_id: cls)
    monkeypatch.setattr(replay_module.library, "base_rate", fake_base_rate)
    monkeypatch.setattr(replay_module.library, "signature", fake_signature)
    monkeypatch.setattr(
        replay_module,
        "_evaluate_precursors_at_time",
        lambda *_args, **_kwargs: ({"var_a": 1.5}, False),
    )
    monkeypatch.setattr(
        replay_module,
        "posterior_with_ci",
        lambda *_args, **_kwargs: _make_posterior(point=0.4),
    )

    model_p, trace = evaluate_class_at_frozen_time(
        cls.class_id,
        _PREDICTION_TS,
        _frozen_state(),
        store=store,
        library_version=99,
    )
    assert model_p == pytest.approx(0.4)
    # Pinned attempt + unpinned fallback = two base_rate lookups.
    assert base_rate_calls == [99, None]
    # Signature lookup uses the *resolved* version on the persisted row.
    assert signature_calls == [11]
    assert trace["library_version"] == 11
