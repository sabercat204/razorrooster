"""WHO Disease Outbreak News (DON) connector (T-054).

Pulls disease outbreak announcements from the WHO DON RSS feed at
``https://www.who.int/feeds/entity/csr/don/en/rss.xml``.

The DON feed is unauthenticated and freely available. WHO does not
publish a per-IP rate limit; the connector applies a polite client-side
limiter at one request per 1.0 seconds. The feed itself is a single
endpoint that returns the most recent ~30 entries per fetch — there is
no pagination at the feed level.

Entries map to the ``event_stream`` canonical schema:

- ``event_ts`` is parsed from the RSS ``pubDate``.
- ``event_class`` is extracted from the title where possible (e.g.,
  "Cholera - Sudan" -> ``Cholera``).
- ``country_iso3`` is mapped from the title's country fragment via
  :func:`to_iso3` (T-031). Ambiguous or unrecognized country fragments
  return ``None``.
- ``description`` is the RSS ``description`` field, with HTML stripped
  conservatively. The full HTML body of the source-side page is not
  fetched in v1 (DEFER-004 equivalent).

Backfill is not supported in v1 — the RSS feed only exposes a rolling
window of recent entries. Historical archives exist but are not on the
RSS endpoint. Operators wanting historical depth should add an archive-
download path in v1.1.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final
from xml.etree import ElementTree as ET

import httpx

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    License,
    RateLimitedError,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.credentials import CredentialBundle
from razor_rooster.data_ingest.normalization.base import (
    EventStreamRecord,
    NormalizedRecord,
    RawRecord,
)
from razor_rooster.data_ingest.normalization.geo import to_iso3
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_DON_RSS_URL: Final[str] = "https://www.who.int/feeds/entity/csr/don/en/rss.xml"
_DON_TIMEOUT_SECONDS: Final[float] = 30.0
_DON_MIN_INTERVAL_SECONDS: Final[float] = 1.0
_DON_MAX_RETRIES: Final[int] = 5

# WHO titles use various separators (em dash, en dash, hyphen) between the
# disease name and the country. We normalize to whichever matches first.
_TITLE_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"\s+[\u2013\u2014\u2010\-]\s+")

# Strip HTML tags from RSS description fragments. Conservative: removes
# only opening/closing tag markers, leaves text. Not a full HTML parser.
_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@register
class WhoDonConnector(Connector):
    """Pulls WHO DON entries into the ``event_stream`` schema."""

    source_id = "who_don"
    title = "WHO Disease Outbreak News"
    canonical_schema = SchemaType.EVENT_STREAM
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "who_don@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
        feed_url: str | None = None,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_DON_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._feed_url = feed_url or _DON_RSS_URL

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        feed_xml = self._fetch_feed()
        for raw in self._parse_entries(feed_xml):
            if raw.source_publication_ts >= since:
                yield raw

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        title = payload.get("title", "")
        event_class, country_fragment = self._split_title(title)
        country_iso3 = to_iso3(country_fragment) if country_fragment else None

        description = self._clean_description(payload.get("description", ""))

        return EventStreamRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            event_ts=raw.source_publication_ts,
            country_iso3=country_iso3,
            actor_primary=None,
            actor_secondary=None,
            event_class=event_class,
            description=description,
        )

    # --- internals ---------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _DON_MIN_INTERVAL_SECONDS:
            time.sleep(_DON_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_feed(self) -> str:
        last_exc: Exception | None = None
        for attempt in range(_DON_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(self._feed_url)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "WHO DON request error attempt=%d error=%s",
                    attempt,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "WHO DON transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"WHO DON returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _DON_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"WHO DON rate-limited or transient-failed past {_DON_MAX_RETRIES} retries: {last_exc}"
        )

    def _parse_entries(self, feed_xml: str) -> Iterator[RawRecord]:
        try:
            root = ET.fromstring(feed_xml)
        except ET.ParseError as exc:
            logger.warning("WHO DON feed parse error: %s", exc)
            return

        # The feed is an RSS 2.0 document: <rss><channel><item>...</item></channel></rss>.
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()
            pub_date_raw = (item.findtext("pubDate") or "").strip()
            guid = (item.findtext("guid") or link).strip()

            pub_date_ts = self._parse_pub_date(pub_date_raw)
            payload: dict[str, Any] = {
                "title": title,
                "link": link,
                "description": description,
                "pubDate": pub_date_raw,
                "guid": guid,
            }
            yield RawRecord(
                source_id=self.source_id,
                source_record_id=guid or link or title,
                source_payload_json=payload,
                source_publication_ts=pub_date_ts,
            )

    def _parse_pub_date(self, value: str) -> datetime:
        if not value:
            return datetime.now(tz=UTC)
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return datetime.now(tz=UTC)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _split_title(self, title: str) -> tuple[str | None, str | None]:
        """Split a DON title like 'Cholera - Sudan' into (disease, country).

        Returns ``(disease, country)`` when the separator is present, or
        ``(title, None)`` when not. Either field can be ``None``.
        """
        if not title:
            return None, None
        match = _TITLE_SEPARATOR_RE.search(title)
        if match is None:
            return title, None
        disease = title[: match.start()].strip()
        rest = title[match.end() :].strip()
        return disease or None, rest or None

    def _clean_description(self, description: str) -> str:
        without_tags = _HTML_TAG_RE.sub(" ", description)
        normalized_ws = _WHITESPACE_RE.sub(" ", without_tags).strip()
        return normalized_ws
