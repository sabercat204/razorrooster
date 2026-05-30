"""Razor-Rooster operator-facing local read-only web GUI.

A FastAPI app that binds to ``127.0.0.1`` and serves a small set of
read-only dashboards over the existing DuckDB store. No external
assets, no JavaScript framework, no state mutation. The same
imperative-language linter that protects the daily report applies
to every rendered page.

Entry point: ``razor-rooster gui [--port N] [--host 127.0.0.1] [--db PATH]``.

The GUI never modifies state — it's a navigation chrome over the
artifacts the daily-cadence pipeline already produces. Operator
inputs (watch state, threshold edits, etc.) continue to flow
through the existing CLI.
"""

from razor_rooster.gui.app import create_app

__all__ = ["create_app"]
