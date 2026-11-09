"""DuckDB output: metrics in one file, scenario labels in a separate file.

The metrics file holds no label data. The labels file is ground truth: only `eval/` may open
it. Features, models, the agent and the dashboard must read the metrics file alone.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import polars as pl

from contracts import ScenarioLabel
from simulator.engine import SimulationResult

METRICS_TABLE = "metrics"
LABELS_TABLE = "scenario_labels"

_METRICS_DDL = f"""
CREATE TABLE {METRICS_TABLE} (
    ts TIMESTAMPTZ NOT NULL,
    cluster VARCHAR NOT NULL,
    object_type VARCHAR NOT NULL,
    object_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    unit VARCHAR NOT NULL
)
"""
_LABELS_DDL = f"""
CREATE TABLE {LABELS_TABLE} (
    label_id VARCHAR PRIMARY KEY,
    scenario VARCHAR NOT NULL,
    variant VARCHAR NOT NULL,
    cluster VARCHAR NOT NULL,
    object_id VARCHAR NOT NULL,
    "start" TIMESTAMPTZ NOT NULL,
    "end" TIMESTAMPTZ NOT NULL
)
"""


def _metrics_frame(result: SimulationResult) -> pl.DataFrame:
    """One long frame of every series, built column-wise rather than record by record."""
    ts = pl.Series("ts", result.timestamps()).dt.replace_time_zone("UTC")
    frames = [
        pl.DataFrame({"ts": ts, "value": values}).with_columns(
            pl.lit(result.cluster).alias("cluster"),
            pl.lit(object_type.value).alias("object_type"),
            pl.lit(object_id).alias("object_id"),
            pl.lit(metric).alias("metric"),
            pl.lit(result.units[(object_type, object_id, metric)]).alias("unit"),
        )
        for (object_type, object_id, metric), values in result.series.items()
    ]
    columns = ["ts", "cluster", "object_type", "object_id", "metric", "value", "unit"]
    return pl.concat(frames).select(columns)


def default_labels_path(metrics_path: Path) -> Path:
    """Labels file next to the metrics file: run.duckdb gives run.labels.duckdb."""
    return metrics_path.with_suffix(".labels.duckdb")


def _prepare(path: Path, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"{path} already exists; pass --overwrite to replace it")
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_metrics(path: Path, result: SimulationResult) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "metrics.parquet"
        _metrics_frame(result).write_parquet(staged)
        con = duckdb.connect(str(path))
        try:
            con.execute(_METRICS_DDL)
            con.execute(
                f"INSERT INTO {METRICS_TABLE} SELECT * FROM read_parquet(?) "
                "ORDER BY ts, object_type, object_id, metric",
                [staged.as_posix()],
            )
        finally:
            con.close()


def _write_labels(path: Path, result: SimulationResult) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(_LABELS_DDL)
        con.executemany(
            f"INSERT INTO {LABELS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lab.label_id,
                    lab.scenario.value,
                    lab.variant.value,
                    lab.cluster,
                    lab.object_id,
                    lab.start,
                    lab.end,
                )
                for lab in result.labels
            ],
        )
    finally:
        con.close()


def write_run(
    metrics_path: Path,
    result: SimulationResult,
    labels_path: Path | None = None,
    overwrite: bool = False,
) -> Path:
    """Write metrics and labels to two separate DuckDB files; return the labels path used."""
    labels_path = labels_path or default_labels_path(metrics_path)
    if labels_path.resolve() == metrics_path.resolve():
        raise ValueError("labels must go to a different file than the metrics")
    for path in (metrics_path, labels_path):  # check both before touching either
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path} already exists; pass --overwrite to replace it")
    _prepare(metrics_path, overwrite)
    _prepare(labels_path, overwrite)
    _write_metrics(metrics_path, result)
    _write_labels(labels_path, result)
    return labels_path


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)


def read_labels(path: Path) -> list[ScenarioLabel]:
    """Read ScenarioLabels from a labels file. Only eval/ (and tests) may call this."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT label_id, scenario, variant, cluster, object_id, "
            f"\"start\" AT TIME ZONE 'UTC', \"end\" AT TIME ZONE 'UTC' FROM {LABELS_TABLE} "
            "ORDER BY label_id"
        ).fetchall()
    finally:
        con.close()
    return [
        ScenarioLabel(
            label_id=r[0],
            scenario=r[1],
            variant=r[2],
            cluster=r[3],
            object_id=r[4],
            start=_as_utc(r[5]),
            end=_as_utc(r[6]),
        )
        for r in rows
    ]


def metric_row_count(path: Path) -> int:
    """Count rows in the metrics table."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        row = con.execute(f"SELECT count(*) FROM {METRICS_TABLE}").fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0
