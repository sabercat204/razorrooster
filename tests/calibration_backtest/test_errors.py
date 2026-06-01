"""T-CB-003 — calibration_backtest typed exception hierarchy tests."""

from __future__ import annotations

import pytest

from razor_rooster.calibration_backtest import errors as errors_module
from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    BacktestPersistenceError,
    BacktestSchemaError,
    CalibrationBacktestError,
    DiskBudgetError,
    InsufficientPrecursorData,
    InvalidLagError,
    InvalidResolutionError,
    MappingNotFoundError,
    NoPolarityError,
    RecentWindowError,
    RunNotFoundError,
)

_SUBCLASSES: tuple[type[CalibrationBacktestError], ...] = (
    BacktestConfigError,
    BacktestPersistenceError,
    BacktestSchemaError,
    DiskBudgetError,
    InsufficientPrecursorData,
    InvalidLagError,
    InvalidResolutionError,
    MappingNotFoundError,
    NoPolarityError,
    RecentWindowError,
)


def test_root_constructible() -> None:
    """The root error stores the message on ``self.message``."""
    with pytest.raises(CalibrationBacktestError) as excinfo:
        raise CalibrationBacktestError("boom")
    assert excinfo.value.message == "boom"
    assert str(excinfo.value) == "boom"


def test_root_inherits_from_exception() -> None:
    assert issubclass(CalibrationBacktestError, Exception)


@pytest.mark.parametrize("subclass", _SUBCLASSES)
def test_each_subclass_inherits_from_root(
    subclass: type[CalibrationBacktestError],
) -> None:
    """Every typed exception inherits from the calibration-backtest root."""
    assert issubclass(subclass, CalibrationBacktestError)


@pytest.mark.parametrize("subclass", _SUBCLASSES)
def test_each_subclass_is_exception(
    subclass: type[CalibrationBacktestError],
) -> None:
    """Every typed exception is also a built-in ``Exception``."""
    assert issubclass(subclass, Exception)


@pytest.mark.parametrize("subclass", _SUBCLASSES)
def test_message_attribute(subclass: type[CalibrationBacktestError]) -> None:
    """Each subclass carries the ``message`` attribute set via the root init."""
    instance = subclass("context-detail")
    assert instance.message == "context-detail"
    assert str(instance) == "context-detail"


@pytest.mark.parametrize("subclass", _SUBCLASSES)
def test_subclass_can_be_caught_as_root(
    subclass: type[CalibrationBacktestError],
) -> None:
    """Catching ``CalibrationBacktestError`` traps every subsystem error."""
    with pytest.raises(CalibrationBacktestError):
        raise subclass("trapped")


def test_repr_includes_message() -> None:
    """``__repr__`` contains the class name and quoted message for log capture."""
    err = NoPolarityError("missing polarity")
    rendered = repr(err)
    assert "NoPolarityError" in rendered
    assert "'missing polarity'" in rendered


@pytest.mark.parametrize("subclass", _SUBCLASSES)
def test_repr_for_each_subclass(
    subclass: type[CalibrationBacktestError],
) -> None:
    """Every subclass renders ``ClassName('message')`` via ``__repr__``."""
    err = subclass("ctx")
    assert repr(err) == f"{subclass.__name__}('ctx')"


def test_all_listed_alphabetical() -> None:
    """``__all__`` is ordered alphabetically per project convention."""
    public = errors_module.__all__
    assert list(public) == sorted(public)


def test_all_listed_exhaustive() -> None:
    """Every defined exception class appears in ``__all__``."""
    expected = {
        "BacktestConfigError",
        "BacktestPersistenceError",
        "BacktestSchemaError",
        "CalibrationBacktestError",
        "DiskBudgetError",
        "InsufficientPrecursorData",
        "InvalidLagError",
        "InvalidResolutionError",
        "MappingNotFoundError",
        "NoPolarityError",
        "RecentWindowError",
        "RunNotFoundError",
    }
    assert set(errors_module.__all__) == expected


def test_run_not_found_error_stores_run_id() -> None:
    """``RunNotFoundError`` preserves the offending run_id and renders a
    deterministic message via the base ``CalibrationBacktestError`` init.
    """
    err = RunNotFoundError("abc123")
    assert err.run_id == "abc123"
    assert err.message == "Run not found: abc123"
    assert str(err) == "Run not found: abc123"
    assert isinstance(err, CalibrationBacktestError)


def test_errors_reexported_from_package() -> None:
    """The package ``__init__`` re-exports every error name in ``__all__``."""
    import razor_rooster.calibration_backtest as cb_pkg

    for name in errors_module.__all__:
        assert name in cb_pkg.__all__
        assert getattr(cb_pkg, name) is getattr(errors_module, name)
