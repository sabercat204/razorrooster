"""Migration discovery and runner for ``data_ingest`` (T-013).

Each migration is a Python module in this package named ``m####_<description>``
exposing two callables:

- ``up(conn: duckdb.DuckDBPyConnection) -> None``: apply the migration.
- ``down(conn: duckdb.DuckDBPyConnection) -> None``: roll back. Only callable
  via an explicit CLI flag — never auto-runs.

The runner discovers migrations alphabetically (so ``m0001`` < ``m0002`` < ...),
checks the ``schema_migrations`` table for applied versions, and applies any
unapplied migrations in order.

Concurrency: callers are responsible for serializing migrations against
ingest cycles. ``run_pending_migrations`` acquires its own connection from
the store and runs migrations within a transaction per migration.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

import duckdb

logger = logging.getLogger(__name__)


_MIGRATION_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^m(\d{4})_([a-z0-9_]+)$")


class MigrationModule(Protocol):
    """Structural protocol every migration module must satisfy."""

    def up(self, conn: duckdb.DuckDBPyConnection) -> None: ...

    def down(self, conn: duckdb.DuckDBPyConnection) -> None: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """A discovered migration with its parsed metadata."""

    version: int
    description: str
    module_name: str
    module: MigrationModule


class MigrationError(RuntimeError):
    """Base class for migration framework failures."""


class MigrationDiscoveryError(MigrationError):
    """Raised when a migration module is malformed (bad name, missing up/down)."""


class MigrationApplicationError(MigrationError):
    """Raised when a migration's ``up()`` raises during application."""


def _ensure_schema_migrations_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the schema_migrations table if it does not already exist.

    The DDL here mirrors persistence/operational_schemas.py exactly so that
    the table can be created in either order (operational schemas applied
    first, or migrations applied first).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version                INTEGER     PRIMARY KEY,
            applied_at             TIMESTAMPTZ NOT NULL,
            description            VARCHAR     NOT NULL
        )
        """
    )


def discover_migrations(package_name: str = __name__) -> tuple[Migration, ...]:
    """Discover all migration modules in the named package.

    Returns migrations sorted by version. Modules whose names do not match
    the migration naming convention are ignored (so a ``conftest.py`` or
    similar lives quietly alongside them).

    Raises :class:`MigrationDiscoveryError` if a module has the migration
    naming pattern but lacks the required ``up``/``down`` callables, or if
    two migrations declare the same version number.
    """
    package = importlib.import_module(package_name)
    if not hasattr(package, "__path__"):
        raise MigrationDiscoveryError(f"package {package_name} is not a package (no __path__)")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()

    for mod_info in pkgutil.iter_modules(package.__path__):
        match = _MIGRATION_NAME_RE.match(mod_info.name)
        if not match:
            continue
        version = int(match.group(1))
        description = match.group(2).replace("_", " ")
        full_name = f"{package_name}.{mod_info.name}"
        module = importlib.import_module(full_name)
        if not hasattr(module, "up") or not callable(module.up):
            raise MigrationDiscoveryError(f"{full_name}: missing callable 'up'")
        if not hasattr(module, "down") or not callable(module.down):
            raise MigrationDiscoveryError(f"{full_name}: missing callable 'down'")
        if version in seen_versions:
            raise MigrationDiscoveryError(f"duplicate migration version {version} (in {full_name})")
        seen_versions.add(version)
        migrations.append(
            Migration(
                version=version,
                description=description,
                module_name=full_name,
                module=module,
            )
        )

    migrations.sort(key=lambda m: m.version)
    return tuple(migrations)


def applied_versions(conn: duckdb.DuckDBPyConnection) -> tuple[int, ...]:
    """Return the set of migration versions already applied, sorted ascending."""
    _ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return tuple(int(r[0]) for r in rows)


def run_pending_migrations(
    conn: duckdb.DuckDBPyConnection,
    *,
    package_name: str = __name__,
) -> tuple[Migration, ...]:
    """Apply any migrations that have not yet been applied to this connection.

    Returns the migrations that were just applied (in order). If everything
    is up to date, returns an empty tuple. Each migration is wrapped in its
    own transaction so a partial failure leaves prior migrations applied
    and aborts the failing migration cleanly.

    Raises :class:`MigrationApplicationError` if a migration's ``up()`` raises.
    """
    _ensure_schema_migrations_table(conn)
    discovered = discover_migrations(package_name)
    already = set(applied_versions(conn))
    pending = tuple(m for m in discovered if m.version not in already)

    just_applied: list[Migration] = []
    for migration in pending:
        logger.info(
            "applying migration version=%d description=%r module=%s",
            migration.version,
            migration.description,
            migration.module_name,
        )
        try:
            conn.execute("BEGIN TRANSACTION")
            migration.module.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                [migration.version, datetime.now(tz=UTC), migration.description],
            )
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise MigrationApplicationError(
                f"migration {migration.version} ({migration.description}) failed"
            ) from exc
        just_applied.append(migration)

    return tuple(just_applied)


def rollback_migration(
    conn: duckdb.DuckDBPyConnection,
    version: int,
    *,
    package_name: str = __name__,
) -> Migration:
    """Roll back a single applied migration.

    Only callable explicitly. Raises :class:`MigrationError` if the named
    version is not currently applied or is not discoverable.
    """
    _ensure_schema_migrations_table(conn)
    discovered = {m.version: m for m in discover_migrations(package_name)}
    if version not in discovered:
        raise MigrationError(f"migration version {version} not found in {package_name}")
    if version not in applied_versions(conn):
        raise MigrationError(f"migration version {version} is not currently applied")

    migration = discovered[version]
    try:
        conn.execute("BEGIN TRANSACTION")
        migration.module.down(conn)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", [version])
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        raise MigrationApplicationError(
            f"rollback of migration {version} ({migration.description}) failed"
        ) from exc

    return migration
