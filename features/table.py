"""The `features` table in the store, and a helper that pivots it to one frame per object type.

One row per (ts, object_type, object_id, feature_name). A null value means "not computable
here", for example a window with too little data. Writing is idempotent: rows are keyed and
replaced, so computing the same range twice leaves the table unchanged.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

from features.compute import (
    EPOCH,
    FEATURE_SCHEMA,
    FeaturesError,
    connection,
    data_span,
    datetime_of,
    iter_feature_blocks,
    range_minutes,
)
from features.spec import FeatureSet, get_features

FEATURES_TABLE = "features"
MINUTE_US = 60_000_000

DDL = f"""
CREATE TABLE IF NOT EXISTS {FEATURES_TABLE} (
    ts TIMESTAMPTZ NOT NULL,
    object_type VARCHAR NOT NULL,
    object_id VARCHAR NOT NULL,
    feature_name VARCHAR NOT NULL,
    value DOUBLE,
    PRIMARY KEY (object_type, object_id, feature_name, ts)
)
"""


@dataclass(frozen=True)
class WriteSummary:
    """What a write computed."""

    rows: int
    series: int
    times: int
    start: datetime
    end: datetime


def write_features(
    store_path: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    step: int = 5,
    rebuild: bool = False,
    feature_set: FeatureSet | None = None,
) -> WriteSummary:
    """Compute features over a range and upsert them into the store's `features` table."""
    feature_set = feature_set or get_features()
    if not store_path.is_file():
        raise FeaturesError(f"store not found: {store_path}")
    con = duckdb.connect(str(store_path))
    try:
        span = data_span(con)
        if span is None:
            raise FeaturesError("clean_metrics is empty; nothing to compute")
        times = range_minutes(start or span[0], end or span[1], step)
        if len(times) == 0:
            raise FeaturesError("the requested range holds no evaluation times")
        if rebuild:
            con.execute(f"DROP TABLE IF EXISTS {FEATURES_TABLE}")
        con.execute(DDL)
        rows = series = 0
        con.execute("BEGIN TRANSACTION")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / "block.parquet"
                for block in iter_feature_blocks(con, times, feature_set):
                    block.write_parquet(staged)
                    con.execute(
                        f"INSERT OR REPLACE INTO {FEATURES_TABLE} "
                        "SELECT ts, object_type, object_id, feature_name, value "
                        "FROM read_parquet(?)",
                        [staged.as_posix()],
                    )
                    rows += block.height
                    series += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return WriteSummary(
            rows, series, len(times), datetime_of(int(times[0])), datetime_of(int(times[-1]))
        )
    finally:
        con.close()


def pivot_features(long: pl.DataFrame, object_type: str) -> pl.DataFrame:
    """One wide frame for an object type: a row per (ts, object_id), a column per feature."""
    wanted = long.filter(pl.col("object_type") == object_type)
    if wanted.is_empty():
        return pl.DataFrame(schema={"ts": FEATURE_SCHEMA["ts"], "object_id": pl.String})
    wide = wanted.pivot(on="feature_name", index=["ts", "object_id"], values="value")
    columns = sorted(c for c in wide.columns if c not in ("ts", "object_id"))
    return wide.select(["ts", "object_id", *columns]).sort(["object_id", "ts"])


def read_features(
    store: Path | duckdb.DuckDBPyConnection,
    object_type: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pl.DataFrame:
    """Read stored features for one object type, pivoted wide."""
    with connection(store) as con:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if FEATURES_TABLE not in tables:
            raise FeaturesError("the store has no features table; run features.run first")
        where = "WHERE object_type = ?"
        params: list[object] = [object_type]
        if start is not None:
            where += " AND ts >= make_timestamptz(?)"
            params.append(_us(start))
        if end is not None:
            where += " AND ts <= make_timestamptz(?)"
            params.append(_us(end))
        data = con.execute(
            f"SELECT epoch_us(ts) AS ts_us, object_id, feature_name, value "
            f"FROM {FEATURES_TABLE} {where}",
            params,
        ).fetchnumpy()
    n = len(data["ts_us"])
    long = pl.DataFrame(
        {
            "ts": pl.Series(data["ts_us"], dtype=pl.Int64)
            .cast(pl.Datetime("us"))
            .dt.replace_time_zone("UTC"),
            "object_type": pl.Series([object_type] * n, dtype=pl.String),
            "object_id": pl.Series(data["object_id"].tolist(), dtype=pl.String),
            "feature_name": pl.Series(data["feature_name"].tolist(), dtype=pl.String),
            "value": pl.Series(
                np.ma.filled(data["value"].astype(np.float64), np.nan),
                dtype=pl.Float64,
                nan_to_null=True,
            ),
        }
    )
    return pivot_features(long, object_type)


def _us(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(microseconds=1)
