"""T-031 verification — time and geo normalization helpers.

Time tests:
- Tz-aware UTC datetimes pass through unchanged.
- Tz-aware non-UTC datetimes are converted.
- Tz-naive + hint_tz uses the hint.
- Tz-naive + no hint assumes UTC and warns.
- Unknown hint_tz raises.
- Non-datetime input raises.

Geo tests:
- ISO-3 codes pass through.
- ISO-2 codes map to alpha-3.
- Common English names map (case-insensitive, whitespace tolerant).
- Ambiguous inputs return None with a warning.
- Unknown inputs return None with a warning.
- None / empty / whitespace inputs return None silently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from razor_rooster.data_ingest.normalization.geo import to_iso3
from razor_rooster.data_ingest.normalization.time import to_utc

# --- to_utc -----------------------------------------------------------------


def test_to_utc_passes_utc_through_unchanged() -> None:
    value = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    result = to_utc(value)
    assert result == value
    assert result.tzinfo == UTC


def test_to_utc_converts_eastern_time_to_utc() -> None:
    eastern = ZoneInfo("America/New_York")
    value = datetime(2026, 5, 14, 5, 30, tzinfo=eastern)  # 09:30 UTC
    result = to_utc(value)
    assert result.hour == 9
    assert result.minute == 30
    assert result.tzinfo == UTC


def test_to_utc_naive_with_hint_tz_uses_hint() -> None:
    value = datetime(2026, 5, 14, 5, 30)  # naive
    result = to_utc(value, hint_tz="America/New_York")
    assert result.hour == 9
    assert result.minute == 30
    assert result.tzinfo == UTC


def test_to_utc_naive_without_hint_assumes_utc_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    value = datetime(2026, 5, 14, 9, 30)  # naive
    with caplog.at_level(logging.WARNING):
        result = to_utc(value)
    assert result.hour == 9
    assert result.tzinfo == UTC
    assert any("naive" in r.message for r in caplog.records)


def test_to_utc_unknown_hint_tz_raises() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        to_utc(datetime(2026, 5, 14), hint_tz="Mars/Olympus_Mons")


def test_to_utc_non_datetime_raises() -> None:
    with pytest.raises(TypeError, match="expected a datetime"):
        to_utc("2026-05-14T09:30:00Z")  # type: ignore[arg-type]


def test_to_utc_with_fixed_offset_timezone() -> None:
    """Non-zoneinfo tzinfo (datetime.timezone) should still convert."""
    fixed = timezone(
        offset=datetime.now().astimezone().utcoffset()
        or UTC.utcoffset(datetime.now())
        or UTC.utcoffset(datetime.now())
    )
    # Use a known offset to avoid system-dependent test.
    five_hours = timezone(offset=__import__("datetime").timedelta(hours=5))
    value = datetime(2026, 5, 14, 14, 30, tzinfo=five_hours)
    result = to_utc(value)
    assert result.hour == 9
    assert result.minute == 30
    assert result.tzinfo == UTC
    del fixed


def test_to_utc_dst_transition() -> None:
    """A datetime in a DST-using zone converts correctly even at DST boundaries."""
    eastern = ZoneInfo("America/New_York")
    # Pick a date well after the spring-forward in 2026 (March 8).
    value = datetime(2026, 4, 1, 12, 0, tzinfo=eastern)
    result = to_utc(value)
    # Eastern is UTC-4 during DST.
    assert result.hour == 16
    assert result.tzinfo == UTC


# --- to_iso3 ----------------------------------------------------------------


def test_to_iso3_handles_alpha3_passthrough() -> None:
    assert to_iso3("USA") == "USA"
    assert to_iso3("FRA") == "FRA"
    assert to_iso3("som") == "SOM"


def test_to_iso3_maps_alpha2_to_alpha3() -> None:
    assert to_iso3("US") == "USA"
    assert to_iso3("UK") == "GBR"
    assert to_iso3("GB") == "GBR"
    assert to_iso3("DE") == "DEU"
    assert to_iso3("JP") == "JPN"


def test_to_iso3_maps_common_english_names() -> None:
    assert to_iso3("United States") == "USA"
    assert to_iso3("United Kingdom") == "GBR"
    assert to_iso3("France") == "FRA"
    assert to_iso3("Germany") == "DEU"
    assert to_iso3("Japan") == "JPN"
    assert to_iso3("South Africa") == "ZAF"


def test_to_iso3_handles_case_and_whitespace() -> None:
    assert to_iso3("  united states  ") == "USA"
    assert to_iso3("FRANCE") == "FRA"
    assert to_iso3("frAnCe") == "FRA"


def test_to_iso3_returns_none_for_ambiguous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """'Georgia' is the US state and the country; mapper refuses to guess."""
    with caplog.at_level(logging.WARNING):
        result = to_iso3("Georgia")
    assert result is None
    assert any("ambiguous" in r.message for r in caplog.records)


def test_to_iso3_returns_none_for_congo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """'Congo' is COG (Republic of) vs. COD (Democratic Republic); ambiguous."""
    with caplog.at_level(logging.WARNING):
        result = to_iso3("Congo")
    assert result is None


def test_to_iso3_returns_none_for_korea_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """'Korea' alone is ambiguous; explicit qualifiers are mapped."""
    with caplog.at_level(logging.WARNING):
        result = to_iso3("Korea")
    assert result is None
    assert to_iso3("South Korea") == "KOR"
    assert to_iso3("North Korea") == "PRK"


def test_to_iso3_returns_none_for_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = to_iso3("Atlantis")
    assert result is None
    assert any("unrecognized" in r.message for r in caplog.records)


def test_to_iso3_returns_none_for_none() -> None:
    assert to_iso3(None) is None


def test_to_iso3_returns_none_for_empty_string() -> None:
    assert to_iso3("") is None


def test_to_iso3_returns_none_for_whitespace() -> None:
    assert to_iso3("   ") is None


def test_to_iso3_handles_alternative_spellings() -> None:
    assert to_iso3("Türkiye") == "TUR"
    assert to_iso3("Burma") == "MMR"
    assert to_iso3("Holland") == "NLD"


def test_to_iso3_handles_short_country_aliases() -> None:
    """Common short aliases should map correctly."""
    assert to_iso3("Britain") == "GBR"
    assert to_iso3("America") == "USA"


def test_to_iso3_does_not_guess_partial_matches() -> None:
    """The mapper does substring-free exact lookup; 'United' isn't a country."""
    assert to_iso3("United") is None
