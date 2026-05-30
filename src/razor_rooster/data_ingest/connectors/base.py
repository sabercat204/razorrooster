"""Connector ABC and shared fetch infrastructure (T-030).

Defines the contract every per-source connector implements:

- :class:`Connector` — abstract base class with ``fetch_incremental``,
  ``fetch_backfill``, ``normalize``, and ``health_check`` methods.
- :class:`License` — enumeration of license postures the connector can carry.
- :class:`ResumeToken` — opaque marker passed through ``fetch_backfill`` so
  connectors can resume after interruption.
- :class:`ConnectorHealth` — typed result of ``health_check``.
- :class:`ConnectorOutcome` — typed result of a per-cycle run.
- :func:`run_incremental` — uniform entry point that wraps a connector's
  incremental run with failure isolation, structured logging, and
  per-source last-fetch updates.
- :func:`exponential_backoff_with_jitter` — the default retry sleep schedule
  used by rate-limit-aware connectors (REQ-SRC-002).

The ABC takes a :class:`DuckDBStore` and a credential bundle (or ``None`` for
unauthenticated sources) at construction. Subclasses are expected to be
small — most of the per-source code is in ``fetch_incremental`` and
``normalize``; everything else (rate limiting, retries, persistence,
provenance) is composed.

Concurrency: ``run_incremental`` is safe to call from multiple threads
provided each invocation targets a different connector. Calling it
concurrently with the same connector instance is undefined behavior.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from razor_rooster.data_ingest.credentials import CredentialBundle
from razor_rooster.data_ingest.normalization.base import NormalizedRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType

logger = logging.getLogger(__name__)


class License(StrEnum):
    """License postures the connector can declare.

    The values match the strings written into ``sources.license``. Sources
    that haven't completed acknowledgement land on ``UNKNOWN`` until their
    startup gate writes the canonical posture.
    """

    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    CC_BY = "CC_BY"
    CC_BY_NC = "CC_BY_NC"
    CC_BY_SA = "CC_BY_SA"
    ACLED_TERMS_VERSIONED = "ACLED_TERMS_VERSIONED"
    POLYMARKET_TERMS_VERSIONED = "POLYMARKET_TERMS_VERSIONED"
    TERMS_OF_SERVICE = "TERMS_OF_SERVICE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ResumeToken:
    """Opaque resume marker passed through ``fetch_backfill``.

    A connector that supports backfill produces tokens at points where
    resumption is safe (typically after a successful page commit). The
    backfill orchestrator (T-034) persists the token in ``backfill_state``
    after each batch. ``None`` means "start from the beginning."
    """

    value: str


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """Typed result of a connector's ``health_check`` call.

    ``ok`` reflects whether the source is currently reachable and the
    connector's auth (if any) is valid. ``latency_ms`` is the round-trip
    time of the probe call; ``message`` is a short human-readable note
    suitable for the cycle report.
    """

    source_id: str
    ok: bool
    latency_ms: float
    message: str | None = None


@dataclass(slots=True)
class ConnectorOutcome:
    """Result of one connector's run within a cycle (REQ-LOG-001 §6.1)."""

    source_id: str
    status: str  # 'ok' | 'partial' | 'failed' | 'skipped'
    records_ingested: int = 0
    records_skipped_duplicate: int = 0
    duration_seconds: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)


class ConnectorError(RuntimeError):
    """Base class for connector-side failures."""


class CredentialMissingError(ConnectorError):
    """Raised when a connector requires credentials and none are loaded."""


class RateLimitedError(ConnectorError):
    """Raised when the source consistently rate-limits past the retry budget."""


def exponential_backoff_with_jitter(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    max_seconds: float = 60.0,
    jitter: float = 0.25,
) -> float:
    """Return the sleep duration for a given retry attempt (0-indexed).

    Computes ``base_seconds * 2**attempt`` capped at ``max_seconds``, then
    multiplies by a uniform jitter factor in ``[1-jitter, 1+jitter]``. The
    jitter is bounded so callers can reason about worst-case wait times
    without consulting the random number generator directly.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if base_seconds < 0:
        raise ValueError("base_seconds must be >= 0")
    if max_seconds < base_seconds:
        raise ValueError("max_seconds must be >= base_seconds")
    if not 0 <= jitter < 1:
        raise ValueError("jitter must be in [0, 1)")
    capped: float = min(base_seconds * (2**attempt), max_seconds)
    factor: float = 1.0 + random.uniform(-jitter, jitter)
    return capped * factor


class Connector(ABC):
    """Abstract base class for ingestion connectors (design §3.2).

    Subclasses must implement :meth:`fetch_incremental`, :meth:`normalize`,
    and (where applicable) :meth:`fetch_backfill`. ``health_check`` has a
    default implementation that subclasses can override.

    The ABC's ``__init__`` validates that the connector has the credentials
    it requires; subclasses with no auth pass ``credentials=None`` and the
    check is skipped.
    """

    #: Unique source identifier matching ``sources.source_id``.
    source_id: str
    #: Human-readable label for log messages and reports.
    title: str
    #: Which canonical schema this connector writes to.
    canonical_schema: SchemaType
    #: License posture written into ``sources.license`` on registration.
    license: License
    #: Default cadence ('daily' | 'weekly' | 'monthly' | 'annual').
    cadence_default: str
    #: Whether the connector implements ``fetch_backfill``.
    backfill_supported: bool
    #: Connector-defined version string (e.g. "fred@0.1.0").
    connector_version: str
    #: Whether the source's data is non-commercial-use only by default.
    license_noncommercial_required: bool = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
    ) -> None:
        if not getattr(self, "source_id", ""):
            raise TypeError(f"{type(self).__name__} must declare class attribute 'source_id'")
        if not getattr(self, "title", ""):
            raise TypeError(f"{type(self).__name__} must declare class attribute 'title'")
        if not isinstance(getattr(self, "canonical_schema", None), SchemaType):
            raise TypeError(
                f"{type(self).__name__} must declare class attribute 'canonical_schema' "
                "(a SchemaType value)"
            )
        if not isinstance(getattr(self, "license", None), License):
            raise TypeError(
                f"{type(self).__name__} must declare class attribute 'license' "
                "(a License enum value)"
            )
        self.store = store
        self.credentials = credentials

    @abstractmethod
    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        """Pull records published on or after ``since``.

        Yields :class:`RawRecord` instances; the orchestrator passes each
        through :meth:`normalize` before persistence. Subclasses are
        responsible for pagination and rate-limit-aware retry within this
        method.
        """

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        """Pull historical records up to ``until``, resumable via ``resume_token``.

        Yields tuples of ``(record, resume_token_for_next_batch)``; the
        backfill orchestrator (T-034) persists the token after each batch
        commit. Default implementation raises :class:`NotImplementedError`
        because connectors that don't support backfill (i.e.
        ``backfill_supported`` is False) must not silently return an empty
        iterator. Subclasses that do support backfill must override this.
        """
        if not self.backfill_supported:
            raise NotImplementedError(
                f"{type(self).__name__} does not support backfill; "
                "set backfill_supported = True and override fetch_backfill"
            )
        raise NotImplementedError(
            f"{type(self).__name__}.fetch_backfill must be overridden when "
            "backfill_supported is True"
        )
        # The unreachable yield below is what makes this method a generator
        # rather than a plain function; without it, the return-type annotation
        # would be a lie. ``if False`` keeps mypy happy under strict mode.
        if False:
            yield  # pragma: no cover

    @abstractmethod
    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        """Convert a source-native record to its canonical-schema form.

        Subclasses must not impute or correct missing values; preserve the
        source-native payload verbatim in ``source_payload_json`` and only
        populate the canonical columns that the source provides.
        """

    def health_check(self) -> ConnectorHealth:
        """Default health check returns ``ok=True`` if construction succeeded.

        Subclasses with a network endpoint should override this to perform
        a lightweight probe (e.g., a metadata-only API call) and report
        latency.
        """
        return ConnectorHealth(
            source_id=self.source_id,
            ok=True,
            latency_ms=0.0,
            message="default health check (no probe)",
        )


def run_incremental(
    connector: Connector,
    *,
    since: datetime,
    persister: Callable[[Iterator[NormalizedRecord]], int],
) -> ConnectorOutcome:
    """Run a connector's incremental fetch with uniform failure isolation.

    The cycle scheduler (T-033) calls this for each due connector. Failures
    are captured in the outcome's ``errors`` list rather than propagated;
    the cycle continues with the remaining connectors.

    ``persister`` is the callable that consumes normalized records and
    writes them to the canonical table via the staging-merge pattern. It
    returns the number of records ingested. The orchestrator owns the
    persistence callable so connectors don't need to know about DuckDB.
    """
    started = time.monotonic()
    outcome = ConnectorOutcome(source_id=connector.source_id, status="ok")
    try:
        normalized = (connector.normalize(raw) for raw in connector.fetch_incremental(since))
        outcome.records_ingested = persister(normalized)
    except CredentialMissingError as exc:
        outcome.status = "skipped"
        outcome.errors.append({"type": "credential_missing", "message": str(exc)})
        logger.warning(
            "connector %s skipped: %s",
            connector.source_id,
            exc,
            extra={"source_id": connector.source_id},
        )
    except RateLimitedError as exc:
        outcome.status = "failed"
        outcome.errors.append({"type": "rate_limit_exhausted", "message": str(exc)})
        logger.exception(
            "connector %s rate-limited past retry budget",
            connector.source_id,
            extra={"source_id": connector.source_id},
        )
    except Exception as exc:
        outcome.status = "failed"
        outcome.errors.append({"type": type(exc).__name__, "message": str(exc)})
        logger.exception(
            "connector %s failed",
            connector.source_id,
            extra={"source_id": connector.source_id},
        )
    finally:
        outcome.duration_seconds = time.monotonic() - started
    return outcome
