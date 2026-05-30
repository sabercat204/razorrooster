"""``razor-rooster gui`` CLI subcommand.

Starts a local FastAPI app on ``127.0.0.1`` and serves the read-only
operator GUI. The default port is ``8765`` (override via ``--port``
or the ``RAZORROO_GUI_PORT`` env var). The default DuckDB path
follows the same resolution order as the other subsystems'
``--db`` flags: explicit ``--db``, then ``RAZOR_ROOSTER_DB``, then
``data/trough.duckdb``.

The server binds to loopback only. Passing a non-loopback host is
refused — the GUI is a single-operator tool, never a shared
service.
"""

from __future__ import annotations

import logging
import os
import webbrowser
from pathlib import Path

import click
import uvicorn

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"
_DEFAULT_PORT = 8765
_PORT_ENV = "RAZORROO_GUI_PORT"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get(_DEFAULT_DB_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


def _resolve_port(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    env_port = os.environ.get(_PORT_ENV)
    if env_port:
        try:
            return int(env_port)
        except ValueError as exc:
            raise click.BadParameter(
                f"Invalid {_PORT_ENV}: {env_port!r}; expected an integer port."
            ) from exc
    return _DEFAULT_PORT


@click.command(name="gui")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help=(
        "Host to bind. Must be a loopback address; non-loopback "
        "values are refused (the GUI is single-operator, never a "
        "shared service)."
    ),
)
@click.option(
    "--port",
    default=None,
    type=int,
    help=("Port to bind. Defaults to RAZORROO_GUI_PORT env var if set, otherwise 8765."),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help=(
        "Path to the DuckDB store. Defaults to RAZOR_ROOSTER_DB env "
        "var if set, otherwise data/trough.duckdb."
    ),
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    default=False,
    help="Open the GUI in the default browser on startup.",
)
@click.option(
    "--reload",
    "reload",
    is_flag=True,
    default=False,
    help=(
        "Enable Uvicorn's auto-reload on source-file changes. "
        "Useful during development; not recommended for normal use."
    ),
)
def gui_cmd(
    host: str,
    port: int | None,
    db_path_str: str | None,
    open_browser: bool,
    reload: bool,
) -> None:
    """Launch the read-only operator GUI on a local port.

    The server binds to ``127.0.0.1`` by default, reads from the
    DuckDB store, and serves a small set of dashboards over the
    persisted artifacts. No external assets, no JavaScript
    framework, no state mutation — it's a navigation chrome over
    the daily-cadence pipeline's outputs.
    """
    if host not in _LOOPBACK_HOSTS:
        raise click.BadParameter(
            f"--host {host!r} is not a loopback address; the GUI is "
            f"single-operator only. Allowed: {sorted(_LOOPBACK_HOSTS)}."
        )
    resolved_port = _resolve_port(port)
    if resolved_port < 1 or resolved_port > 65_535:
        raise click.BadParameter(f"--port {resolved_port} is out of range [1, 65535].")
    db_path = _resolve_db_path(db_path_str)
    if not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    url = f"http://{host}:{resolved_port}/"
    click.echo(f"Razor-Rooster GUI starting on {url}")
    click.echo(f"  db: {db_path}")
    click.echo("  read-only; no external assets; loopback only.")
    click.echo("  Ctrl+C to exit.")

    if open_browser:
        # Open after a short delay would be nicer, but webbrowser.open
        # returns immediately so this is fine — uvicorn will be ready
        # by the time the browser fetches.
        webbrowser.open(url)

    # Build the app via the factory pattern so reload works (uvicorn
    # passes the import string and re-imports on changes).
    os.environ["RAZORROO_GUI_DB"] = str(db_path)
    uvicorn.run(
        "razor_rooster.gui.cli:_create_app_from_env",
        host=host,
        port=resolved_port,
        reload=reload,
        factory=True,
        log_level="info",
    )


def _create_app_from_env() -> object:
    """Factory used by uvicorn to build the app.

    Reads the DB path from ``RAZORROO_GUI_DB`` (set by ``gui_cmd``
    just before starting uvicorn). Using an env-var handoff keeps
    the factory signature compatible with uvicorn's reload mode.
    """
    from razor_rooster.gui.app import create_app

    db_path_str = os.environ.get("RAZORROO_GUI_DB")
    if not db_path_str:
        msg = (
            "RAZORROO_GUI_DB not set; this factory is meant to be "
            "called by the `razor-rooster gui` CLI subcommand."
        )
        raise RuntimeError(msg)
    return create_app(db_path=Path(db_path_str))


__all__ = ["gui_cmd"]
