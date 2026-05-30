"""T-021 verification — structured JSON logging with credential redaction.

The redaction tests are the security-critical part. Each redaction test
asserts that a synthetic credential value never appears in any output bytes,
across the three pathways:

1. The format-string template (``logger.info("key=%s")``).
2. Format args (``logger.info("key=%s", "secret")``).
3. ``extra={...}`` payloads, including nested dicts.

Plus URL query stripping and HTTP-header redaction.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from razor_rooster.data_ingest.logging.structured import (
    ConnectorOutcome,
    CycleSummary,
    JsonFormatter,
    RedactionFilter,
    configure_structured_logger,
    cycle_logger,
)


def _make_logger(tmp_path: Path, name: str) -> tuple[logging.Logger, Path]:
    log_path = tmp_path / f"{name}.jsonl"
    logger = configure_structured_logger(name, target=log_path)
    logger.handlers = [h for h in logger.handlers if h.formatter is not None]
    return logger, log_path


def _read_log_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_json_formatter_emits_one_object_per_line() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = formatter.format(record)
    parsed = json.loads(line)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "x"
    assert parsed["timestamp"].endswith("+00:00")


def test_json_formatter_includes_extras() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.cycle_id = "abc-123"
    record.records_ingested = 42
    parsed = json.loads(formatter.format(record))
    assert parsed["extras"]["cycle_id"] == "abc-123"
    assert parsed["extras"]["records_ingested"] == 42


def test_json_formatter_includes_exception_info() -> None:
    formatter = JsonFormatter()
    try:
        raise RuntimeError("simulated failure")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="oops",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "RuntimeError" in parsed["exc_info"]
    assert "simulated failure" in parsed["exc_info"]


def test_redaction_strips_long_token_in_message(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact1")
    long_token = "a" * 40  # >32 chars → matches the token regex
    logger.info(f"got token={long_token}")
    contents = log_path.read_text()
    assert long_token not in contents
    assert "<redacted>" in contents


def test_redaction_strips_token_in_format_args(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact2")
    long_token = "b" * 50
    logger.info("got token=%s", long_token)
    contents = log_path.read_text()
    assert long_token not in contents


def test_redaction_strips_token_in_extra_dict(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact3")
    long_token = "c" * 64
    logger.info("event", extra={"raw_token": long_token})
    contents = log_path.read_text()
    assert long_token not in contents


def test_redaction_strips_token_in_nested_extra(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact4")
    long_token = "d" * 32  # exactly 32 chars (boundary)
    logger.info("event", extra={"request": {"params": {"api_key": long_token}}})
    contents = log_path.read_text()
    assert long_token not in contents


def test_redaction_short_strings_unaffected(tmp_path: Path) -> None:
    """A short string (less than 32 chars) is not a credential and should pass through."""
    logger, log_path = _make_logger(tmp_path, "redact5")
    logger.info("normal message", extra={"source_id": "fred", "duration_seconds": 12.4})
    parsed = _read_log_lines(log_path)
    assert parsed[0]["message"] == "normal message"
    assert parsed[0]["extras"]["source_id"] == "fred"
    assert parsed[0]["extras"]["duration_seconds"] == 12.4


def test_redaction_strips_url_query_string(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact6")
    long_query_value = "z" * 40
    url = f"https://example.com/api/v1/data?api_key={long_query_value}&format=json"
    logger.info("fetching %s", url)
    contents = log_path.read_text()
    assert long_query_value not in contents
    # The path itself should still be present so logs are useful.
    assert "https://example.com/api/v1/data" in contents


def test_redaction_strips_authorization_header(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact7")
    secret_token = "xyz123abcdef456ghi789jkl012mno345pq"  # >32 chars
    headers = {"Authorization": f"Bearer {secret_token}", "Content-Type": "application/json"}
    logger.info("request", extra={"headers": headers})
    contents = log_path.read_text()
    assert secret_token not in contents
    # Content-Type is fine to log.
    assert "application/json" in contents


def test_redaction_strips_x_api_key_header_case_insensitive(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact8")
    secret = "sensitive_api_key_value_should_not_appear"
    headers = {"X-API-Key": secret, "user-agent": "razor-rooster/0.1"}
    logger.info("request", extra={"headers": headers})
    contents = log_path.read_text()
    assert secret not in contents
    assert "razor-rooster/0.1" in contents


def test_redaction_strips_cookie_header(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "redact9")
    cookie_secret = "session=abcdefghij1234567890abcdefgh"  # contains a long token
    headers = {"Cookie": cookie_secret}
    logger.info("request", extra={"headers": headers})
    contents = log_path.read_text()
    assert "abcdefghij1234567890abcdefgh" not in contents


def test_redaction_strips_bearer_token_with_dots(tmp_path: Path) -> None:
    """JWT-style tokens contain dots and shouldn't slip through."""
    logger, log_path = _make_logger(tmp_path, "redact10")
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.signaturepart"
    logger.info("auth: Bearer %s", jwt)
    contents = log_path.read_text()
    assert jwt not in contents


def test_redaction_filter_can_be_added_directly_to_a_logger() -> None:
    """The filter is usable independently of the JsonFormatter."""
    handler = logging.NullHandler()
    handler.addFilter(RedactionFilter())
    logger = logging.getLogger("test_redact_direct")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    record = logger.makeRecord(
        name="test_redact_direct",
        level=logging.INFO,
        fn="",
        lno=0,
        msg="key=%s",
        args=("a" * 40,),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    assert "a" * 40 not in record.getMessage()


def test_configure_structured_logger_creates_parent_dirs(tmp_path: Path) -> None:
    deep_path = tmp_path / "deep" / "nested" / "log.jsonl"
    assert not deep_path.parent.exists()
    logger = configure_structured_logger("test_parent_dirs", target=deep_path)
    logger.info("hello")
    assert deep_path.exists()
    # Cleanup so the logger's handler doesn't leak into other tests.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_configure_structured_logger_idempotent(tmp_path: Path) -> None:
    """Re-calling with the same target does not duplicate file handlers."""
    target = tmp_path / "idem.jsonl"
    logger1 = configure_structured_logger("test_idem", target=target)
    logger2 = configure_structured_logger("test_idem", target=target)
    assert logger1 is logger2
    file_handlers = [h for h in logger1.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    for handler in list(logger1.handlers):
        logger1.removeHandler(handler)
        handler.close()


def test_cycle_logger_writes_summary_on_exit(tmp_path: Path) -> None:
    log_dir = tmp_path / "cycles"
    with cycle_logger(cycle_id="test-cycle", log_dir=log_dir) as summary:
        summary.connectors.append(
            ConnectorOutcome(
                source_id="fred", status="ok", records_ingested=10, duration_seconds=1.2
            )
        )
        summary.stale_sources.append("noaa")
    log_files = list(log_dir.glob("cycle-*.jsonl"))
    assert len(log_files) == 1
    lines = _read_log_lines(log_files[0])
    summary_line = next(line for line in lines if "cycle_summary" in line.get("extras", {}))
    assert summary_line["extras"]["cycle_summary"]["cycle_id"] == "test-cycle"
    connectors = summary_line["extras"]["cycle_summary"]["connectors"]
    assert len(connectors) == 1
    assert connectors[0]["source_id"] == "fred"
    assert connectors[0]["records_ingested"] == 10
    assert summary_line["extras"]["cycle_summary"]["stale_sources"] == ["noaa"]


def test_cycle_logger_records_exception_then_re_raises(tmp_path: Path) -> None:
    log_dir = tmp_path / "cycles_err"
    with (
        pytest.raises(RuntimeError, match="boom"),
        cycle_logger(cycle_id="errcycle", log_dir=log_dir) as summary,
    ):
        summary.connectors.append(ConnectorOutcome(source_id="fred", status="ok"))
        raise RuntimeError("boom")
    log_files = list(log_dir.glob("cycle-*.jsonl"))
    assert len(log_files) == 1
    lines = _read_log_lines(log_files[0])
    summary_line = next(line for line in lines if "cycle_summary" in line.get("extras", {}))
    anomalies = summary_line["extras"]["cycle_summary"]["anomalies_detected"]
    assert any(a["type"] == "cycle_exception" for a in anomalies)
    assert any("boom" in a["message"] for a in anomalies)


def test_cycle_logger_default_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without log_dir, cycle_logger writes to ./logs/cycles/."""
    monkeypatch.chdir(tmp_path)
    with cycle_logger() as _:
        pass
    assert (tmp_path / "logs" / "cycles").exists()


def test_dataclasses_serialize_to_json(tmp_path: Path) -> None:
    """``ConnectorOutcome`` and ``CycleSummary`` are JSON-friendly via asdict."""
    from dataclasses import asdict

    outcome = ConnectorOutcome(
        source_id="fred",
        status="ok",
        records_ingested=100,
        records_skipped_duplicate=10,
        duration_seconds=5.0,
        errors=[{"type": "rate_limit", "retries": 2}],
    )
    summary = CycleSummary(
        cycle_id="abc",
        started_at="2026-05-14T08:00:00+00:00",
        connectors=[outcome],
    )
    serialized = json.dumps(asdict(summary))
    parsed = json.loads(serialized)
    assert parsed["cycle_id"] == "abc"
    assert parsed["connectors"][0]["records_ingested"] == 100


def test_redaction_does_not_strip_short_random_strings(tmp_path: Path) -> None:
    """Source IDs, table names, etc. (short strings) should pass through unchanged."""
    logger, log_path = _make_logger(tmp_path, "shortok")
    logger.info("table=%s source=%s", "event_stream", "fred")
    parsed = _read_log_lines(log_path)
    assert "event_stream" in parsed[0]["message"]
    assert "fred" in parsed[0]["message"]


def test_redaction_handles_strings_with_no_credentials(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "clean")
    msg = "Cycle started for connector fred at 2026-05-14T08:00:00Z"
    logger.info(msg)
    contents = log_path.read_text()
    assert msg in contents


def test_token_regex_matches_only_token_shapes() -> None:
    """The regex shouldn't fire on common short identifiers."""
    matches = re.findall(r"\b[A-Za-z0-9_\-]{32,}\b", "fred 2026 abc-123 cycle-id-abc")
    assert matches == []


def test_url_query_strip_does_not_break_path_logging(tmp_path: Path) -> None:
    logger, log_path = _make_logger(tmp_path, "url_path")
    logger.info("GET https://api.example.com/v1/data")  # no query string
    contents = log_path.read_text()
    assert "https://api.example.com/v1/data" in contents
