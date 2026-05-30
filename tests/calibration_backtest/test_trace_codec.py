"""T-CB-011 — calibration_backtest trace codec tests.

Round-trip, canonicalisation, compression-ratio, and error-path
coverage for
:mod:`razor_rooster.calibration_backtest.engines.trace_codec`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import zstandard

from razor_rooster.calibration_backtest.engines import trace_codec as trace_codec_module
from razor_rooster.calibration_backtest.engines.trace_codec import (
    COMPRESSION_ALGORITHM,
    COMPRESSION_LEVEL,
    build_trace,
    decode_trace,
    encode_trace,
)
from razor_rooster.calibration_backtest.errors import BacktestPersistenceError
from razor_rooster.calibration_backtest.models import BacktestTrace, CompressionAlgorithm


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_module_exports() -> None:
    """`engines/__init__.py` re-exports the codec public surface."""
    from razor_rooster.calibration_backtest import engines as engines_module

    assert engines_module.encode_trace is encode_trace
    assert engines_module.decode_trace is decode_trace
    assert engines_module.build_trace is build_trace
    assert engines_module.COMPRESSION_LEVEL == COMPRESSION_LEVEL
    assert engines_module.COMPRESSION_ALGORITHM is COMPRESSION_ALGORITHM


def test_compression_constants_match_design() -> None:
    """D4: zstd at level 3."""
    assert COMPRESSION_LEVEL == 3
    assert COMPRESSION_ALGORITHM is CompressionAlgorithm.ZSTD
    assert COMPRESSION_ALGORITHM.value == "zstd"


def test_round_trip_simple() -> None:
    payload: dict[str, Any] = {"a": 1, "b": "x"}
    blob, size = encode_trace(payload)
    decoded = decode_trace(blob)
    assert dict(decoded) == payload
    assert size == len(_canonical_bytes(payload))


def test_round_trip_nested() -> None:
    payload: dict[str, Any] = {"outer": {"inner": [1, 2, {"k": "v"}]}}
    blob, _size = encode_trace(payload)
    decoded = decode_trace(blob)
    assert dict(decoded) == payload


def test_round_trip_unicode() -> None:
    payload: dict[str, Any] = {"text": "résumé · café"}
    blob, size = encode_trace(payload)
    # UTF-8 encoding of multibyte chars must be reflected in the
    # decompressed-size hint.
    assert size == len(_canonical_bytes(payload))
    decoded = decode_trace(blob)
    assert dict(decoded) == payload


def test_round_trip_representative_scanner_trace() -> None:
    """Representative scanner Trace shape (design §3.6)."""
    payload: dict[str, Any] = {
        "class_id": "geo.conflict.escalation.v1",
        "library_version": 7,
        "prior": 0.12,
        "precursors": [
            {"id": "evt.alpha", "weight": 0.4, "decay": 0.85},
            {"id": "evt.beta", "weight": 0.6, "decay": 0.5},
        ],
        "posterior": 0.34,
        "log_odds_shift": 1.21,
        "is_candidate": True,
        "warnings": [],
        "ci_method": "bootstrap",
        "ci_lower": 0.27,
        "ci_upper": 0.41,
    }
    blob, size = encode_trace(payload)
    decoded = decode_trace(blob)
    assert dict(decoded) == payload
    assert size == len(_canonical_bytes(payload))


def test_decompressed_size_matches_canonical_serialisation() -> None:
    """`decompressed_size_bytes` equals len of canonical UTF-8 JSON."""
    payload: dict[str, Any] = {"z": [1, 2, 3], "a": "alpha", "m": {"k": True}}
    _blob, size = encode_trace(payload)
    expected = len(_canonical_bytes(payload))
    assert size == expected


def test_canonical_keys_sorted_independent_of_input_order() -> None:
    """Permuting input key order must yield the same compressed blob."""
    payload_a: dict[str, Any] = {"a": 1, "b": 2, "c": 3}
    payload_b: dict[str, Any] = {"c": 3, "a": 1, "b": 2}
    blob_a, _ = encode_trace(payload_a)
    blob_b, _ = encode_trace(payload_b)
    assert blob_a == blob_b


def test_compression_reduces_size_for_repetitive_payload() -> None:
    """Highly repetitive structured JSON shrinks under zstd level 3."""
    repetitive: dict[str, Any] = {
        f"key_{i:04d}": {"sector": "geopolitics", "outcome": "resolved", "p": 0.5}
        for i in range(200)
    }
    blob, size = encode_trace(repetitive)
    assert len(blob) < size, (
        f"expected zstd to shrink repetitive payload (raw={size}, compressed={len(blob)})"
    )
    # Sanity-check the design doc claim of ~4-5x shrink (allow generous
    # margin so we don't rely on exact zstd internals).
    assert len(blob) * 3 < size


def test_decode_unknown_algorithm_raises() -> None:
    blob, _ = encode_trace({"a": 1})
    with pytest.raises(BacktestPersistenceError, match="unsupported compression_algorithm"):
        decode_trace(blob, algorithm="lzma")


def test_decode_corrupted_blob_raises() -> None:
    """Random bytes are not a valid zstd stream."""
    with pytest.raises(BacktestPersistenceError, match="zstd decompression failed"):
        decode_trace(b"not-a-zstd-frame", algorithm="zstd")


def test_decode_non_object_payload_raises() -> None:
    """Decoded payload must be a JSON object (mapping)."""
    serialised = json.dumps([1, 2, 3], separators=(",", ":")).encode("utf-8")
    blob = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL).compress(serialised)
    with pytest.raises(BacktestPersistenceError, match="must be a JSON object"):
        decode_trace(blob)


def test_decode_invalid_json_after_decompression_raises() -> None:
    """Valid zstd frame containing non-JSON bytes raises a wrapped error."""
    blob = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL).compress(b"not json at all {{{")
    with pytest.raises(BacktestPersistenceError, match="JSON deserialisation failed"):
        decode_trace(blob)


def test_build_trace_populates_dataclass() -> None:
    payload: dict[str, Any] = {"class_id": "demo", "p": 0.5}
    trace = build_trace("run-abc", "pred-001", payload)
    assert isinstance(trace, BacktestTrace)
    assert trace.run_id == "run-abc"
    assert trace.prediction_id == "pred-001"
    assert trace.compression_algorithm is CompressionAlgorithm.ZSTD
    assert trace.decompressed_size_bytes == len(_canonical_bytes(payload))
    # Round-trip the payload via the persisted blob.
    decoded = decode_trace(
        trace.trace_json_compressed,
        algorithm=trace.compression_algorithm.value,
    )
    assert dict(decoded) == payload


def test_build_trace_validates_run_id_and_prediction_id() -> None:
    """Empty identifiers must surface via the dataclass validator."""
    from razor_rooster.calibration_backtest.errors import BacktestConfigError

    with pytest.raises(BacktestConfigError):
        build_trace("", "p", {"a": 1})
    with pytest.raises(BacktestConfigError):
        build_trace("r", "", {"a": 1})


def test_encoding_is_deterministic() -> None:
    """Same payload, level, and library version → byte-identical blobs."""
    payload: dict[str, Any] = {"x": 1, "y": [3, 2, 1], "z": {"nested": "value"}}
    blob_a, size_a = encode_trace(payload)
    blob_b, size_b = encode_trace(payload)
    assert blob_a == blob_b
    assert size_a == size_b


def test_decode_default_algorithm_is_zstd() -> None:
    """Calling `decode_trace` without `algorithm` must accept zstd blobs."""
    payload: dict[str, Any] = {"k": "v"}
    blob, _ = encode_trace(payload)
    decoded = decode_trace(blob)  # default algorithm
    assert dict(decoded) == payload


def test_round_trip_floats_and_nulls() -> None:
    payload: dict[str, Any] = {
        "p": 0.3333333333,
        "q": None,
        "flag": False,
        "arr": [None, 1.5, "x"],
    }
    blob, _ = encode_trace(payload)
    decoded = decode_trace(blob)
    assert dict(decoded) == payload


def test_module_all_surface() -> None:
    expected = {
        "COMPRESSION_ALGORITHM",
        "COMPRESSION_LEVEL",
        "build_trace",
        "decode_trace",
        "encode_trace",
    }
    assert set(trace_codec_module.__all__) == expected
