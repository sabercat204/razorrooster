"""Geo-restriction gate (T-PMC-020; REQ-PMC-GEO-001, REQ-PMC-GEO-002).

Runs at every connector startup. The gate fails closed: missing config or a
restricted jurisdiction raises :class:`StartupRefusal` and the connector
does not run. There is no "I don't know my jurisdiction" code path. There
is no proxy/VPN circumvention support.

Operators declare their jurisdiction via either:

- The ``OPERATOR_JURISDICTION`` environment variable, or
- The ``jurisdiction`` field of ``config/operator.yaml``.

Env var wins on conflict so transient overrides for testing are explicit.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import yaml

from razor_rooster.polymarket_connector.config.loader import (
    RestrictedJurisdictionsConfig,
    load_restricted_jurisdictions,
)

logger = logging.getLogger(__name__)


_OPERATOR_JURISDICTION_ENV: Final[str] = "OPERATOR_JURISDICTION"
_OPERATOR_CONFIG_DEFAULT: Final[Path] = Path("config") / "operator.yaml"
_RESTRICTED_CONFIG_DEFAULT: Final[Path] = Path("config") / "restricted_jurisdictions.yaml"


class StartupRefusal(RuntimeError):
    """Raised when the geo gate refuses to start the connector.

    The exception message is what the operator sees on stderr; keep it
    actionable and specific.
    """


def _read_operator_jurisdiction(operator_config_path: Path) -> str | None:
    """Return the operator's declared jurisdiction or ``None`` if not set.

    Env var wins on conflict. The operator config file is optional; if it
    does not exist or has no ``jurisdiction`` field, only the env var path
    is consulted.
    """
    env_value = os.environ.get(_OPERATOR_JURISDICTION_ENV)
    if env_value is not None and env_value.strip():
        return env_value.strip()

    if not operator_config_path.exists():
        return None
    try:
        with operator_config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise StartupRefusal(
            f"operator config at {operator_config_path} is invalid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        return None
    declared = raw.get("jurisdiction")
    if declared is None:
        return None
    if not isinstance(declared, str):
        raise StartupRefusal(
            f"operator config 'jurisdiction' must be a string, got {type(declared).__name__}"
        )
    declared = declared.strip()
    return declared if declared else None


def check_jurisdiction(
    *,
    operator_config_path: Path | str = _OPERATOR_CONFIG_DEFAULT,
    restricted_config_path: Path | str = _RESTRICTED_CONFIG_DEFAULT,
    restricted: RestrictedJurisdictionsConfig | None = None,
) -> str:
    """Verify the operator's jurisdiction is permitted. Return the normalized value on success.

    Raises :class:`StartupRefusal` when:

    - No jurisdiction is declared (env var or operator config field).
    - The declared jurisdiction matches a restricted entry (case-insensitive).
    - The restricted-jurisdictions config cannot be loaded.

    The `restricted` keyword argument lets callers inject a pre-loaded
    config (used by tests). If absent, the function reads
    ``restricted_config_path``.
    """
    declared = _read_operator_jurisdiction(Path(operator_config_path))
    if declared is None:
        raise StartupRefusal(
            f"{_OPERATOR_JURISDICTION_ENV} is not configured. Set the env var or "
            "config/operator.yaml jurisdiction field. polymarket_connector refuses "
            "to run without an explicit jurisdiction declaration."
        )

    if restricted is None:
        try:
            restricted = load_restricted_jurisdictions(restricted_config_path)
        except Exception as exc:
            raise StartupRefusal(
                f"failed to load restricted-jurisdictions config from "
                f"{restricted_config_path}: {exc}"
            ) from exc

    declared_norm = declared.upper()
    restricted_norm = {entry.strip().upper() for entry in restricted.restricted}
    if declared_norm in restricted_norm:
        raise StartupRefusal(
            f"Jurisdiction {declared!r} is on Polymarket's restricted list. "
            "polymarket_connector cannot run from this jurisdiction. If you "
            "believe this is incorrect, see Polymarket's geographic-restrictions "
            "documentation and update config/restricted_jurisdictions.yaml only "
            "after confirming the change against Polymarket's current published "
            "restrictions."
        )

    logger.info("geo gate accepted jurisdiction %r", declared_norm)
    return declared_norm
