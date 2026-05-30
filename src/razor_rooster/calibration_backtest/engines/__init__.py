"""Calibration-backtest computation engines (T-CB-001, T-CB-011).

Hosts the replay loop, freezer, polarity resolver, scoring, comparison,
and trace codec. Engine bodies land in subsequent tasks.

The trace codec (T-CB-011) is implemented and re-exported here so
callers can ``from razor_rooster.calibration_backtest.engines import
encode_trace, decode_trace, build_trace`` without reaching into the
submodule path.
"""

from __future__ import annotations

from razor_rooster.calibration_backtest.engines.trace_codec import (
    COMPRESSION_ALGORITHM,
    COMPRESSION_LEVEL,
    build_trace,
    decode_trace,
    encode_trace,
)

__all__ = [
    "COMPRESSION_ALGORITHM",
    "COMPRESSION_LEVEL",
    "build_trace",
    "decode_trace",
    "encode_trace",
]
