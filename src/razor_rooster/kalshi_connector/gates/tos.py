"""Kalshi ToS acknowledgement gate (T-KSI-021; REQ-KSI-TOS-001 / REQ-KSI-TOS-002).

Verifies the operator has acknowledged the current Kalshi Terms of
Service before any Kalshi sync runs. Mirrors the Polymarket pattern
with two Kalshi-specific changes:

1. The acknowledgement record explicitly carries the
   ``acknowledged_posture='read_only'`` value (per REQ-KSI-TOS-002). v2
   trading work will require a separate acknowledgement with
   ``acknowledged_posture='trading'``.
2. The ToS URL defaults to ``https://kalshi.com/docs/kalshi-terms-of-service``
   but is operator-updateable via ``config/kalshi.yaml`` (the Polymarket
   gate hard-codes the URL because Polymarket's path is more stable).

Network resilience: if the canonical URL is unreachable, the gate falls
back to the last-known hash recorded in ``kalshi_tos_version_history``.
If neither matches the recorded acknowledgement, the gate refuses the
connector and instructs the operator to re-acknowledge.

The gate writes nothing on success; the ``ack-tos`` CLI subcommand is
the only legitimate writer of new acknowledgements via
:func:`record_acknowledgement`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import duckdb
import httpx

from razor_rooster.data_ingest.persistence.provenance import (
    AcknowledgedPosture,
    get_license_posture,
    record_license_acknowledgement,
)
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
)

logger = logging.getLogger(__name__)


# Default Kalshi ToS URL. Operators can override via ``config/kalshi.yaml``;
# the gate accepts the URL as an argument so the CLI can pass the
# config-loaded value.
DEFAULT_KALSHI_TOS_URL: Final[str] = "https://kalshi.com/docs/kalshi-terms-of-service"


# Default network timeout for the ToS fetch. Short enough that we don't
# block startup, long enough to tolerate normal latency.
_DEFAULT_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0

# v1 connector posture. v2 trading work will introduce 'trading'.
_KALSHI_V1_POSTURE: Final[AcknowledgedPosture] = "read_only"


class ToSGateError(RuntimeError):
    """Base class for Kalshi ToS gate failures."""


class ToSAcknowledgementRequired(ToSGateError):
    """Raised when no current acknowledgement is on record.

    Attributes:
        tos_version_hash: The hash the gate verified is current. The
            ``ack-tos`` CLI uses this when prompting the operator.
        tos_url: The URL the operator should review.
        cli_command: The exact command the operator should run.
    """

    def __init__(
        self,
        *,
        tos_version_hash: str,
        tos_url: str,
        cli_command: str = "razor-rooster kalshi ack-tos",
    ) -> None:
        super().__init__(
            f"Kalshi ToS acknowledgement is required. Run {cli_command!r} "
            f"after reviewing the current Terms at {tos_url}."
        )
        self.tos_version_hash = tos_version_hash
        self.tos_url = tos_url
        self.cli_command = cli_command


class ToSPostureMismatch(ToSGateError):
    """Raised when the recorded acknowledgement is for the wrong posture.

    v1 only operates in ``read_only`` posture. An acknowledgement
    recorded for ``trading`` posture (a v2 concept) is refused so the
    operator does not accidentally enable read-only sync under a
    trading-grade authorization.

    Attributes:
        recorded_posture: The posture currently recorded on the
            ``kalshi`` source row.
        required_posture: The posture v1 requires (``'read_only'``).
        cli_command: The exact command the operator should run to
            re-acknowledge with the correct posture.
    """

    def __init__(
        self,
        *,
        recorded_posture: str | None,
        required_posture: AcknowledgedPosture = _KALSHI_V1_POSTURE,
        cli_command: str = "razor-rooster kalshi ack-tos",
    ) -> None:
        recorded_repr = repr(recorded_posture) if recorded_posture is not None else "(unset)"
        super().__init__(
            f"Kalshi ToS acknowledgement is for posture {recorded_repr} but "
            f"v1 requires {required_posture!r}. Re-run {cli_command!r} to "
            "acknowledge under the read-only posture."
        )
        self.recorded_posture = recorded_posture
        self.required_posture = required_posture
        self.cli_command = cli_command


class ToSHashUnavailable(ToSGateError):
    """Raised when neither a live fetch nor a last-known hash is available."""


@dataclass(frozen=True, slots=True)
class ToSGateResult:
    """Outcome of a successful gate check.

    Attributes:
        tos_version_hash: The hash currently in force.
        used_fallback: True if the gate accepted the last-known hash from
            ``kalshi_tos_version_history`` because the live URL was
            unreachable.
        acknowledged_at: When the operator's recorded acknowledgement
            was written.
        acknowledged_posture: The posture the acknowledgement was
            recorded under (always ``'read_only'`` for v1).
    """

    tos_version_hash: str
    used_fallback: bool
    acknowledged_at: datetime
    acknowledged_posture: AcknowledgedPosture


def hash_tos_text(text: str) -> str:
    """Return the SHA-256 hex digest of the canonical (stripped) ToS text."""
    canonical = text.strip().encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def fetch_current_tos_hash(
    *,
    url: str = DEFAULT_KALSHI_TOS_URL,
    timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> str:
    """Fetch the canonical ToS document and return its SHA-256 hash.

    Raises :class:`httpx.HTTPError` (or subclasses) on network failure;
    callers in the gate path catch and fall back to the last-known hash.
    """
    if client is None:
        with httpx.Client(timeout=timeout_seconds) as ephemeral:
            response = ephemeral.get(url)
            response.raise_for_status()
            return hash_tos_text(response.text)
    response = client.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return hash_tos_text(response.text)


def _last_known_hash(conn: duckdb.DuckDBPyConnection) -> str | None:
    row = conn.execute(
        "SELECT tos_version_hash FROM kalshi_tos_version_history ORDER BY last_seen_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _record_observed_hash(
    conn: duckdb.DuckDBPyConnection,
    *,
    tos_version_hash: str,
    tos_url: str,
    when: datetime,
) -> None:
    """Insert or update the ``kalshi_tos_version_history`` entry."""
    existing = conn.execute(
        "SELECT 1 FROM kalshi_tos_version_history WHERE tos_version_hash = ?",
        [tos_version_hash],
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO kalshi_tos_version_history "
            "(tos_version_hash, tos_url, first_seen_at, last_seen_at, notes) "
            "VALUES (?, ?, ?, ?, NULL)",
            [tos_version_hash, tos_url, when, when],
        )
    else:
        conn.execute(
            "UPDATE kalshi_tos_version_history SET last_seen_at = ? WHERE tos_version_hash = ?",
            [when, tos_version_hash],
        )


def check_tos_acknowledged(
    conn: duckdb.DuckDBPyConnection,
    *,
    url: str = DEFAULT_KALSHI_TOS_URL,
    timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    required_posture: AcknowledgedPosture = _KALSHI_V1_POSTURE,
) -> ToSGateResult:
    """Verify the operator has acknowledged the current Kalshi ToS.

    Steps:

    1. Try to fetch and hash the live ToS. On success, record the hash
       in ``kalshi_tos_version_history`` (insert-or-update), and use it
       as the current hash.
    2. On fetch failure, fall back to the most recently observed hash
       in ``kalshi_tos_version_history``.
    3. Compare the current hash to the operator's recorded
       ``license_terms_hash`` on the ``kalshi`` source row.
    4. Verify the recorded ``acknowledged_posture`` matches
       ``required_posture`` (default ``'read_only'`` for v1).
    5. Match → success. Hash mismatch or no recorded ack →
       :class:`ToSAcknowledgementRequired`. Posture mismatch →
       :class:`ToSPostureMismatch`.

    Raises :class:`ToSHashUnavailable` if both the live fetch and the
    fallback fail (no last-known hash on file).
    """
    when = now or datetime.now(tz=UTC)

    live_hash: str | None
    used_fallback = False
    try:
        live_hash = fetch_current_tos_hash(url=url, timeout_seconds=timeout_seconds, client=client)
        logger.info("fetched live Kalshi ToS hash %s", live_hash[:12])
    except httpx.HTTPError as exc:
        logger.warning(
            "could not fetch live Kalshi ToS (%s); falling back to last-known hash",
            exc,
        )
        live_hash = None

    if live_hash is not None:
        _record_observed_hash(conn, tos_version_hash=live_hash, tos_url=url, when=when)
        current_hash = live_hash
    else:
        last_known = _last_known_hash(conn)
        if last_known is None:
            raise ToSHashUnavailable(
                "Kalshi ToS could not be fetched and no last-known hash is "
                "on file. Confirm network access to kalshi.com and retry."
            )
        current_hash = last_known
        used_fallback = True

    posture = get_license_posture(conn, KALSHI_LIVE_SOURCE_ID)
    if posture is None:
        raise ToSAcknowledgementRequired(tos_version_hash=current_hash, tos_url=url)
    if posture.license_terms_hash != current_hash:
        raise ToSAcknowledgementRequired(tos_version_hash=current_hash, tos_url=url)
    if posture.license_acknowledged_at is None:
        raise ToSAcknowledgementRequired(tos_version_hash=current_hash, tos_url=url)
    if posture.acknowledged_posture != required_posture:
        raise ToSPostureMismatch(
            recorded_posture=posture.acknowledged_posture,
            required_posture=required_posture,
        )

    return ToSGateResult(
        tos_version_hash=current_hash,
        used_fallback=used_fallback,
        acknowledged_at=posture.license_acknowledged_at,
        acknowledged_posture=posture.acknowledged_posture,
    )


def record_acknowledgement(
    conn: duckdb.DuckDBPyConnection,
    *,
    tos_version_hash: str,
    when: datetime | None = None,
    posture: AcknowledgedPosture = _KALSHI_V1_POSTURE,
) -> None:
    """Record an operator acknowledgement of the current Kalshi ToS.

    Called by ``razor-rooster kalshi ack-tos`` after the operator
    confirms they've read the Terms. Writes the hash, timestamp, and
    posture to the ``kalshi`` source row via the shared provenance
    helper.

    Idempotent: re-acknowledging the same hash + posture refreshes the
    timestamp.
    """
    record_license_acknowledgement(
        conn,
        source_id=KALSHI_LIVE_SOURCE_ID,
        terms_hash=tos_version_hash,
        when=when,
        commercial_use_recorded_grant=False,
        acknowledged_posture=posture,
    )


__all__ = [
    "DEFAULT_KALSHI_TOS_URL",
    "ToSAcknowledgementRequired",
    "ToSGateError",
    "ToSGateResult",
    "ToSHashUnavailable",
    "ToSPostureMismatch",
    "check_tos_acknowledged",
    "fetch_current_tos_hash",
    "hash_tos_text",
    "record_acknowledgement",
]
