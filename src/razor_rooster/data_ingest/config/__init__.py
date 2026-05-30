"""Configuration loading for ``data_ingest`` (T-022).

Loads and validates the YAML config files that drive the ingest cycle:

- ``config/ingest_schedule.yaml`` — per-source cadence and freshness thresholds
  (REQ-SCHED-001, design §7.1).
- ``config/source_caps.yaml`` — global corpus cap, warn/pause percentages, and
  per-source size and depth caps (REQ-BACKFILL-003, design §7.2).

Validation uses Pydantic v2 models with strict types. Invalid configs raise
on load so the cycle aborts at startup rather than ingesting against
malformed inputs.
"""

from .loader import (
    ConfigError,
    GlobalCaps,
    IngestScheduleConfig,
    PerSourceCaps,
    SourceCapsConfig,
    SourceSchedule,
    load_ingest_schedule,
    load_source_caps,
)

__all__ = [
    "ConfigError",
    "GlobalCaps",
    "IngestScheduleConfig",
    "PerSourceCaps",
    "SourceCapsConfig",
    "SourceSchedule",
    "load_ingest_schedule",
    "load_source_caps",
]
