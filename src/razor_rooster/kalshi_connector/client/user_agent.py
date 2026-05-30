"""User-Agent string and shared httpx client factory (T-KSI-032; NFR-KSI-TOS-001).

Kalshi's Terms request that automated clients identify themselves
clearly. The User-Agent string follows the convention::

    razor-rooster-kalshi/<connector-version> (research; +<contact>)

The ``research`` token signals this is a non-commercial research
client; Kalshi's automation-friendliness is conditional on that
posture. The contact suffix is optional; operators set it via the
``KALSHI_CONTACT`` env var if they want their identifier reachable.
The connector version is the package's ``__version__`` so the UA
tracks release boundaries automatically.

The factory returns an ``httpx.Client`` configured with:

- The User-Agent header.
- A default timeout (per-request override allowed).
- A no-op base URL so callers can pass full URLs to ``get`` / ``post``.

Rate limiting and retry are layered separately by the per-API client
modules so this factory stays focused on transport.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

from razor_rooster import __version__

logger = logging.getLogger(__name__)


# Default timeout (seconds) applied to httpx clients. Overridable via
# the ``timeout_seconds`` kwarg.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

# Env var that lets the operator inject a contact identifier into the UA.
_CONTACT_ENV: Final[str] = "KALSHI_CONTACT"


def build_user_agent(*, contact: str | None = None) -> str:
    """Construct the User-Agent string for Kalshi HTTP calls.

    Resolution order: explicit ``contact`` argument > ``KALSHI_CONTACT``
    env var > no contact suffix.
    """
    if contact is None:
        env_contact = os.environ.get(_CONTACT_ENV)
        if env_contact and env_contact.strip():
            contact = env_contact.strip()
    base = f"razor-rooster-kalshi/{__version__}"
    if contact:
        if any(c in contact for c in "\r\n"):
            raise ValueError(f"KALSHI_CONTACT must not contain CR/LF, got {contact!r}")
        return f"{base} (research; +{contact})"
    return f"{base} (research)"


def build_httpx_client(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    contact: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Construct an ``httpx.Client`` with Kalshi-aware defaults.

    The returned client is the caller's to manage. Use as a context
    manager or call ``close()`` explicitly. The factory does not bind
    the client to a base URL — callers pass full URLs to
    ``get`` / ``post``.

    The factory deliberately omits the rate limiter and retry harness;
    the per-API client wrappers (T-KSI-033) layer those in. Mixing them
    here would force every call to participate in limiting even when a
    test wants to isolate transport behavior.
    """
    headers = {"User-Agent": build_user_agent(contact=contact)}
    if extra_headers:
        for key, value in extra_headers.items():
            if any(c in key + value for c in "\r\n"):
                raise ValueError(f"extra header {key!r} contains CR/LF; refusing to send")
            headers[key] = value
    return httpx.Client(
        timeout=timeout_seconds,
        headers=headers,
        follow_redirects=True,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "build_httpx_client",
    "build_user_agent",
]
