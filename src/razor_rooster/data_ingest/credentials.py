"""Environment-variable credential loader (T-020).

Connectors that require authentication call :func:`load_credentials_for` at
startup. Credentials live exclusively in ``.env`` (read via ``python-dotenv``)
and are returned as typed bundles.

Two bundle shapes are supported in v1:

- :class:`ApiKeyBundle` — single token, used by FRED, EIA, NRC ADAMS,
  regulations.gov, NOAA CDO.
- :class:`UserPasswordBundle` — username + password, used by ACLED's OAuth
  password grant (REQ-ACLED-AUTH-001).

The loader follows three discipline rules:

1. **No persistence.** Credentials are returned in memory and never written
   to DuckDB, log files, or anywhere on disk by this module.
2. **No interpolation into URLs or formatted strings.** No general-purpose
   "format credentials" helper exists. Connectors put bundle values into
   request headers explicitly.
3. **Missing credentials return None.** Callers decide whether absence is
   skip-the-source (typical for optional sources) or fatal startup error.

Adding a new source's credentials is a code change here plus an env-var
documentation update; we do not auto-discover env vars.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApiKeyBundle:
    """A single API key.

    The key is in :attr:`api_key`; an optional :attr:`extra_token` is for
    sources that require a secondary token (e.g., NOAA CDO's `token` header,
    the EIA legacy v2 paths). When unused, :attr:`extra_token` is ``None``.
    """

    source_id: str
    api_key: str
    extra_token: str | None = None

    def __repr__(self) -> str:
        # Don't leak credential values via repr.
        return (
            f"ApiKeyBundle(source_id={self.source_id!r}, "
            f"api_key=<redacted, len={len(self.api_key)}>, "
            f"extra_token={'<redacted>' if self.extra_token else 'None'})"
        )


@dataclass(frozen=True, slots=True)
class UserPasswordBundle:
    """OAuth password-grant credentials.

    Currently used by ACLED. The bundle holds the credentials needed to
    exchange for an access token; it is not the access token itself
    (tokens are managed in-process per REQ-ACLED-AUTH-002 and never
    persisted).
    """

    source_id: str
    username: str
    password: str

    def __repr__(self) -> str:
        return (
            f"UserPasswordBundle(source_id={self.source_id!r}, "
            f"username=<redacted>, password=<redacted>)"
        )


CredentialBundle = ApiKeyBundle | UserPasswordBundle
"""Tagged union over the supported credential shapes."""


# Per-source env-var schema. Each entry maps source_id to either:
#   - {"api_key": <env_var>, "extra_token": <env_var or None>}    → ApiKeyBundle
#   - {"username": <env_var>, "password": <env_var>}              → UserPasswordBundle
#
# Adding a new authenticated source requires a new entry here. v1 covers the
# 12 sources from the data_ingest spec; sources that don't require auth (e.g.,
# World Bank, GDELT, USGS) are intentionally absent from this map.
_SOURCE_CREDENTIAL_SCHEMA: Final[dict[str, dict[str, str | None]]] = {
    "fred": {"api_key": "FRED_API_KEY", "extra_token": None},
    "acled": {"username": "ACLED_USERNAME", "password": "ACLED_PASSWORD"},
    "eia": {"api_key": "EIA_API_KEY", "extra_token": None},
    "nrc_adams": {"api_key": "NRC_ADAMS_API_KEY", "extra_token": None},
    "regulations_gov": {"api_key": "REGULATIONS_GOV_API_KEY", "extra_token": None},
    "noaa": {"api_key": "NOAA_CDO_TOKEN", "extra_token": None},
}


_DOTENV_LOADED = False


def _ensure_dotenv_loaded(env_path: Path | str | None = None) -> None:
    """Load ``.env`` once per process.

    The default ``load_dotenv`` call walks upward from the CWD looking for a
    ``.env`` file; we accept an explicit override for tests so the loader
    can be exercised without touching the operator's real environment.
    """
    global _DOTENV_LOADED
    if env_path is not None:
        # Explicit path: always reload so tests get fresh state.
        load_dotenv(dotenv_path=env_path, override=True)
        return
    if _DOTENV_LOADED:
        return
    load_dotenv(override=False)
    _DOTENV_LOADED = True


def load_credentials_for(
    source_id: str,
    *,
    env_path: Path | str | None = None,
) -> CredentialBundle | None:
    """Return credentials for ``source_id``, or ``None`` if unavailable.

    Returns ``None`` in three cases:

    1. The source isn't in the credential schema (i.e., it doesn't require
       auth in v1).
    2. The schema entry exists but one or more required env vars is missing
       or empty.
    3. The values are present but empty after stripping whitespace.

    Callers that *require* credentials raise on a ``None`` return; callers
    that have optional auth (e.g., a smoke-test runner) skip the source.
    """
    schema = _SOURCE_CREDENTIAL_SCHEMA.get(source_id)
    if schema is None:
        return None

    _ensure_dotenv_loaded(env_path)

    if "username" in schema and "password" in schema:
        username_var = schema["username"]
        password_var = schema["password"]
        if username_var is None or password_var is None:
            return None
        username = (os.environ.get(username_var) or "").strip()
        password = (os.environ.get(password_var) or "").strip()
        if not username or not password:
            return None
        return UserPasswordBundle(source_id=source_id, username=username, password=password)

    if "api_key" in schema:
        api_key_var = schema["api_key"]
        if api_key_var is None:
            return None
        api_key = (os.environ.get(api_key_var) or "").strip()
        if not api_key:
            return None

        extra_var = schema.get("extra_token")
        extra_token: str | None = None
        if extra_var is not None:
            raw = (os.environ.get(extra_var) or "").strip()
            extra_token = raw if raw else None

        return ApiKeyBundle(source_id=source_id, api_key=api_key, extra_token=extra_token)

    return None


def required_env_vars_for(source_id: str) -> tuple[str, ...]:
    """Return the env var names a source needs.

    Useful for documentation generation and for the operator README. Returns
    an empty tuple if the source is unauthenticated.
    """
    schema = _SOURCE_CREDENTIAL_SCHEMA.get(source_id)
    if schema is None:
        return ()
    names: list[str] = []
    for key in ("api_key", "extra_token", "username", "password"):
        value = schema.get(key)
        if isinstance(value, str):
            names.append(value)
    return tuple(names)
