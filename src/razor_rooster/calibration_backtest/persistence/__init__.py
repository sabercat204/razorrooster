"""Calibration-backtest schemas, migrations, and persistence operations.

Re-exports the public table-name constants, migration-version helpers, the
idempotent insert/update operations (T-CB-010), and the disk-budget guard
entry points (T-CB-012) so callers can
``from razor_rooster.calibration_backtest.persistence import insert_run``
without reaching into :mod:`schemas`, :mod:`operations`, or
:mod:`disk_budget`.
"""

from __future__ import annotations

from razor_rooster.calibration_backtest.persistence.disk_budget import (
    DEFAULT_DISK_CAP_MB,
    assert_under_cap,
    estimate_run_footprint_mb,
    measure_current_footprint_mb,
    projected_disk_usage_mb,
)
from razor_rooster.calibration_backtest.persistence.operations import (
    fetch_predictions,
    fetch_run,
    fetch_run_status,
    fetch_trace,
    insert_prediction,
    insert_predictions_batch,
    insert_run,
    insert_trace,
    list_runs,
    run_exists,
    update_run_status,
)
from razor_rooster.calibration_backtest.persistence.schemas import (
    ALL_DDL,
    ALL_TABLES,
    DDL_PREDICTIONS,
    DDL_RUNS,
    DDL_TRACES,
    INDEXES,
    SCHEMA_NAMESPACE,
    TABLE_PREDICTIONS,
    TABLE_RUNS,
    TABLE_TRACES,
    VERSION_6001,
    VERSION_6002,
    list_ddl,
    list_tables,
)

__all__ = [
    "ALL_DDL",
    "ALL_TABLES",
    "DDL_PREDICTIONS",
    "DDL_RUNS",
    "DDL_TRACES",
    "DEFAULT_DISK_CAP_MB",
    "INDEXES",
    "SCHEMA_NAMESPACE",
    "TABLE_PREDICTIONS",
    "TABLE_RUNS",
    "TABLE_TRACES",
    "VERSION_6001",
    "VERSION_6002",
    "assert_under_cap",
    "estimate_run_footprint_mb",
    "fetch_predictions",
    "fetch_run",
    "fetch_run_status",
    "fetch_trace",
    "insert_prediction",
    "insert_predictions_batch",
    "insert_run",
    "insert_trace",
    "list_ddl",
    "list_runs",
    "list_tables",
    "measure_current_footprint_mb",
    "projected_disk_usage_mb",
    "run_exists",
    "update_run_status",
]
