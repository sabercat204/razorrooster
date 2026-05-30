"""Imperative-language linter (T-PE-041; OQ-PE-006 resolution).

Reads ``config/forbidden_phrases.yaml`` and runs case-insensitive
substring match against rendered analysis output. Refuses to ship
output containing any forbidden phrase by raising
:class:`ImperativeLanguageDetected` with the offending phrase
highlighted.

The catalog is operator-extensible: edit the YAML to add patterns
as new imperative drift is noticed in real outputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CATALOG_PATH = Path("config") / "forbidden_phrases.yaml"


@dataclass(frozen=True, slots=True)
class LinterCatalog:
    """Loaded forbidden-phrase catalog."""

    phrases: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> LinterCatalog:
        target = path or DEFAULT_CATALOG_PATH
        if not target.exists():
            return cls(phrases=cls.default_phrases())
        with target.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        if not isinstance(payload, dict):
            return cls(phrases=cls.default_phrases())
        raw_phrases = payload.get("phrases") or []
        if not isinstance(raw_phrases, list):
            return cls(phrases=cls.default_phrases())
        return cls(phrases=tuple(str(p).strip() for p in raw_phrases if str(p).strip()))

    @staticmethod
    def default_phrases() -> tuple[str, ...]:
        """Fallback phrase list used when no YAML is present.

        Matches the seed entries in ``config/forbidden_phrases.yaml``
        verbatim. Tests can rely on this when the file is missing.
        """
        return (
            "you should buy",
            "you should sell",
            "buy this",
            "sell this",
            "go long",
            "go short",
            "i recommend",
            "the trade is",
            "take this position",
            "guaranteed to",
        )


class ImperativeLanguageDetected(RuntimeError):
    """Raised when the linter finds a forbidden phrase in rendered output."""

    def __init__(self, phrase: str, snippet: str) -> None:
        super().__init__(
            f"forbidden imperative phrase {phrase!r} found in rendered output: ...{snippet}..."
        )
        self.phrase = phrase
        self.snippet = snippet


def check_text(
    text: str,
    *,
    catalog: LinterCatalog | None = None,
    extra_phrases: Iterable[str] = (),
) -> None:
    """Raise :class:`ImperativeLanguageDetected` if any phrase matches.

    Args:
        text: Rendered analysis output.
        catalog: Override the loaded catalog (test-injection).
        extra_phrases: Additional one-off phrases to check beyond the
            catalog.

    Returns:
        None on clean output.
    """
    cat = catalog or LinterCatalog.from_yaml()
    haystack = text.lower()
    all_phrases = list(cat.phrases) + [str(p).strip() for p in extra_phrases if str(p).strip()]
    for phrase in all_phrases:
        needle = phrase.lower()
        if not needle:
            continue
        idx = haystack.find(needle)
        if idx != -1:
            start = max(0, idx - 20)
            end = min(len(text), idx + len(phrase) + 20)
            snippet = text[start:end].replace("\n", " ")
            raise ImperativeLanguageDetected(phrase=phrase, snippet=snippet)


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "ImperativeLanguageDetected",
    "LinterCatalog",
    "check_text",
]
