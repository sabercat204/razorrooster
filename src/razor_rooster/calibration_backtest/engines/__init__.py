"""Calibration-backtest computation engines (T-CB-001, T-CB-011, T-CB-017).

Hosts the replay loop, freezer, polarity resolver, scoring, comparison,
and trace codec. Engine bodies land across the Phase 3 task sequence.

The trace codec (T-CB-011) and the per-prediction orchestration wrapper
(T-CB-017, :func:`evaluate_class_at_frozen_time`) are re-exported here
so callers can ``from razor_rooster.calibration_backtest.engines import
encode_trace, decode_trace, build_trace, evaluate_class_at_frozen_time``
without reaching into individual submodules.
"""

from __future__ import annotations

from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_MIN_SUPPORT,
    DEFAULT_RECENT_WINDOW_DAYS,
    MappedResolution,
    ReplayResult,
    evaluate_class_at_frozen_time,
    iter_mapped_resolutions,
    polarity_correct,
    run_backtest,
)
from razor_rooster.calibration_backtest.engines.trace_codec import (
    COMPRESSION_ALGORITHM,
    COMPRESSION_LEVEL,
    build_trace,
    decode,
    decode_trace,
    encode,
    encode_trace,
)

__all__ = [
    "COMPRESSION_ALGORITHM",
    "COMPRESSION_LEVEL",
    "DEFAULT_MIN_SUPPORT",
    "DEFAULT_RECENT_WINDOW_DAYS",
    "MappedResolution",
    "ReplayResult",
    "build_trace",
    "decode",
    "decode_trace",
    "encode",
    "encode_trace",
    "evaluate_class_at_frozen_time",
    "iter_mapped_resolutions",
    "polarity_correct",
    "run_backtest",
]
