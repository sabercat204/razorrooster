"""Eligibility allow-list gate (T-KSI-020; REQ-KSI-ELIG-001 / REQ-KSI-ELIG-002).

Runs at every Kalshi connector startup. The gate fails closed: missing
config or a jurisdiction not on the allow-list raises
:class:`EligibilityRefusal` and the connector does not run.

Design notes:

- The gate **inverts** the Polymarket deny-list pattern. Refusal occurs
  when the operator's declared jurisdiction is **not** on the allow-list
  in ``config/kalshi_allowed_jurisdictions.yaml`` (Kalshi is a
  CFTC-regulated US-only market in v1).
- The gate reuses the same ``OPERATOR_JURISDICTION`` env var that the
  Polymarket geo gate consults. Operator declares jurisdiction once; the
  two gates enforce their respective postures from a single declaration.
  The same operator-config file (``config/operator.yaml``) is consulted
  as a fallback to the env var. Env var wins on conflict.
- There is no proxy/VPN circumvention support, no auto-detection.

Refusal messages name the file the operator must edit so the path
forward is explicit.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import yaml

from razor_rooster.kalshi_connector.config.loader import (
    KalshiAllowedJurisdictionsConfig,
    load_kalshi_allowed_jurisdictions,
)

logger = logging.getLogger(__name__)


_OPERATOR_JURISDICTION_ENV: Final[str] = "OPERATOR_JURISDICTION"
_OPERATOR_CONFIG_DEFAULT: Final[Path] = Path("config") / "operator.yaml"
_ALLOWED_CONFIG_DEFAULT: Final[Path] = Path("config") / "kalshi_allowed_jurisdictions.yaml"


class EligibilityRefusal(RuntimeError):
    """Raised when the eligibility allow-list gate refuses startup.

    The exception message is what the operator sees on stderr; keep it
    actionable and specific.
    """


def _read_operator_jurisdiction(operator_config_path: Path) -> str | None:
    """Return the operator's declared jurisdiction or ``None`` if not set.

    Env var wins on conflict. The operator config file is optional; if
    it does not exist or has no ``jurisdiction`` field, only the env-var
    path is consulted.
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
        raise EligibilityRefusal(
            f"operator config at {operator_config_path} is invalid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        return None
    declared = raw.get("jurisdiction")
    if declared is None:
        return None
    if not isinstance(declared, str):
        raise EligibilityRefusal(
            f"operator config 'jurisdiction' must be a string, got {type(declared).__name__}"
        )
    declared = declared.strip()
    return declared if declared else None


def check_eligibility(
    *,
    operator_config_path: Path | str = _OPERATOR_CONFIG_DEFAULT,
    allowed_config_path: Path | str = _ALLOWED_CONFIG_DEFAULT,
    allowed: KalshiAllowedJurisdictionsConfig | None = None,
) -> str:
    """Verify the operator's jurisdiction is on the Kalshi allow-list.

    Returns the normalized (uppercased) jurisdiction value on success.

    Raises :class:`EligibilityRefusal` when:

    - No jurisdiction is declared (env var or operator config field).
    - The declared jurisdiction is **not** in
      ``config/kalshi_allowed_jurisdictions.yaml`` (case-insensitive).
    - The allow-list config cannot be loaded.

    The ``allowed`` keyword argument lets callers inject a pre-loaded
    config (used by tests). If absent, the function reads
    ``allowed_config_path``.
    """
    declared = _read_operator_jurisdiction(Path(operator_config_path))
    if declared is None:
        raise EligibilityRefusal(
            f"{_OPERATOR_JURISDICTION_ENV} is not configured. Set the env "
            "var or config/operator.yaml jurisdiction field. "
            "kalshi_connector refuses to run without an explicit "
            "jurisdiction declaration."
        )

    if allowed is None:
        try:
            allowed = load_kalshi_allowed_jurisdictions(allowed_config_path)
        except Exception as exc:
            raise EligibilityRefusal(
                f"failed to load Kalshi allow-list config from {allowed_config_path}: {exc}"
            ) from exc

    declared_norm = declared.upper()
    allowed_norm = {entry.strip().upper() for entry in allowed.allowed}
    if declared_norm not in allowed_norm:
        raise EligibilityRefusal(
            f"Jurisdiction {declared!r} is not on the Kalshi allow-list. "
            "Kalshi is a CFTC-regulated US designated contract market and "
            "the v1 connector enforces an allow-list posture. To enable a "
            "new jurisdiction, edit "
            "config/kalshi_allowed_jurisdictions.yaml after verifying "
            "Kalshi permits participation from that jurisdiction."
        )

    logger.info("kalshi eligibility gate accepted jurisdiction %r", declared_norm)
    return declared_norm


__all__ = [
    "EligibilityRefusal",
    "check_eligibility",
]
