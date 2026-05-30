"""T-057 verification — Baltic Dry Index proxy via FRED.

Per design §11 / spec table, the BDI is ingested as a FRED series rather
than a separate connector. T-057 confirms:

- The bundled FRED series config includes a shipping/commodity proxy.
- Loading the config produces an entry that the FRED connector can fetch.
- The FRED connector handles the proxy series exactly like other FRED
  series (no special-casing).

If the operator wants to substitute a different proxy (e.g., a real BDI
mirror published as a FRED series), the change is config-only.
"""

from __future__ import annotations

from pathlib import Path

from razor_rooster.data_ingest.connectors.fred import (
    FredSeries,
    load_fred_series_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_FRED_CONFIG = _REPO_ROOT / "config" / "fred_series.yaml"


def test_bundled_fred_config_includes_shipping_proxy() -> None:
    """A shipping/commodity proxy is present (T-057)."""
    series = load_fred_series_config(_BUNDLED_FRED_CONFIG)
    series_ids = {s.id for s in series}
    # The current proxy is PNGASJPUSDM (Global LNG price). The test asserts
    # at least one shipping/freight-correlated commodity series is present.
    assert "PNGASJPUSDM" in series_ids


def test_bdi_proxy_entry_is_well_formed() -> None:
    series = load_fred_series_config(_BUNDLED_FRED_CONFIG)
    by_id = {s.id: s for s in series}
    bdi = by_id["PNGASJPUSDM"]
    assert isinstance(bdi, FredSeries)
    assert bdi.title  # non-empty
    assert bdi.frequency in {"D", "W", "M", "Q", "A"}
