"""zstd encode/decode for ``backtest_traces`` (T-CB-011).

Implements REQ-CB-PERSIST-002 + D4 from the calibration_backtest design:
per-prediction traces are always-on, serialised as canonical JSON, and
compressed with zstd (level 3 by default) before being persisted to
``backtest_traces.trace_json_compressed`` as a BLOB.

Pipeline (design §3.6, §3.11):

* ``encode_trace(payload)`` — ``json.dumps(sort_keys=True,
  separators=(",", ":"))`` → ``utf-8`` → ``zstandard.ZstdCompressor(
  level=3).compress(...)``. Returns ``(compressed_blob,
  decompressed_size_bytes)`` so callers can populate
  :class:`~razor_rooster.calibration_backtest.models.BacktestTrace`
  and the budget guard.
* ``decode_trace(blob, algorithm="zstd")`` — validates the algorithm
  marker (only ``"zstd"`` is supported in v1; other markers raise
  :class:`BacktestPersistenceError`), decompresses with
  :class:`zstandard.ZstdDecompressor`, then ``json.loads`` the
  resulting bytes. Decompression failures (corrupt blobs) bubble out
  as :class:`BacktestPersistenceError` so persistence-layer callers
  can handle them uniformly.
* ``build_trace(run_id, prediction_id, payload)`` — convenience
  constructor that calls :func:`encode_trace` and returns a populated
  :class:`BacktestTrace` dataclass with the canonical zstd algorithm
  marker.

The canonical-JSON normalisation (sorted keys, no whitespace) ensures
``decode(encode(payload)) == payload`` for any JSON-roundtrippable
mapping, and guarantees that two replays of the same trace produce
byte-identical compressed blobs (deterministic for a given payload,
zstandard library version, and compression level).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

import zstandard

from razor_rooster.calibration_backtest.errors import BacktestPersistenceError
from razor_rooster.calibration_backtest.models import BacktestTrace, CompressionAlgorithm

#: Default zstd compression level (D4: level 3 → ~4-5x shrink on structured JSON).
COMPRESSION_LEVEL: Final[int] = 3

#: Canonical compression-algorithm marker persisted alongside every blob.
COMPRESSION_ALGORITHM: Final[CompressionAlgorithm] = CompressionAlgorithm.ZSTD


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialise *payload* to canonical UTF-8 JSON bytes.

    Uses sorted keys and the most compact separators so the byte
    representation is stable across replays (design §3.11).
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_trace(payload: Mapping[str, Any]) -> tuple[bytes, int]:
    """Serialise and zstd-compress a trace payload.

    :param payload: A JSON-roundtrippable mapping (the scanner trace
        ``to_dict()`` output).
    :returns: A ``(compressed_blob, decompressed_size_bytes)`` tuple
        where ``decompressed_size_bytes`` is the length of the canonical
        UTF-8 JSON representation prior to compression. Persist both
        values to ``backtest_traces`` (REQ-CB-PERSIST-002,
        REQ-CB-PERSIST-003).
    """
    serialised = _canonical_json_bytes(payload)
    decompressed_size_bytes = len(serialised)
    compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)
    compressed_blob = compressor.compress(serialised)
    return compressed_blob, decompressed_size_bytes


def decode_trace(blob: bytes, *, algorithm: str = "zstd") -> Mapping[str, Any]:
    """Decompress and deserialise a persisted trace blob.

    :param blob: The ``trace_json_compressed`` BLOB from
        ``backtest_traces``.
    :param algorithm: The persisted compression-algorithm marker. Only
        ``"zstd"`` is supported in v1; any other marker raises
        :class:`BacktestPersistenceError` for forward compatibility.
    :returns: The original trace mapping (``dict``) recovered from the
        compressed blob.
    :raises BacktestPersistenceError: If *algorithm* is unsupported, the
        blob fails to decompress, or the decompressed payload is not
        valid JSON.
    """
    if algorithm != CompressionAlgorithm.ZSTD.value:
        raise BacktestPersistenceError(
            f"decode_trace: unsupported compression_algorithm {algorithm!r} "
            f"(expected {CompressionAlgorithm.ZSTD.value!r})"
        )
    try:
        decompressor = zstandard.ZstdDecompressor()
        decompressed = decompressor.decompress(blob)
    except zstandard.ZstdError as exc:
        raise BacktestPersistenceError(f"decode_trace: zstd decompression failed: {exc}") from exc
    try:
        payload: Any = json.loads(decompressed)
    except json.JSONDecodeError as exc:
        raise BacktestPersistenceError(f"decode_trace: JSON deserialisation failed: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise BacktestPersistenceError(
            f"decode_trace: decoded payload must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def build_trace(run_id: str, prediction_id: str, payload: Mapping[str, Any]) -> BacktestTrace:
    """Encode *payload* and return a populated :class:`BacktestTrace`.

    Convenience constructor used by the replay loop and persistence
    layer: it pairs :func:`encode_trace` with the canonical
    :data:`COMPRESSION_ALGORITHM` marker so callers do not have to
    repeat the wiring at every site.
    """
    compressed_blob, decompressed_size_bytes = encode_trace(payload)
    return BacktestTrace(
        run_id=run_id,
        prediction_id=prediction_id,
        trace_json_compressed=compressed_blob,
        decompressed_size_bytes=decompressed_size_bytes,
        compression_algorithm=COMPRESSION_ALGORITHM,
    )


def encode(trace_dict: Mapping[str, Any]) -> bytes:
    """Return the zstd-compressed canonical JSON encoding of ``trace_dict``.

    Thin wrapper around :func:`encode_trace` for callers that only need
    the compressed BLOB and not the decompressed-size sidecar. Used by
    the replay loop (T-CB-019) when assembling the
    :class:`BacktestTrace` insert payload alongside an explicit size
    capture from :func:`encode_trace`.

    The encoding is deterministic: ``encode(payload)`` is byte-identical
    across replays for the same payload, ``zstandard`` library version,
    and :data:`COMPRESSION_LEVEL` (D4: zstd level 3).
    """
    blob, _decompressed_size_bytes = encode_trace(trace_dict)
    return blob


def decode(blob: bytes, *, algorithm: str = "zstd") -> Mapping[str, Any]:
    """Alias for :func:`decode_trace` matching the design §3.5 call shape.

    Symmetric counterpart to :func:`encode`: ``decode(encode(t)) == t``
    under canonical-JSON normalisation. Provided so the replay-loop
    site reads as ``trace_codec.encode(t)`` / ``trace_codec.decode(b)``
    while existing call sites that already use :func:`encode_trace` /
    :func:`decode_trace` continue to work unchanged.
    """
    return decode_trace(blob, algorithm=algorithm)


__all__ = [
    "COMPRESSION_ALGORITHM",
    "COMPRESSION_LEVEL",
    "build_trace",
    "decode",
    "decode_trace",
    "encode",
    "encode_trace",
]
