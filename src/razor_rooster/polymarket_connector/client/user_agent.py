"""User-Agent string and shared httpx client factory (T-PMC-032; NFR-PMC-TOS-001).

Polymarket's Terms request that automated clients identify themselves
clearly. The User-Agent string follows the convention::

    razor-rooster-polymarket/<connector-version> (+<contact-url-or-email>)

The contact suffix is optional; operators set it via the
``POLYMARKET_CONTACT`` env var if they want their identifier reachable.
The connector version is the package's ``__version__`` so the UA tracks
release boundaries automatically.

The factory returns an ``httpx.Client`` configured with:

- The User-Agent header.
- A default timeout (per-request override allowed).
- A no-op base URL so callers can pass full URLs to ``get`` / ``post``.

Rate-limiting and retry are layered separately by the per-API client
modules (Gamma, CLOB) so this factory stays focused on transport.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

from razor_rooster import __version__

logger = logging.getLogger(__name__)


# Default timeout (seconds) applied to httpx clients. Overridable via the
# ``timeout`` kwarg on the client factory.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

# Env var that lets the operator inject a contact identifier into the UA.
# When set, the UA looks like ``razor-rooster-polymarket/0.1.0 (+ops@example.com)``.
_CONTACT_ENV: Final[str] = "POLYMARKET_CONTACT"


def build_user_agent(*, contact: str | None = None) -> str:
    """Construct the User-Agent string for Polymarket HTTP calls.

    Resolution order: explicit ``contact`` argument > ``POLYMARKET_CONTACT``
    env var > no contact suffix.
    """
    if contact is None:
        env_contact = os.environ.get(_CONTACT_ENV)
        if env_contact and env_contact.strip():
            contact = env_contact.strip()
    base = f"razor-rooster-polymarket/{__version__}"
    if contact:
        # Reject suspicious characters that could break header parsing.
        if any(c in contact for c in "\r\n"):
            raise ValueError(f"POLYMARKET_CONTACT must not contain CR/LF, got {contact!r}")
        return f"{base} (+{contact})"
    return base


def build_httpx_client(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    contact: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Client:
    """Construct an ``httpx.Client`` with the Polymarket-aware defaults.

    The returned client is the caller's to manage (use as a context
    manager or call ``close()`` explicitly). It does not lock the user
    into a base URL — callers pass full URLs to ``get``/``post``.

    The client does not include the rate limiter or retry logic; those
    are applied by the per-API client wrappers (T-PMC-033, T-PMC-034).
    Layering them at this level would force every call to participate
    even when the caller already handled limiting in a different way
    (e.g. tests mock the limiter).
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
