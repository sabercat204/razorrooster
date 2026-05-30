"""Geographic identifier normalization (T-031, OQ-006).

Maps source-native country identifiers (full names, ISO 3166-1 alpha-2
codes, common variants, and a few historical or political alternatives) to
ISO 3166-1 alpha-3 codes. Sub-national normalization is deferred per the
OQ-006 resolution.

Discipline: ambiguous inputs return ``None`` and log a warning. The mapper
never guesses. If a connector's source produces a country name that isn't
in the curated table, the operator sees the warning and decides whether to
add it to the lookup or leave the country_iso3 column NULL.

The table is intentionally short — it covers the v1 source set's most-
common country references. Extending it is a low-friction operator change
(append a row, the test suite catches collisions).
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger(__name__)


# A small curated table of common country aliases. Keys are normalized to
# uppercase before lookup so the caller doesn't need to worry about case.
# Sources of overlap (e.g., "GEORGIA" → US state vs. country) are left
# ambiguous on purpose; the value is the alpha-3 only when there is no
# realistic alternative meaning for the literal input.
_ALIAS_TO_ISO3: Final[dict[str, str]] = {
    # ISO 3166-1 alpha-3 codes that match themselves.
    "USA": "USA",
    "GBR": "GBR",
    "FRA": "FRA",
    "DEU": "DEU",
    "JPN": "JPN",
    "CHN": "CHN",
    "RUS": "RUS",
    "CAN": "CAN",
    "AUS": "AUS",
    "ITA": "ITA",
    "ESP": "ESP",
    "BRA": "BRA",
    "MEX": "MEX",
    "ARG": "ARG",
    "IND": "IND",
    "TUR": "TUR",
    "EGY": "EGY",
    "ZAF": "ZAF",
    "NGA": "NGA",
    "KEN": "KEN",
    "ETH": "ETH",
    "SOM": "SOM",
    "SDN": "SDN",
    "SSD": "SSD",
    "YEM": "YEM",
    "SYR": "SYR",
    "IRQ": "IRQ",
    "IRN": "IRN",
    "ISR": "ISR",
    "PSE": "PSE",
    "JOR": "JOR",
    "LBN": "LBN",
    "AFG": "AFG",
    "PAK": "PAK",
    "BGD": "BGD",
    "MMR": "MMR",
    "THA": "THA",
    "VNM": "VNM",
    "PHL": "PHL",
    "IDN": "IDN",
    "KOR": "KOR",
    "PRK": "PRK",
    "TWN": "TWN",
    "UKR": "UKR",
    "POL": "POL",
    "ROU": "ROU",
    "GRC": "GRC",
    "PRT": "PRT",
    "NLD": "NLD",
    "BEL": "BEL",
    "CHE": "CHE",
    "AUT": "AUT",
    "SWE": "SWE",
    "NOR": "NOR",
    "FIN": "FIN",
    "DNK": "DNK",
    "IRL": "IRL",
    "VEN": "VEN",
    "COL": "COL",
    "PER": "PER",
    "CHL": "CHL",
    "BOL": "BOL",
    "URY": "URY",
    "PRY": "PRY",
    "ECU": "ECU",
    "CUB": "CUB",
    "HTI": "HTI",
    "DOM": "DOM",
    "GTM": "GTM",
    "HND": "HND",
    "SLV": "SLV",
    "NIC": "NIC",
    "CRI": "CRI",
    "PAN": "PAN",
    "JAM": "JAM",
    "TTO": "TTO",
    # ISO alpha-2 → alpha-3.
    "US": "USA",
    "GB": "GBR",
    "UK": "GBR",
    "FR": "FRA",
    "DE": "DEU",
    "JP": "JPN",
    "CN": "CHN",
    "RU": "RUS",
    "CA": "CAN",
    "AU": "AUS",
    "IT": "ITA",
    "ES": "ESP",
    "BR": "BRA",
    "MX": "MEX",
    "AR": "ARG",
    "IN": "IND",
    "TR": "TUR",
    "EG": "EGY",
    "ZA": "ZAF",
    "NG": "NGA",
    "KE": "KEN",
    "ET": "ETH",
    "SO": "SOM",
    "SD": "SDN",
    "SS": "SSD",
    "YE": "YEM",
    "SY": "SYR",
    "IQ": "IRQ",
    "IR": "IRN",
    "IL": "ISR",
    "PS": "PSE",
    "JO": "JOR",
    "LB": "LBN",
    "AF": "AFG",
    "PK": "PAK",
    "BD": "BGD",
    "MM": "MMR",
    "TH": "THA",
    "VN": "VNM",
    "PH": "PHL",
    "ID": "IDN",
    "KR": "KOR",
    "KP": "PRK",
    "TW": "TWN",
    "UA": "UKR",
    "PL": "POL",
    "RO": "ROU",
    "GR": "GRC",
    "PT": "PRT",
    "NL": "NLD",
    "BE": "BEL",
    "CH": "CHE",
    "AT": "AUT",
    "SE": "SWE",
    "NO": "NOR",
    "FI": "FIN",
    "DK": "DNK",
    "IE": "IRL",
    "VE": "VEN",
    "CO": "COL",
    "PE": "PER",
    "CL": "CHL",
    "BO": "BOL",
    "UY": "URY",
    "PY": "PRY",
    "EC": "ECU",
    "CU": "CUB",
    "HT": "HTI",
    "DO": "DOM",
    "GT": "GTM",
    "HN": "HND",
    "SV": "SLV",
    "NI": "NIC",
    "CR": "CRI",
    "PA": "PAN",
    "JM": "JAM",
    "TT": "TTO",
    # Common English country names.
    "UNITED STATES": "USA",
    "UNITED STATES OF AMERICA": "USA",
    "UNITED STATES OF AMERICA (USA)": "USA",
    "AMERICA": "USA",
    "UNITED KINGDOM": "GBR",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "GBR",
    "GREAT BRITAIN": "GBR",
    "BRITAIN": "GBR",
    "ENGLAND": "GBR",
    "FRANCE": "FRA",
    "GERMANY": "DEU",
    "JAPAN": "JPN",
    "CHINA": "CHN",
    "PEOPLE'S REPUBLIC OF CHINA": "CHN",
    "PRC": "CHN",
    "RUSSIA": "RUS",
    "RUSSIAN FEDERATION": "RUS",
    "CANADA": "CAN",
    "AUSTRALIA": "AUS",
    "ITALY": "ITA",
    "SPAIN": "ESP",
    "BRAZIL": "BRA",
    "MEXICO": "MEX",
    "ARGENTINA": "ARG",
    "INDIA": "IND",
    "TURKEY": "TUR",
    "TÜRKIYE": "TUR",
    "TURKIYE": "TUR",
    "EGYPT": "EGY",
    "SOUTH AFRICA": "ZAF",
    "NIGERIA": "NGA",
    "KENYA": "KEN",
    "ETHIOPIA": "ETH",
    "SOMALIA": "SOM",
    "SUDAN": "SDN",
    "SOUTH SUDAN": "SSD",
    "YEMEN": "YEM",
    "SYRIA": "SYR",
    "SYRIAN ARAB REPUBLIC": "SYR",
    "IRAQ": "IRQ",
    "IRAN": "IRN",
    "ISLAMIC REPUBLIC OF IRAN": "IRN",
    "ISRAEL": "ISR",
    "PALESTINE": "PSE",
    "STATE OF PALESTINE": "PSE",
    "PALESTINIAN TERRITORY": "PSE",
    "JORDAN": "JOR",
    "LEBANON": "LBN",
    "AFGHANISTAN": "AFG",
    "PAKISTAN": "PAK",
    "BANGLADESH": "BGD",
    "MYANMAR": "MMR",
    "BURMA": "MMR",
    "THAILAND": "THA",
    "VIETNAM": "VNM",
    "VIET NAM": "VNM",
    "PHILIPPINES": "PHL",
    "INDONESIA": "IDN",
    "SOUTH KOREA": "KOR",
    "REPUBLIC OF KOREA": "KOR",
    "NORTH KOREA": "PRK",
    "DEMOCRATIC PEOPLE'S REPUBLIC OF KOREA": "PRK",
    "DPRK": "PRK",
    "TAIWAN": "TWN",
    "UKRAINE": "UKR",
    "POLAND": "POL",
    "ROMANIA": "ROU",
    "GREECE": "GRC",
    "PORTUGAL": "PRT",
    "NETHERLANDS": "NLD",
    "THE NETHERLANDS": "NLD",
    "HOLLAND": "NLD",
    "BELGIUM": "BEL",
    "SWITZERLAND": "CHE",
    "AUSTRIA": "AUT",
    "SWEDEN": "SWE",
    "NORWAY": "NOR",
    "FINLAND": "FIN",
    "DENMARK": "DNK",
    "IRELAND": "IRL",
    "VENEZUELA": "VEN",
    "COLOMBIA": "COL",
    "PERU": "PER",
    "CHILE": "CHL",
    "BOLIVIA": "BOL",
    "URUGUAY": "URY",
    "PARAGUAY": "PRY",
    "ECUADOR": "ECU",
    "CUBA": "CUB",
    "HAITI": "HTI",
    "DOMINICAN REPUBLIC": "DOM",
    "GUATEMALA": "GTM",
    "HONDURAS": "HND",
    "EL SALVADOR": "SLV",
    "NICARAGUA": "NIC",
    "COSTA RICA": "CRI",
    "PANAMA": "PAN",
    "JAMAICA": "JAM",
    "TRINIDAD AND TOBAGO": "TTO",
}


# Inputs that match multiple plausible countries are explicitly ambiguous.
# The mapper returns None and logs for these, so the operator can resolve
# manually if the source's context disambiguates.
_AMBIGUOUS_INPUTS: Final[frozenset[str]] = frozenset(
    {
        # State of Georgia (USA) vs. country of Georgia.
        "GEORGIA",
        # State of New York vs. New York country (none, but sometimes
        # appears in source data and is worth flagging).
        # State of Washington vs. country of (former) Washington (none).
        # Common ambiguous country/historical-region names:
        "CONGO",  # Republic of the Congo (COG) vs. Democratic Republic (COD)
        "MACEDONIA",  # North Macedonia (MKD) vs. Greek region
        "KOREA",  # KOR vs. PRK
    }
)


def to_iso3(value: str | None) -> str | None:
    """Map a country identifier to ISO 3166-1 alpha-3.

    Returns ``None`` for:

    - ``None`` or empty/whitespace-only input.
    - Inputs in the ambiguous set (with a warning logged).
    - Inputs that don't match any alias in the table (with a warning logged).

    Returns the matching alpha-3 code otherwise. Lookup is case-insensitive
    and tolerant of leading/trailing whitespace.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    upper = stripped.upper()

    if upper in _AMBIGUOUS_INPUTS:
        logger.warning(
            "to_iso3: ambiguous country input %r, returning None for explicit handling",
            value,
        )
        return None

    iso3 = _ALIAS_TO_ISO3.get(upper)
    if iso3 is None:
        logger.warning("to_iso3: unrecognized country input %r, returning None", value)
        return None
    return iso3
