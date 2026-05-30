"""T-PMC-032 — User-Agent and httpx client factory tests."""

from __future__ import annotations

import pytest

from razor_rooster import __version__
from razor_rooster.polymarket_connector.client.user_agent import (
    DEFAULT_TIMEOUT_SECONDS,
    build_httpx_client,
    build_user_agent,
)


def test_user_agent_default_no_contact() -> None:
    ua = build_user_agent()
    assert ua == f"razor-rooster-polymarket/{__version__}"


def test_user_agent_explicit_contact() -> None:
    ua = build_user_agent(contact="ops@example.test")
    assert ua == f"razor-rooster-polymarket/{__version__} (+ops@example.test)"


def test_user_agent_env_var_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_CONTACT", "ops@example.test")
    ua = build_user_agent()
    assert "ops@example.test" in ua


def test_explicit_contact_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYMARKET_CONTACT", "env@example.test")
    ua = build_user_agent(contact="explicit@example.test")
    assert "explicit@example.test" in ua
    assert "env@example.test" not in ua


def test_user_agent_rejects_crlf_in_contact() -> None:
    with pytest.raises(ValueError, match="CR/LF"):
        build_user_agent(contact="bad\r\ninjection")


def test_httpx_client_has_user_agent_header() -> None:
    client = build_httpx_client()
    try:
        ua = client.headers.get("User-Agent")
        assert ua is not None
        assert "razor-rooster-polymarket" in ua
    finally:
        client.close()


def test_httpx_client_default_timeout() -> None:
    client = build_httpx_client()
    try:
        # httpx exposes the timeout as a Timeout instance with .read etc.
        # We just confirm it is not None / not the httpx default-no-timeout.
        assert client.timeout is not None
        # The configured value should be DEFAULT_TIMEOUT_SECONDS.
        assert float(client.timeout.read or 0) == DEFAULT_TIMEOUT_SECONDS
    finally:
        client.close()


def test_httpx_client_custom_timeout() -> None:
    client = build_httpx_client(timeout_seconds=2.5)
    try:
        assert float(client.timeout.read or 0) == 2.5
    finally:
        client.close()


def test_httpx_client_extra_headers() -> None:
    client = build_httpx_client(extra_headers={"X-Test-Header": "value"})
    try:
        assert client.headers.get("X-Test-Header") == "value"
        # User-Agent still present.
        assert client.headers.get("User-Agent") is not None
    finally:
        client.close()


def test_httpx_client_rejects_crlf_in_extra_headers() -> None:
    with pytest.raises(ValueError, match="CR/LF"):
        build_httpx_client(extra_headers={"X-Bad": "value\r\nhost: evil.test"})
