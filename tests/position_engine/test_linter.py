"""T-PE-041 — imperative-language linter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    LinterCatalog,
    check_text,
)


def test_clean_text_passes() -> None:
    text = (
        "if the operator chose to act, the suggested fraction would be 0.05. "
        "this is decision-support; not investment advice."
    )
    check_text(text)


def test_rejects_you_should_buy() -> None:
    with pytest.raises(ImperativeLanguageDetected) as exc_info:
        check_text("at this delta, you should buy this market.")
    assert exc_info.value.phrase == "you should buy"


def test_rejects_you_should_sell() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("based on this analysis, you should sell.")


def test_rejects_buy_this() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("buy this position immediately.")


def test_rejects_go_long() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("recommendation: go long here.")


def test_rejects_i_recommend() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("based on the analysis, i recommend acting.")


def test_rejects_the_trade_is() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("the trade is straightforward given the delta.")


def test_rejects_take_this_position() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("take this position now while liquidity is high.")


def test_rejects_guaranteed_to() -> None:
    with pytest.raises(ImperativeLanguageDetected):
        check_text("at half-Kelly, this is guaranteed to grow log-bankroll.")


def test_case_insensitive_match() -> None:
    """Phrases match regardless of operator case."""
    with pytest.raises(ImperativeLanguageDetected):
        check_text("YOU SHOULD BUY HERE.")
    with pytest.raises(ImperativeLanguageDetected):
        check_text("You Should Buy Here.")


def test_extra_phrases_extend_check() -> None:
    """Operators can pass extra phrases for one-off checks."""
    with pytest.raises(ImperativeLanguageDetected) as exc_info:
        check_text("just dive in already.", extra_phrases=("dive in",))
    assert exc_info.value.phrase == "dive in"


def test_default_catalog_when_yaml_missing(tmp_path: Path) -> None:
    """LinterCatalog falls back to default phrases when the file is absent."""
    catalog = LinterCatalog.from_yaml(path=tmp_path / "missing.yaml")
    assert "you should buy" in catalog.phrases


def test_loads_catalog_from_yaml() -> None:
    """The shipped config/forbidden_phrases.yaml validates and loads."""
    catalog = LinterCatalog.from_yaml()
    assert "you should buy" in catalog.phrases
    assert "go long" in catalog.phrases


def test_explicit_catalog_overrides_yaml() -> None:
    """Tests can inject a minimal catalog."""
    catalog = LinterCatalog(phrases=("forbidden_test_phrase",))
    with pytest.raises(ImperativeLanguageDetected):
        check_text("this contains forbidden_test_phrase by design.", catalog=catalog)
    # The yaml's "you should buy" doesn't fire because we used an explicit catalog.
    check_text("you should buy", catalog=catalog)


def test_error_includes_phrase_and_snippet() -> None:
    with pytest.raises(ImperativeLanguageDetected) as exc_info:
        check_text(
            "this is the analysis. based on the data, i recommend acting "
            "here. consider the warnings.",
        )
    assert exc_info.value.phrase == "i recommend"
    assert "i recommend" in exc_info.value.snippet
