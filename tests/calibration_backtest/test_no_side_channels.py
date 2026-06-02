"""T-CB-054 — no-side-channels audit and no-circular-dependency static check.

Three pure-AST audits that gate calibration_backtest's architectural
contracts (REQ-CB-PL-002, design §3.2, §3.10, §3.15):

* **Audit 1 — no back-edges** (design §3.15): walk every ``.py`` under the
  seven canonical upstream packages
  (``pattern_library``, ``signal_scanner``, ``mispricing_detector``,
  ``polymarket_connector``, ``data_ingest``, ``report_generator``,
  ``position_engine``) and assert that none of them ``import`` or
  ``from``-import any submodule of
  ``razor_rooster.calibration_backtest``. The reverse direction (CB →
  upstream) is unrestricted by design.

* **Audit 2 — meta-class queries DuckDB directly** (design §3.16): the
  pattern-library meta-class
  ``pattern_library/classes/polymarket_resolution_calibration.py`` must
  consume upstream tables via ``duckdb`` directly and must not import
  anything from ``calibration_backtest``. This protects the dependency
  graph against the most plausible accidental cycle.

* **Audit 3 — side-channel ban** (design §3.10): walk every ``.py`` under
  ``razor_rooster.calibration_backtest`` and assert (a) no network-egress
  modules are imported, and (b) every SQL ``INSERT`` / ``UPDATE`` /
  ``DELETE`` / ``CREATE TABLE`` / ``DROP TABLE`` statement targets one of
  the three CB-owned tables (``backtest_runs``, ``backtest_predictions``,
  ``backtest_traces``) — temp tables prefixed ``tmp_`` are allowed for
  CREATE/DROP only. Upstream tables (``polymarket_resolutions``,
  ``comparison_resolutions``, ``class_market_mappings``, ``comparisons``,
  ``time_series``, ``event_stream``, ``sources``) MUST NOT be mutated.

The audits are deliberately collection-time pytest gates (no
``perf`` / ``slow`` markers) so any architectural regression fails the
default ``pytest -q`` run.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from razor_rooster.calibration_backtest.persistence import schemas as cb_schemas

# ---------------------------------------------------------------------------
# Path helpers — anchor on the installed package roots, not CWD
# ---------------------------------------------------------------------------


def _src_root() -> Path:
    """Return the absolute path to ``src/razor_rooster``.

    Anchored on the calibration_backtest package's ``__file__`` so the
    audit works regardless of where pytest is invoked from. Equivalent to
    ``<repo>/src/razor_rooster``.
    """
    import razor_rooster.calibration_backtest as cb_pkg

    cb_init = Path(cb_pkg.__file__).resolve()
    # cb_init = <repo>/src/razor_rooster/calibration_backtest/__init__.py
    return cb_init.parent.parent


def _iter_py_files(root: Path) -> Iterator[Path]:
    """Yield every ``.py`` under *root* in deterministic sorted order.

    Sorted output makes failure messages stable so the operator can diff
    them across CI runs.
    """
    yield from sorted(p for p in root.rglob("*.py") if p.is_file())


# ---------------------------------------------------------------------------
# Canonical upstream-package list (REQ-CB-PL-002, scout amendment 2026-06-01)
# ---------------------------------------------------------------------------

CANONICAL_UPSTREAM_PACKAGES: Final[tuple[str, ...]] = (
    "pattern_library",
    "signal_scanner",
    "mispricing_detector",
    "polymarket_connector",
    "data_ingest",
    "report_generator",
    "position_engine",
)


# Tables CB is permitted to mutate (TABLE_RUNS / _PREDICTIONS / _TRACES).
ALLOWED_CB_TABLES: Final[frozenset[str]] = frozenset(
    {
        cb_schemas.TABLE_RUNS,
        cb_schemas.TABLE_PREDICTIONS,
        cb_schemas.TABLE_TRACES,
    }
)


# Upstream tables CB MUST NOT mutate (design §3.10 — read-only freezer).
FORBIDDEN_UPSTREAM_TABLES: Final[frozenset[str]] = frozenset(
    {
        "polymarket_resolutions",
        "comparison_resolutions",
        "class_market_mappings",
        "comparisons",
        "time_series",
        "event_stream",
        "sources",
    }
)


# Network-egress modules a deterministic replay layer must never touch.
FORBIDDEN_NETWORK_MODULES: Final[frozenset[str]] = frozenset(
    {
        "requests",
        "urllib",
        "urllib.request",
        "httpx",
        "socket",
        "http.client",
    }
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    """Parse *path* with the standard library AST and tag the filename."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_targets(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    """Return every fully qualified module name an import node references.

    For ``import foo, foo.bar`` the result is ``("foo", "foo.bar")``. For
    ``from foo.bar import baz, qux`` the result is
    ``("foo.bar", "foo.bar.baz", "foo.bar.qux")`` — both the module being
    imported from AND the dotted-suffix forms of each imported name, so a
    later ``startswith`` check catches both
    ``from razor_rooster import calibration_backtest`` and
    ``from razor_rooster.calibration_backtest import api`` shapes.
    """
    targets: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            targets.append(alias.name)
    else:  # ast.ImportFrom
        if node.module is not None:
            targets.append(node.module)
            for alias in node.names:
                targets.append(f"{node.module}.{alias.name}")
    return tuple(targets)


def _is_calibration_backtest_target(target: str) -> bool:
    """True iff *target* refers to the calibration_backtest package."""
    if target in ("calibration_backtest", "razor_rooster.calibration_backtest"):
        return True
    return target.startswith("calibration_backtest.") or target.startswith(
        "razor_rooster.calibration_backtest."
    )


# ---------------------------------------------------------------------------
# Audit 1 — no back-edges from canonical 7 packages to calibration_backtest
# ---------------------------------------------------------------------------


def test_no_back_edges_from_upstream_packages_to_calibration_backtest() -> None:
    """No upstream package may ``import`` calibration_backtest (REQ-CB-PL-002).

    Walk the AST of every ``.py`` under each of the seven canonical
    upstream packages and assert that every Import / ImportFrom target
    fails :func:`_is_calibration_backtest_target`. Failure surfaces a
    sorted list ``(file:line: stmt)`` so the operator can locate the
    offending edge directly.
    """
    src = _src_root()
    offenders: list[str] = []

    for pkg_name in CANONICAL_UPSTREAM_PACKAGES:
        pkg_root = src / pkg_name
        assert pkg_root.is_dir(), f"canonical upstream package missing on disk: {pkg_root}"
        for py_file in _iter_py_files(pkg_root):
            tree = _parse(py_file)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import | ast.ImportFrom):
                    continue
                for target in _module_targets(node):
                    if _is_calibration_backtest_target(target):
                        offenders.append(f"{py_file}:{node.lineno}: imports {target!r}")

    assert offenders == [], (
        "Back-edge from canonical upstream package to "
        "razor_rooster.calibration_backtest detected (REQ-CB-PL-002, "
        "design §3.15):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Audit 2 — meta-class queries DuckDB directly, no calibration_backtest import
# ---------------------------------------------------------------------------


def _meta_class_path() -> Path:
    """Locate the polymarket_resolution_calibration meta-class on disk.

    The meta-class lives under
    ``pattern_library/classes/polymarket_resolution_calibration.py`` per
    the live tree (the spec text mentions a ``meta/`` subpath, but the
    scout amendment confirmed the canonical location is
    ``classes/polymarket_resolution_calibration.py``).
    """
    src = _src_root()
    candidates = (
        src / "pattern_library" / "classes" / "polymarket_resolution_calibration.py",
        src / "pattern_library" / "classes" / "meta" / "polymarket_resolution_calibration.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "polymarket_resolution_calibration meta-class not found at any "
        f"expected location: {[str(c) for c in candidates]}"
    )


def test_meta_class_queries_duckdb_directly() -> None:
    """Meta-class imports duckdb and does NOT import calibration_backtest.

    Verifies design §3.16 / REQ-CB-PL-002: the pattern_library meta-class
    must consume upstream tables via DuckDB directly. Importing anything
    from calibration_backtest would create a circular dependency.
    """
    path = _meta_class_path()
    tree = _parse(path)

    imports_duckdb = False
    cb_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for target in _module_targets(node):
            if target == "duckdb" or target.startswith("duckdb."):
                imports_duckdb = True
            if _is_calibration_backtest_target(target):
                cb_imports.append(f"{path}:{node.lineno}: imports {target!r}")

    assert cb_imports == [], (
        "pattern_library meta-class must not import calibration_backtest "
        "(design §3.16, REQ-CB-PL-002):\n  " + "\n  ".join(cb_imports)
    )
    assert imports_duckdb, (
        f"pattern_library meta-class at {path} must import 'duckdb' to "
        "query upstream tables directly (design §3.16)."
    )


# ---------------------------------------------------------------------------
# Audit 3 — side-channel: no network egress, no foreign-table writes
# ---------------------------------------------------------------------------


# Variable names that can substitute for a literal table name inside an
# f-string SQL statement. ``{TABLE_RUNS}`` / ``{TABLE_PREDICTIONS}`` /
# ``{TABLE_TRACES}`` are direct table aliases; ``{table}`` is the loop
# variable used inside ``m6001.down()`` which iterates ``ALL_TABLES`` (see
# migrations/m6001_calibration_backtest_initial.py:74-75).
_SQL_NAME_ALIASES: Final[dict[str, frozenset[str]]] = {
    "TABLE_RUNS": frozenset({cb_schemas.TABLE_RUNS}),
    "TABLE_PREDICTIONS": frozenset({cb_schemas.TABLE_PREDICTIONS}),
    "TABLE_TRACES": frozenset({cb_schemas.TABLE_TRACES}),
    "table": ALLOWED_CB_TABLES,
}


def _concretize_string(node: ast.AST) -> str | None:
    """Reduce a string-or-fstring AST node to a flat audit string.

    Returns ``None`` when the node is neither a ``str`` ``Constant`` nor
    a ``JoinedStr`` (i.e. not a recognisable string literal). For
    f-strings every ``FormattedValue`` whose expression is a bare
    ``Name`` is replaced by ``<<NAME>>`` so the SQL regex can later
    substitute the alias against :data:`_SQL_NAME_ALIASES`. Format spec /
    conversion are ignored — only the variable identity matters for the
    audit.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                inner = value.value
                if isinstance(inner, ast.Name):
                    parts.append(f"<<{inner.id}>>")
                    continue
                # Attribute / call / subscript expressions inside f-strings
                # cannot be statically resolved here. Use a sentinel so the
                # SQL audit can flag the statement explicitly rather than
                # silently approving an unknown table reference.
                parts.append("<<UNKNOWN>>")
                continue
            # Any other node type is not part of a valid f-string AST.
            parts.append("<<UNKNOWN>>")
        return "".join(parts)
    return None


def _collect_docstring_nodes(tree: ast.Module) -> set[int]:
    """Return the ``id()`` of every ``Constant`` node used as a docstring.

    A docstring is the first statement of a Module / FunctionDef /
    AsyncFunctionDef / ClassDef body when that statement is an
    ``Expr`` whose ``value`` is a ``Constant`` of type ``str``. SQL-like
    text inside docstrings is prose, not executed code, so the SQL audit
    skips these nodes to avoid flagging design notes (e.g. textual
    references to ``CREATE TABLE IF NOT EXISTS`` in module headers).
    """
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node,
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ):
            continue
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstring_ids.add(id(first.value))
    return docstring_ids


def _collect_joinedstr_child_ids(tree: ast.Module) -> set[int]:
    """Return ``id()`` of every Constant nested inside a JoinedStr.

    ``ast.walk`` yields f-string children alongside the parent JoinedStr,
    so a literal like ``f"DROP TABLE IF EXISTS {table}"`` would surface
    its inner ``Constant('DROP TABLE IF EXISTS ')`` as a second pass —
    truncated to drop the substituted variable. The audit relies on the
    JoinedStr-level concretisation (which preserves the ``<<table>>``
    placeholder), so the inner Constants must be filtered out to avoid
    spurious partial-match failures.
    """
    nested_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for child in node.values:
            nested_ids.add(id(child))
            # FormattedValue.format_spec may itself be a JoinedStr that
            # nests further Constants; recurse defensively.
            if isinstance(child, ast.FormattedValue) and child.format_spec is not None:
                for inner in ast.walk(child.format_spec):
                    nested_ids.add(id(inner))
    return nested_ids


def _iter_string_nodes(
    tree: ast.Module,
) -> Iterator[tuple[ast.Constant | ast.JoinedStr, str]]:
    """Yield every concretised string in *tree* with its originating node.

    Docstrings (per :func:`_collect_docstring_nodes`) and Constants
    nested inside an f-string (per :func:`_collect_joinedstr_child_ids`)
    are skipped — the former is prose, the latter would emit a truncated
    half of the f-string's logical text and produce false positives. The
    yielded node is guaranteed to be either ``ast.Constant`` (string
    literal) or ``ast.JoinedStr`` (f-string) so the caller can read
    ``node.lineno`` without a cast.
    """
    docstring_ids = _collect_docstring_nodes(tree)
    nested_ids = _collect_joinedstr_child_ids(tree)
    for node in ast.walk(tree):
        if id(node) in docstring_ids or id(node) in nested_ids:
            continue
        if isinstance(node, ast.JoinedStr):
            text = _concretize_string(node)
            if text is not None:
                yield node, text
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value


# Regex matches the SQL action keywords plus the immediately following
# table-name token. Re-uses ``re.IGNORECASE`` so lowercase ``insert into``
# or mixed-case statements would also be caught. The table-name capture
# accepts the f-string alias form ``<<NAME>>`` as well as bareword
# identifiers.
_SQL_STATEMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|"
    r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+"
    r"(<<[A-Za-z_][A-Za-z0-9_]*>>|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _resolve_table_aliases(token: str) -> frozenset[str]:
    """Map a captured token to the concrete table names it can refer to.

    Bareword tokens map to themselves (a single-element set). Alias
    tokens of the form ``<<NAME>>`` resolve via :data:`_SQL_NAME_ALIASES`;
    unknown aliases (``<<UNKNOWN>>`` or any unrecognised name) yield an
    empty set so the caller can flag them as unresolved.
    """
    if not token.startswith("<<"):
        return frozenset({token})
    inner = token[2:-2]
    return _SQL_NAME_ALIASES.get(inner, frozenset())


def _is_temp_table(name: str) -> bool:
    """Allow CREATE/DROP statements that target ``tmp_``-prefixed tables."""
    return name.startswith("tmp_")


def test_calibration_backtest_imports_no_network_modules() -> None:
    """CB must not import any network-egress module (design §3.10).

    The replay path is deterministic over frozen DuckDB state; reaching
    out to the network would silently violate the freezer contract.
    """
    cb_root = _src_root() / "calibration_backtest"
    offenders: list[str] = []

    for py_file in _iter_py_files(cb_root):
        tree = _parse(py_file)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for target in _module_targets(node):
                # Exact-match against the forbidden set covers
                # ``socket``, ``urllib``, ``urllib.request``, ``http.client``
                # etc. ``urllib.parse`` is intentionally NOT in the set:
                # it is pure parsing, no egress.
                if target in FORBIDDEN_NETWORK_MODULES:
                    offenders.append(f"{py_file}:{node.lineno}: imports {target!r}")

    assert offenders == [], (
        "calibration_backtest must not import network-egress modules "
        "(design §3.10):\n  " + "\n  ".join(offenders)
    )


def test_calibration_backtest_sql_writes_only_target_owned_tables() -> None:
    """Every CB SQL write must target backtest_{runs,predictions,traces}.

    Walks every string literal / f-string under
    ``src/razor_rooster/calibration_backtest`` and parses out SQL action
    statements with :data:`_SQL_STATEMENT_RE`. Each captured table token
    is resolved through :data:`_SQL_NAME_ALIASES`; the resulting concrete
    table name set must be a subset of :data:`ALLOWED_CB_TABLES` (or, for
    CREATE/DROP only, a ``tmp_``-prefixed scratch table).

    Failure modes are reported with ``(file:line:col: action table)`` so
    the operator can pinpoint the offending statement directly.
    """
    cb_root = _src_root() / "calibration_backtest"
    offenders: list[str] = []

    for py_file in _iter_py_files(cb_root):
        tree = _parse(py_file)
        for node, text in _iter_string_nodes(tree):
            for match in _SQL_STATEMENT_RE.finditer(text):
                action_raw = match.group(1).upper()
                action = re.sub(r"\s+", " ", action_raw)
                table_token = match.group(2)
                resolved = _resolve_table_aliases(table_token)

                if not resolved:
                    offenders.append(
                        f"{py_file}:{node.lineno}: unresolved {action} "
                        f"target {table_token!r} (statement: {text!r})"
                    )
                    continue

                is_create_or_drop = action.startswith("CREATE TABLE") or action.startswith(
                    "DROP TABLE"
                )

                for table in resolved:
                    if table in ALLOWED_CB_TABLES:
                        continue
                    if is_create_or_drop and _is_temp_table(table):
                        continue
                    offenders.append(
                        f"{py_file}:{node.lineno}: {action} {table!r} is not a CB-owned table"
                    )

    assert offenders == [], (
        "calibration_backtest SQL writes must target only CB-owned tables "
        "(design §3.10, schemas.TABLE_*):\n  " + "\n  ".join(offenders)
    )


def test_calibration_backtest_does_not_mutate_upstream_tables() -> None:
    """CB must not INSERT / UPDATE / DELETE on upstream-owned tables.

    Stronger sibling assertion to
    :func:`test_calibration_backtest_sql_writes_only_target_owned_tables`:
    even if a future contributor introduced a brand-new table outside
    :data:`ALLOWED_CB_TABLES`, this test guarantees the most dangerous
    foreign tables remain untouched. Acts as a denylist backstop on top
    of the allowlist gate above.
    """
    cb_root = _src_root() / "calibration_backtest"
    offenders: list[str] = []

    for py_file in _iter_py_files(cb_root):
        tree = _parse(py_file)
        for node, text in _iter_string_nodes(tree):
            for match in _SQL_STATEMENT_RE.finditer(text):
                action_raw = match.group(1).upper()
                action = re.sub(r"\s+", " ", action_raw)
                # CREATE/DROP TABLE on upstream tables is also banned, but
                # the focus of this assertion is mutation of live rows.
                if not (
                    action.startswith("INSERT INTO")
                    or action.startswith("UPDATE")
                    or action.startswith("DELETE FROM")
                ):
                    continue
                table_token = match.group(2)
                resolved = _resolve_table_aliases(table_token)
                for table in resolved:
                    if table in FORBIDDEN_UPSTREAM_TABLES:
                        offenders.append(
                            f"{py_file}:{node.lineno}: {action} {table!r} "
                            "(upstream table, mutation forbidden)"
                        )

    assert offenders == [], (
        "calibration_backtest must never mutate upstream tables "
        "(design §3.10):\n  " + "\n  ".join(offenders)
    )
