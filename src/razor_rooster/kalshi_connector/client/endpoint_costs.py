"""Per-endpoint token-cost map (T-KSI-030; design §3.6).

Kalshi's rate limiter charges a token cost per endpoint request. The
documented default is 10 tokens per request; the map's existence
shields the connector from future cost drift — when Kalshi changes a
cost, it's a config edit, not a code change.

Endpoint paths use Kalshi's documented placeholder syntax (e.g.
``/series/{series_ticker}``). Concrete request paths are matched by
substituting the placeholders out and looking up the resulting
template; see :func:`cost_for`.

The map is intentionally narrow to the v1 endpoint set
(REQ-KSI-SERIES/EVENT/MARKET/PRICE/OB/SETTLE/TRADE) plus
``/historical/*`` for cutoff routing. New endpoints land here only
when a sync or REST client surfaces them.
"""

from __future__ import annotations

import re
from typing import Final

# Default cost when no template matches. Matches Kalshi's documented
# default request cost. Falling back to the default rather than raising
# means a previously-undocumented endpoint still works; the operator
# learns about the gap through structured logs ("rate limiter using
# default cost for ..." appears in the limiter's telemetry).
DEFAULT_TOKEN_COST: Final[int] = 10


# Per-template token cost. Keys are Kalshi's documented endpoint paths
# with curly-brace placeholders. Production endpoints in v1 share the
# documented default of 10; the map lists them explicitly so a future
# update can change individual entries without touching code.
ENDPOINT_COSTS: Final[dict[str, int]] = {
    # series
    "/series": 10,
    "/series/{series_ticker}": 10,
    # events
    "/events": 10,
    "/events/{event_ticker}": 10,
    # markets
    "/markets": 10,
    "/markets/{ticker}": 10,
    "/markets/{ticker}/orderbook": 10,
    "/markets/{ticker}/candlesticks": 10,
    "/markets/trades": 10,
    # historical
    "/historical/cutoff": 10,
    "/historical/markets": 10,
    "/historical/markets/{ticker}": 10,
    "/historical/markets/{ticker}/candlesticks": 10,
    "/historical/trades": 10,
}


# Ordered list of (regex, template) tuples derived from
# ``ENDPOINT_COSTS``. Matching is longest-template-first so
# ``/markets/{ticker}/orderbook`` matches before ``/markets/{ticker}``.
def _compile_templates() -> list[tuple[re.Pattern[str], str]]:
    # Sort by (fewer placeholders first, then longer first). This makes
    # concrete paths beat parameterized ones — e.g.
    # ``/markets/trades`` (0 placeholders) is matched before
    # ``/markets/{ticker}`` (1 placeholder), and within the same
    # placeholder count, ``/markets/{ticker}/orderbook`` beats
    # ``/markets/{ticker}`` because it's longer.
    def _sort_key(template: str) -> tuple[int, int]:
        placeholder_count = template.count("{")
        return (placeholder_count, -len(template))

    sorted_templates = sorted(ENDPOINT_COSTS.keys(), key=_sort_key)
    compiled: list[tuple[re.Pattern[str], str]] = []
    for tmpl in sorted_templates:
        # Each {placeholder} becomes a non-empty character class that
        # excludes '/' so a one-segment ticker can't swallow further
        # path components.
        pattern_text = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", tmpl) + "$"
        compiled.append((re.compile(pattern_text), tmpl))
    return compiled


_TEMPLATE_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = _compile_templates()


def template_for_path(path: str) -> str | None:
    """Return the matching template for a concrete request path, or None.

    The path may begin with a base URL or with a leading ``/``; the
    matcher inspects only the URL path component starting at ``/``.

    Examples:
        ``/series`` -> ``/series``
        ``/series/INX`` -> ``/series/{series_ticker}``
        ``/markets/KXFOO/orderbook`` -> ``/markets/{ticker}/orderbook``
        ``/random/unknown`` -> ``None``
    """
    normalized = _normalize_path(path)
    for pattern, template in _TEMPLATE_PATTERNS:
        if pattern.match(normalized):
            return template
    return None


def cost_for(path: str) -> int:
    """Return the token cost for a concrete endpoint path.

    Falls back to :data:`DEFAULT_TOKEN_COST` for paths that do not match
    any template. The fallback is deliberately permissive so the
    connector keeps running through documentation drift, while the
    structured-log telemetry at the limiter's call site records the
    miss so operators can update the map.
    """
    template = template_for_path(path)
    if template is None:
        return DEFAULT_TOKEN_COST
    return ENDPOINT_COSTS[template]


# -- helpers ----------------------------------------------------------------


def _normalize_path(path: str) -> str:
    """Strip query string + leading host portion. Return path beginning at /."""
    # Drop fragment and query.
    for delim in ("#", "?"):
        if delim in path:
            path = path.split(delim, 1)[0]
    # If the input is a full URL, return only the path component.
    if "://" in path:
        path = path.split("://", 1)[1]
        path = "/" + path.split("/", 1)[1] if "/" in path else "/"
    if not path.startswith("/"):
        path = "/" + path
    # Strip a Kalshi base path prefix (``/trade-api/v2``) if present so
    # tests can supply either form.
    for prefix in ("/trade-api/v2", "/v2"):
        if path.startswith(prefix):
            path = path[len(prefix) :] or "/"
            break
    # Normalize multiple consecutive slashes.
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


__all__ = [
    "DEFAULT_TOKEN_COST",
    "ENDPOINT_COSTS",
    "cost_for",
    "template_for_path",
]
