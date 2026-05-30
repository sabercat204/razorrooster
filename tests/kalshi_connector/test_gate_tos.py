"""T-KSI-021 — Kalshi ToS acknowledgement gate acceptance tests.

Verifies:
- No prior ack → ToSAcknowledgementRequired with current hash + URL.
- Matching hash + read_only posture → success.
- Hash drift → re-prompt with new hash; old ack does not apply.
- Posture mismatch (e.g., 'trading' acknowledged but v1 requires
  'read_only') → ToSPostureMismatch.
- Live fetch fails + last-known matches → success with used_fallback=True.
- Live fetch fails + no last-known → ToSHashUnavailable.
- record_acknowledgement persists with the read_only posture.
- ack-tos rerun on new hash captures the new entry in
  kalshi_tos_version_history.
- The CLI command surfaced in the exception is razor-rooster kalshi
  ack-tos.
"""

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
from razor_rooster.kalshi_connector.gates.tos import (
    DEFAULT_KALSHI_TOS_URL,
    ToSAcknowledgementRequired,
    ToSGateResult,
    ToSHashUnavailable,
    ToSPostureMismatch,
    check_tos_acknowledged,
    hash_tos_text,
    record_acknowledgement,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)

_TOS_BODY_V1 = "Kalshi Terms of Service v1\nThis is the canonical text."
_TOS_BODY_V2 = "Kalshi Terms of Service v2\nThis is the revised canonical text."


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_tos.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


class _StaticBodyClient:
    """Minimal httpx-compatible client returning fixed body text.

    Mimics ``httpx.Client.get`` so the gate can run against synthetic
    ToS text without a network round-trip.
    """

    def __init__(self, body: str, status_code: int = 200) -> None:
        self._body = body
        self._status_code = status_code

    def get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        return httpx.Response(
            status_code=self._status_code,
            content=self._body.encode("utf-8"),
            request=httpx.Request("GET", url),
        )


class _FailingClient:
    """Client whose every request raises a network error."""

    def get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=httpx.Request("GET", url))


# -- core gate behavior ----------------------------------------------------


def test_no_ack_raises_acknowledgement_required(store: DuckDBStore) -> None:
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn, pytest.raises(ToSAcknowledgementRequired) as exc_info:
        check_tos_acknowledged(conn, client=client)
    assert exc_info.value.tos_version_hash == hash_tos_text(_TOS_BODY_V1)
    assert "kalshi" in exc_info.value.tos_url.lower()
    assert exc_info.value.cli_command == "razor-rooster kalshi ack-tos"


def test_matching_hash_and_read_only_posture_succeeds(store: DuckDBStore) -> None:
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash)
        result = check_tos_acknowledged(conn, client=client)
    assert isinstance(result, ToSGateResult)
    assert result.tos_version_hash == expected_hash
    assert result.used_fallback is False
    assert result.acknowledged_posture == "read_only"


def test_hash_drift_refuses_old_ack(store: DuckDBStore) -> None:
    old_hash = hash_tos_text(_TOS_BODY_V1)
    new_hash = hash_tos_text(_TOS_BODY_V2)
    client_v2 = _StaticBodyClient(_TOS_BODY_V2)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=old_hash)
        with pytest.raises(ToSAcknowledgementRequired) as exc_info:
            check_tos_acknowledged(conn, client=client_v2)
    assert exc_info.value.tos_version_hash == new_hash


def test_posture_mismatch_refuses(store: DuckDBStore) -> None:
    """An ack recorded for 'trading' (v2) is refused under v1."""
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn:
        record_acknowledgement(
            conn,
            tos_version_hash=expected_hash,
            posture="trading",
        )
        with pytest.raises(ToSPostureMismatch) as exc_info:
            check_tos_acknowledged(conn, client=client)
    assert exc_info.value.recorded_posture == "trading"
    assert exc_info.value.required_posture == "read_only"


def test_live_fetch_fails_falls_back_to_last_known(store: DuckDBStore) -> None:
    """When the live fetch fails, the gate accepts the last-known hash."""
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    seeding_client = _StaticBodyClient(_TOS_BODY_V1)
    failing_client = _FailingClient()

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


def test_live_fetch_fails_no_last_known_raises_unavailable(
    store: DuckDBStore,
) -> None:
    failing_client = _FailingClient()
    with store.connection() as conn:
        # No prior live fetch → no last-known hash. Even with an ack,
        # the gate cannot establish the current ToS.
        record_acknowledgement(conn, tos_version_hash="some-hash")

        with pytest.raises(ToSHashUnavailable, match="could not be fetched"):
            check_tos_acknowledged(conn, client=failing_client)


def test_record_acknowledgement_persists_read_only_posture(store: DuckDBStore) -> None:
    """record_acknowledgement writes acknowledged_posture='read_only'."""
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    when = datetime(2026, 5, 16, 12, tzinfo=UTC)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash, when=when)
        row = conn.execute(
            "SELECT license_terms_hash, license_acknowledged_at, "
            "acknowledged_posture FROM sources WHERE source_id = ?",
            ["kalshi"],
        ).fetchone()
    assert row is not None
    assert row[0] == expected_hash
    assert row[1] == when
    assert row[2] == "read_only"


def test_hash_change_records_history_entry(store: DuckDBStore) -> None:
    """Each new live hash records a row in kalshi_tos_version_history."""
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
            "SELECT tos_version_hash FROM kalshi_tos_version_history ORDER BY first_seen_at"
        ).fetchall()
    hashes = [r[0] for r in rows]
    assert expected_hash_v1 in hashes
    assert expected_hash_v2 in hashes


def test_acknowledgement_required_message_includes_cli_command(store: DuckDBStore) -> None:
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn, pytest.raises(ToSAcknowledgementRequired) as exc_info:
        check_tos_acknowledged(conn, client=client)
    assert exc_info.value.cli_command == "razor-rooster kalshi ack-tos"
    assert exc_info.value.tos_url == DEFAULT_KALSHI_TOS_URL


def test_ack_timestamp_round_trips(store: DuckDBStore) -> None:
    """The acknowledged_at returned matches what was recorded."""
    expected_hash = hash_tos_text(_TOS_BODY_V1)
    when = datetime(2026, 5, 16, 12, tzinfo=UTC)
    client = _StaticBodyClient(_TOS_BODY_V1)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash, when=when)
        result = check_tos_acknowledged(conn, client=client, now=when)
    assert result.acknowledged_at == when
