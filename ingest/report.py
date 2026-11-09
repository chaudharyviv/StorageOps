"""The validation report: a stable, versioned format that any validator can produce.

Every validator, the custom checks today and a Great Expectations adapter later, reports in
the same terms: a `FaultCode` per finding, a `BatchResult` per batch and one `RunReport` per
run. Keep this module stable. Changing a field name or meaning means a new `SCHEMA_VERSION`.

Counts: for row faults the unit is rows; for `gap` it is missing minutes (one gap can hide
many); for `schema_batch` it is the rows in the rejected batch. A row with several faults is
counted once under each code but quarantined once, under the first code in `FaultCode` order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1


class FaultCode(StrEnum):
    """Why a row, batch or stretch of time was quarantined. Order is the precedence."""

    SCHEMA_BATCH = "schema_batch"
    SCHEMA_ROW = "schema_row"
    UNKNOWN_METRIC = "unknown_metric"
    UNIT_MISMATCH = "unit_mismatch"
    OUT_OF_RANGE = "out_of_range"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    GAP = "gap"


DESCRIPTIONS = {
    FaultCode.SCHEMA_BATCH: "Batch columns or types differ from the MetricRecord layout (rows)",
    FaultCode.SCHEMA_ROW: "Row fails the MetricRecord contract (rows)",
    FaultCode.UNKNOWN_METRIC: "Metric or object type is not in docs/METRICS.md (rows)",
    FaultCode.UNIT_MISMATCH: "Unit differs from the catalogue unit for the metric (rows)",
    FaultCode.OUT_OF_RANGE: "Value is outside the bounds in docs/metric_rules.yaml (rows)",
    FaultCode.DUPLICATE: "Same series and timestamp seen again (rows)",
    FaultCode.OUT_OF_ORDER: "Timestamp older than the latest seen for the series (rows)",
    FaultCode.GAP: "Minutes missing between samples of a series (minutes)",
}


@dataclass(frozen=True)
class Quarantined:
    """One quarantine entry: a bad row, a rejected batch or a gap."""

    key: str
    batch_id: int | None
    reason: FaultCode
    reasons: tuple[FaultCode, ...]
    detail: str
    object_type: str | None = None
    object_id: str | None = None
    metric: str | None = None
    ts_us: int | None = None
    payload: str | None = None


@dataclass
class BatchResult:
    """What a validator returns for one batch (or, from `finish`, for the end of the stream)."""

    batch_id: int | None
    rows: int
    clean: pl.DataFrame
    quarantined: list[Quarantined] = field(default_factory=list)
    counts: dict[FaultCode, int] = field(default_factory=dict)
    rejected: bool = False
    rows_quarantined: int = 0


class CheckSummary(BaseModel):
    """Totals for one check over the whole run. Every code is always listed."""

    model_config = ConfigDict(frozen=True)

    code: str
    description: str
    failed: int


class BatchSummary(BaseModel):
    """One batch that had at least one fault."""

    model_config = ConfigDict(frozen=True)

    batch_id: int
    rows: int
    clean: int
    quarantined: int
    faults: dict[str, int]


class RunReport(BaseModel):
    """The report for one ingest run. Stored as JSON in validation_runs.report."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    run_id: str
    status: Literal["passed", "failed"]
    metrics_path: str
    started_at: datetime
    finished_at: datetime
    batch_minutes: int
    batches: int
    rows_read: int
    rows_clean: int
    rows_quarantined: int
    rows_loaded: int
    rows_already_present: int
    checks: list[CheckSummary]
    faulty_batches: list[BatchSummary]
