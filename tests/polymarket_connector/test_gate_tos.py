"""T-PMC-021 — ToS acknowledgement gate tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.gates.tos import (
    ToSAcknowledgementRequired,
    ToSGateResult,
    ToSHashUnavailable,
    check_tos_acknowledged,
    hash_tos_text,
    record_acknowledgement,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)

_TOS_BODY_V1 = "Polymarket Terms of Service v1\n\nDoing things, etc."
_TOS_BODY_V2 = "Polymarket Terms of Service v2\n\nDoing different things now."


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "tos_gate.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


class _StaticBodyClient:
    """Minimal httpx-like client that returns a canned body for any GET."""

    def __init__(self, body: str, status_code: int = 200) -> None:
        self._body = body
        self._status = status_code

    def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        del url, timeout
        request = httpx.Request("GET", "https://polymarket.com/tos")
        return httpx.Response(self._status, request=request, content=self._body.encode("utf-8"))


class _RaisingClient:
    """Client that always raises an httpx error to simulate network outage."""

    def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        del url, timeout
        raise httpx.ConnectError("simulated connect failure")


def test_hash_is_stable_for_canonical_text() -> None:
    h1 = hash_tos_text(_TOS_BODY_V1)
    h2 = hash_tos_text("  " + _TOS_BODY_V1 + "\n  ")
    assert h1 == h2  # leading/trailing whitespace stripped before hashing


def test_refusal_when_no_prior_ack(store: DuckDBStore) -> None:
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn, pytest.raises(ToSAcknowledgementRequired) as exc_info:
        check_tos_acknowledged(conn, client=client)
    assert exc_info.value.tos_version_hash == hash_tos_text(_TOS_BODY_V1)
    assert "polymarket" in exc_info.value.tos_url.lower()


def test_pass_after_ack(store: DuckDBStore) -> None:
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash)
        result = check_tos_acknowledged(conn, client=client)
    assert isinstance(result, ToSGateResult)
    assert result.tos_version_hash == expected_hash
    assert result.used_fallback is False
    assert result.acknowledged_at is not None


def test_re_prompt_on_hash_change(store: DuckDBStore) -> None:
    """When the live ToS hash differs from the recorded ack, refuse and re-prompt."""
    old_hash = hash_tos_text(_TOS_BODY_V1)
    new_hash = hash_tos_text(_TOS_BODY_V2)
    client_v2 = _StaticBodyClient(_TOS_BODY_V2)

    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=old_hash)
        with pytest.raises(ToSAcknowledgementRequired) as exc_info:
            check_tos_acknowledged(conn, client=client_v2)

    assert exc_info.value.tos_version_hash == new_hash
    assert exc_info.value.tos_version_hash != old_hash


def test_fallback_to_last_known_when_live_fetch_fails(store: DuckDBStore) -> None:
    """Live fetch fails, last-known hash matches recorded ack → pass."""
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    seeding_client = _StaticBodyClient(_TOS_BODY_V1)
    failing_client = _RaisingClient()

    with store.connection() as conn:
        # First successful run records the live hash and the ack.
        record_acknowledgement(conn, tos_version_hash=expected_hash)
        first = check_tos_acknowledged(conn, client=seeding_client)
        assert first.used_fallback is False

        # Second run with failing live fetch falls back to the recorded
        # last-known hash, which still matches the ack.
        second = check_tos_acknowledged(conn, client=failing_client)
        assert second.used_fallback is True
        assert second.tos_version_hash == expected_hash


def test_no_live_no_lastknown_refuses(store: DuckDBStore) -> None:
    failing_client = _RaisingClient()
    with store.connection() as conn:
        # Make sure the history table is empty.
        rows = conn.execute("SELECT COUNT(*) FROM polymarket_tos_version_history").fetchone()
        assert rows is not None
        assert rows[0] == 0

        with pytest.raises(ToSHashUnavailable, match="could not be fetched"):
            check_tos_acknowledged(conn, client=failing_client)


def test_record_acknowledgement_is_idempotent(store: DuckDBStore) -> None:
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    when_first = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    when_second = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)

    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash, when=when_first)
        record_acknowledgement(conn, tos_version_hash=expected_hash, when=when_second)
        rows = conn.execute(
            "SELECT license_terms_hash, license_acknowledged_at "
            "FROM sources WHERE source_id = 'polymarket'"
        ).fetchone()

    assert rows is not None
    assert rows[0] == expected_hash
    assert rows[1] == when_second  # most recent ack wins (idempotent overwrite)


def test_history_table_records_each_observed_hash(store: DuckDBStore) -> None:
    expected_hash_v1 = hash_tos_text(_TOS_BODY_V1)
    expected_hash_v2 = hash_tos_text(_TOS_BODY_V2)
    client_v1 = _StaticBodyClient(_TOS_BODY_V1)
    client_v2 = _StaticBodyClient(_TOS_BODY_V2)

    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash_v1)
        check_tos_acknowledged(conn, client=client_v1)
        # Now ToS rev'd — gate refuses but should still record the new hash.
        with pytest.raises(ToSAcknowledgementRequired):
            check_tos_acknowledged(conn, client=client_v2)
        rows = conn.execute(
            "SELECT tos_version_hash FROM polymarket_tos_version_history ORDER BY first_seen_at"
        ).fetchall()

    hashes = {r[0] for r in rows}
    assert expected_hash_v1 in hashes
    assert expected_hash_v2 in hashes


def test_fetch_current_tos_hash_uses_provided_client() -> None:
    """The fetch helper consumes a caller-provided client without owning it."""
    client = _StaticBodyClient(_TOS_BODY_V1)
    from razor_rooster.polymarket_connector.gates.tos import (
        fetch_current_tos_hash,
    )

    h = fetch_current_tos_hash(client=client)  # type: ignore[arg-type]
    assert h == hash_tos_text(_TOS_BODY_V1)


def test_acknowledgement_required_carries_actionable_fields(store: DuckDBStore) -> None:
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn, pytest.raises(ToSAcknowledgementRequired) as exc_info:
        check_tos_acknowledged(conn, client=client)
    assert exc_info.value.cli_command == "razor-rooster polymarket ack-tos"
    assert exc_info.value.tos_url.endswith("/tos")
    # The exception's message includes the actionable command for the operator.
    assert "ack-tos" in str(exc_info.value)


def test_passes_when_explicit_now_used(store: DuckDBStore) -> None:
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    client = _StaticBodyClient(_TOS_BODY_V1)
    when = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash, when=when)
        result = check_tos_acknowledged(conn, client=client, now=when)
    assert result.acknowledged_at == when
