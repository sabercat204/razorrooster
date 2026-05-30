"""Top-level Razor-Rooster CLI.

Subcommands are contributed by each subsystem:
- ``razor-rooster ingest`` — see :mod:`razor_rooster.data_ingest.cli`.
- ``razor-rooster polymarket`` — see :mod:`razor_rooster.polymarket_connector.cli`.
- ``razor-rooster pattern-library`` — see :mod:`razor_rooster.pattern_library.cli`.
- ``razor-rooster scan`` — see :mod:`razor_rooster.signal_scanner.cli`.
- ``razor-rooster mispricing`` — see :mod:`razor_rooster.mispricing_detector.cli`.
- ``razor-rooster position-engine`` — see :mod:`razor_rooster.position_engine.cli`.
- ``razor-rooster monitor`` — see :mod:`razor_rooster.monitor.cli`.
- ``razor-rooster kalshi`` — see :mod:`razor_rooster.kalshi_connector.cli`.
- ``razor-rooster report`` — see :mod:`razor_rooster.report_generator.cli`.
- ``razor-rooster gui`` — see :mod:`razor_rooster.gui.cli`.
"""

from __future__ import annotations

import click

from razor_rooster import __version__
from razor_rooster.data_ingest.cli import ingest
from razor_rooster.gui.cli import gui_cmd
from razor_rooster.kalshi_connector.cli import kalshi
from razor_rooster.mispricing_detector.cli import mispricing
from razor_rooster.monitor.cli import monitor
from razor_rooster.pattern_library.cli import pattern_library
from razor_rooster.polymarket_connector.cli import polymarket
from razor_rooster.position_engine.cli import position_engine
from razor_rooster.report_generator.cli import report
from razor_rooster.signal_scanner.cli import scan


@click.group()
@click.version_option(version=__version__, prog_name="razor-rooster")
def main() -> None:
    """Razor-Rooster: geopolitical event forecasting and calibration."""


main.add_command(ingest)
main.add_command(polymarket)
main.add_command(pattern_library)
main.add_command(scan)
main.add_command(mispricing)
main.add_command(position_engine)
main.add_command(monitor)
main.add_command(kalshi)
main.add_command(report)
main.add_command(gui_cmd)


if __name__ == "__main__":  # pragma: no cover
    main()
