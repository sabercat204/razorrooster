"""T-KSI-032 — User-Agent + httpx client factory acceptance tests."""

from __future__ import annotations

import pytest

from razor_rooster import __version__
from razor_rooster.kalshi_connector.client.user_agent import (
    DEFAULT_TIMEOUT_SECONDS,
    build_httpx_client,
    build_user_agent,
)


def test_user_agent_without_contact_includes_research_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    ua = build_user_agent()
    assert ua.startswith(f"razor-rooster-kalshi/{__version__}")
    assert "(research)" in ua


def test_user_agent_with_explicit_contact_renders_with_plus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    ua = build_user_agent(contact="ops@example.com")
    assert "razor-rooster-kalshi" in ua
    assert "(research; +ops@example.com)" in ua


def test_user_agent_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_CONTACT", "env-contact@example.com")
    ua = build_user_agent()
    assert "+env-contact@example.com" in ua


def test_explicit_contact_wins_over_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_CONTACT", "env-contact@example.com")
    ua = build_user_agent(contact="explicit@example.com")
    assert "+explicit@example.com" in ua
    assert "env-contact" not in ua


def test_user_agent_rejects_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    with pytest.raises(ValueError, match="must not contain CR/LF"):
        build_user_agent(contact="ops@example.com\r\nX-Inject: header")


def test_httpx_client_carries_user_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    with build_httpx_client() as client:
        ua = client.headers.get("User-Agent", "")
    assert "razor-rooster-kalshi" in ua


def test_httpx_client_default_timeout_is_thirty() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 30.0


def test_httpx_client_extra_headers_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    with build_httpx_client(extra_headers={"X-Trace-ID": "abc123"}) as client:
        assert client.headers.get("X-Trace-ID") == "abc123"


def test_httpx_client_rejects_crlf_in_extra_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    with (
        pytest.raises(ValueError, match="contains CR/LF"),
        build_httpx_client(extra_headers={"X-Bad": "value\r\nX-Inject: hi"}),
    ):
        pass


def test_httpx_client_follow_redirects_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_CONTACT", raising=False)
    with build_httpx_client() as client:
        assert client.follow_redirects is True
