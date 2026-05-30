"""Structured JSON logging with credential redaction (T-021).

Provides:

- :class:`JsonFormatter` — a stdlib ``logging.Formatter`` subclass that emits
  each log record as a single JSON object per line. Compatible with
  ``logs/cycles/cycle-<iso8601>.jsonl`` style log files (REQ-LOG-001).
- :class:`RedactionFilter` — a stdlib ``logging.Filter`` subclass that strips
  credential-shaped strings, URL query strings, and sensitive HTTP headers
  from log records before emission (REQ-LOG-002).
- :func:`configure_structured_logger` — convenience wiring that attaches both
  to a named logger and a target file or stream.
- :func:`cycle_logger` — context manager that opens a per-cycle JSONL log
  file, accumulates per-connector outcomes, and writes the structured
  cycle-summary line on exit (success or failure).

Design references:
- specs/DATA_INGEST_DESIGN.md §6.1 (structured cycle log format).
- specs/DATA_INGEST_DESIGN.md §6.2 (credential-redaction filter).
- REQ-LOG-001, REQ-LOG-002.

The redaction filter is applied at the formatter layer, *before* anything
hits disk or stderr. Tests verify the filter holds against synthetic
credentials by asserting the credential never appears in any output bytes.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

# Strings of >=32 characters consisting of url-safe base64-ish characters
# are likely API keys/tokens. We also match Bearer-token shapes explicitly.
_LIKELY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"(Bearer\s+)([A-Za-z0-9_\-\.]+)", re.IGNORECASE)
# URL query strings are stripped wholesale so credentials passed via ``?api_key=…``
# do not leak. Anything from ``?`` to whitespace is replaced.
_URL_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"\?[^\s]+")

# HTTP headers whose values must always be redacted.
_SENSITIVE_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}
)

_REDACTED_TOKEN: Final[str] = "<redacted>"
_REDACTED_BEARER: Final[str] = "Bearer <redacted>"
_REDACTED_QUERY: Final[str] = "?<redacted>"


def _redact_string(value: str) -> str:
    """Apply the three redaction passes to a string in fixed order."""
    redacted = _BEARER_RE.sub(_REDACTED_BEARER, value)
    redacted = _URL_QUERY_RE.sub(_REDACTED_QUERY, redacted)
    redacted = _LIKELY_TOKEN_RE.sub(_REDACTED_TOKEN, redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    """Recursively redact a value, returning a new object when changes apply."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_HEADER_NAMES:
                out[key] = _REDACTED_TOKEN
            else:
                out[key] = _redact_value(sub)
        return out
    if isinstance(value, list | tuple):
        seq = [_redact_value(v) for v in value]
        return type(value)(seq) if isinstance(value, tuple) else seq
    return value


class RedactionFilter(logging.Filter):
    """Strip credentials from a log record's message, args, and extras.

    Mutates in place because Python's ``logging`` system passes the same
    record to every handler; if we return a new record only one handler
    sees the redacted form.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the message string itself (the format template).
        if isinstance(record.msg, str):
            record.msg = _redact_string(record.msg)

        # Redact format args. ``args`` may be a tuple or a single mapping.
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = _redact_value(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact_value(a) for a in record.args)
            else:
                record.args = _redact_value(record.args)

        # Redact custom attributes added via ``logger.<level>(..., extra={...})``.
        for attr in list(vars(record)):
            if attr in _DEFAULT_LOGRECORD_ATTRS:
                continue
            try:
                redacted = _redact_value(getattr(record, attr))
            except Exception:
                continue
            setattr(record, attr, redacted)

        return True


# Standard LogRecord attributes we never touch when redacting.
_DEFAULT_LOGRECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Format each log record as a single JSON object on one line.

    Output schema:

        {
            "timestamp": "2026-05-14T08:30:00.000Z",
            "level": "INFO",
            "logger": "razor_rooster.data_ingest.connectors.fred",
            "message": "...",
            "extras": { ... custom fields from extra={...} ... }
        }

    Exception info, when present, is rendered to a string via the parent
    class's ``formatException`` so the JSON line stays single-line.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        # Surface custom fields passed via extra={...}.
        extras: dict[str, Any] = {}
        for attr, value in vars(record).items():
            if attr in _DEFAULT_LOGRECORD_ATTRS:
                continue
            extras[attr] = value
        if extras:
            payload["extras"] = extras
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_structured_logger(
    logger_name: str = "razor_rooster",
    *,
    target: Path | str | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Attach the JSON formatter and redaction filter to ``logger_name``.

    If ``target`` is a path, a :class:`logging.FileHandler` is added pointing
    at it (creating parent directories as needed). If ``target`` is ``None``,
    no handler is added — caller is expected to attach one.

    Idempotent on re-call: existing JSON+redaction handlers attached to the
    logger are not duplicated. Other handlers are left alone.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    formatter = JsonFormatter()
    redaction = RedactionFilter()

    if target is not None:
        target_path = Path(target) if not isinstance(target, Path) else target
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Avoid duplicate file handlers on re-call by checking existing ones.
        already_attached = any(
            isinstance(h, logging.FileHandler)
            and Path(h.baseFilename).resolve() == target_path.resolve()
            for h in logger.handlers
        )
        if not already_attached:
            handler = logging.FileHandler(target_path, encoding="utf-8")
            handler.setFormatter(formatter)
            handler.addFilter(redaction)
            logger.addHandler(handler)

    return logger


@dataclass(slots=True)
class ConnectorOutcome:
    """One connector's outcome within a cycle (REQ-LOG-001 §6.1 schema)."""

    source_id: str
    status: str  # 'ok' | 'partial' | 'failed' | 'skipped'
    records_ingested: int = 0
    records_skipped_duplicate: int = 0
    duration_seconds: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CycleSummary:
    """The full cycle log entry written on cycle exit."""

    cycle_id: str
    started_at: str
    ended_at: str | None = None
    duration_seconds: float | None = None
    connectors: list[ConnectorOutcome] = field(default_factory=list)
    stale_sources: list[str] = field(default_factory=list)
    anomalies_detected: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def cycle_logger(
    cycle_id: str | None = None,
    *,
    log_dir: Path | str | None = None,
    logger_name: str = "razor_rooster.data_ingest.cycles",
) -> Iterator[CycleSummary]:
    """Open a per-cycle JSONL log file and yield a mutable :class:`CycleSummary`.

    On exit (success or failure), writes the structured summary as a single
    JSON line at ``logs/cycles/cycle-<iso8601>.jsonl``. The same file is
    used as the backing handler for any log records emitted via the named
    logger during the ``with`` block, so per-connector log entries and the
    cycle summary live in one file.

    Raises propagate to the caller; the summary is still written, with a
    truncated ``ended_at`` and the error noted in ``anomalies_detected``.
    """
    cid = cycle_id or str(uuid.uuid4())
    started_at = datetime.now(tz=UTC)
    summary = CycleSummary(cycle_id=cid, started_at=started_at.isoformat())

    if log_dir is None:
        log_dir = Path("logs") / "cycles"
    log_dir_path = Path(log_dir) if not isinstance(log_dir, Path) else log_dir
    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_file = log_dir_path / f"cycle-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{cid[:8]}.jsonl"

    cycle_logger_obj = configure_structured_logger(logger_name, target=log_file)

    try:
        yield summary
    except BaseException as exc:
        summary.anomalies_detected.append(
            {"type": "cycle_exception", "message": str(exc), "exception_class": type(exc).__name__}
        )
        raise
    finally:
        ended_at = datetime.now(tz=UTC)
        summary.ended_at = ended_at.isoformat()
        summary.duration_seconds = (ended_at - started_at).total_seconds()
        cycle_logger_obj.info("cycle_summary", extra={"cycle_summary": asdict(summary)})
