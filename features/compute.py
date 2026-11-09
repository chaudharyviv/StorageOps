"""The one feature implementation, used for training (a range of times) and serving (one time).

Both entry points call `iter_feature_blocks`. A feature at time t uses only samples at or
before t: the load is bounded by the last evaluation time, a series that has no sample yet at
t gets no row at t, and every window function looks only backwards from t.

Time resolution is one minute. An evaluation time is floored to the minute, and a sample is
placed in the minute slot it falls in; samples are expected on minute marks, as the simulator
and ingest produce them.

Reads `clean_metrics` only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

from features.functions import FUNCTIONS, Array, Index, SeriesWindows
from features.spec import FeatureSet, FeatureSpec, get_features

CLEAN_TABLE = "clean_metrics"  # same table ingest writes; a test keeps the name in step
MINUTE_US = 60_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

Store = Path | duckdb.DuckDBPyConnection

FEATURE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "ts": pl.Datetime("us", "UTC"),
    "object_type": pl.String,
    "object_id": pl.String,
    "feature_name": pl.String,
    "value": pl.Float64,
}


class FeaturesError(ValueError):
    """The store cannot be used to compute features."""


@dataclass(frozen=True)
class SeriesKey:
    """One metric series: which object, which metric, and when it first has data."""

    object_type: str
    object_id: str
    metric: str
    first_minute: int


def to_utc(moment: datetime) -> datetime:
    """A datetime in UTC; a naive one is taken as UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def minute_of(moment: datetime) -> int:
    """Whole minutes since the epoch, rounding down."""
    return int((to_utc(moment) - EPOCH).total_seconds() // 60)


def datetime_of(minute: int) -> datetime:
    """The start of a minute, in UTC."""
    return EPOCH + timedelta(minutes=minute)


@contextmanager
def connection(store: Store) -> Iterator[duckdb.DuckDBPyConnection]:
    """Use an open connection as is, or open a path read-only for the duration."""
    if isinstance(store, duckdb.DuckDBPyConnection):
        yield store
        return
    if not Path(store).is_file():
        raise FeaturesError(f"store not found: {store}")
    con = duckdb.connect(str(store), read_only=True)
    try:
        yield con
    finally:
        con.close()


def _check_store(con: duckdb.DuckDBPyConnection) -> None:
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if CLEAN_TABLE not in tables:
        raise FeaturesError(f"the store has no {CLEAN_TABLE!r} table; run ingest first")
    row = con.execute(f"SELECT count(DISTINCT cluster) FROM {CLEAN_TABLE}").fetchone()
    if row and int(row[0]) > 1:
        raise FeaturesError("the store holds more than one cluster; features assume one")


def data_span(con: duckdb.DuckDBPyConnection) -> tuple[datetime, datetime] | None:
    """First and last sample time in clean_metrics, or None if it is empty."""
    _check_store(con)
    row = con.execute(f"SELECT epoch_us(min(ts)), epoch_us(max(ts)) FROM {CLEAN_TABLE}").fetchone()
    if row is None or row[0] is None:
        return None
    return (
        EPOCH + timedelta(microseconds=int(row[0])),
        EPOCH + timedelta(microseconds=int(row[1])),
    )


def _series_at_or_before(
    con: duckdb.DuckDBPyConnection, end_minute: int, feature_set: FeatureSet
) -> list[SeriesKey]:
    """Series with a sample at or before the last evaluation time, and features defined."""
    rows = con.execute(
        "SELECT object_type, object_id, metric, epoch_us(min(ts)) "
        f"FROM {CLEAN_TABLE} WHERE ts <= make_timestamptz(?) "
        "GROUP BY object_type, object_id, metric ORDER BY object_type, object_id, metric",
        [end_minute * MINUTE_US],
    ).fetchall()
    keep = {(s.object_type, s.metric) for s in feature_set.specs}
    return [
        SeriesKey(otype, oid, metric, int(first) // MINUTE_US)
        for otype, oid, metric, first in rows
        if (otype, metric) in keep
    ]


def _grid(con: duckdb.DuckDBPyConnection, series: SeriesKey, origin: int, last: int) -> Array:
    """One value per minute from `origin` to `last` inclusive; NaN where there is no sample."""
    data = con.execute(
        f"SELECT epoch_us(ts), value FROM {CLEAN_TABLE} "
        "WHERE object_type = ? AND object_id = ? AND metric = ? "
        "AND ts >= make_timestamptz(?) AND ts <= make_timestamptz(?) ORDER BY ts",
        [series.object_type, series.object_id, series.metric, origin * MINUTE_US, last * MINUTE_US],
    ).fetchnumpy()
    grid = np.full(last - origin + 1, np.nan)
    if len(data["value"]):
        slots = data["epoch_us(ts)"].astype(np.int64) // MINUTE_US - origin
        grid[slots] = np.ma.filled(data["value"].astype(np.float64), np.nan)
    return grid


def _block(
    series: SeriesKey,
    specs: list[FeatureSpec],
    times: Index,
    values: list[Array],
) -> pl.DataFrame:
    n, k = len(times), len(specs)
    ts_us = np.tile(times.astype(np.int64) * MINUTE_US, k)
    return pl.DataFrame(
        {
            "ts": pl.Series(ts_us, dtype=pl.Int64)
            .cast(pl.Datetime("us"))
            .dt.replace_time_zone("UTC"),
            "object_type": pl.Series([series.object_type] * (n * k), dtype=pl.String),
            "object_id": pl.Series([series.object_id] * (n * k), dtype=pl.String),
            "feature_name": pl.Series(
                np.repeat([s.name for s in specs], n).tolist(), dtype=pl.String
            ),
            "value": pl.Series(np.concatenate(values), dtype=pl.Float64, nan_to_null=True),
        }
    )


def iter_feature_blocks(
    store: Store, times: Index, feature_set: FeatureSet | None = None
) -> Iterator[pl.DataFrame]:
    """Yield one long frame per series for the given evaluation minutes (ascending)."""
    feature_set = feature_set or get_features()
    if len(times) == 0:
        return
    last_minute = int(times[-1])
    with connection(store) as con:
        _check_store(con)
        for series in _series_at_or_before(con, last_minute, feature_set):
            mine = times[times >= series.first_minute]  # no row before the series has a sample
            if len(mine) == 0:
                continue
            specs = feature_set.for_series(series.object_type, series.metric)
            origin = int(mine[0]) - (feature_set.max_window - 1)
            grid = _grid(con, series, origin, int(mine[-1]))
            windows = SeriesWindows(
                grid,
                (mine - origin).astype(np.int64),
                feature_set.min_coverage,
                feature_set.level_minutes,
            )
            results = [
                FUNCTIONS[s.function].fn(windows, s.window_minutes, s.param_dict) for s in specs
            ]
            yield _block(series, specs, mine, results)


def _sorted(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.sort(["object_type", "object_id", "feature_name", "ts"])


def _collect(blocks: Iterator[pl.DataFrame]) -> pl.DataFrame:
    frames = list(blocks)
    return _sorted(pl.concat(frames)) if frames else pl.DataFrame(schema=FEATURE_SCHEMA)


def range_minutes(start: datetime, end: datetime, step: int) -> Index:
    """Evaluation minutes from `start` to `end`: multiples of `step` minutes since the epoch."""
    if step < 1:
        raise FeaturesError("step must be at least 1 minute")
    begin = minute_of(start) + (1 if to_utc(start) > datetime_of(minute_of(start)) else 0)
    first = -(-begin // step) * step
    return np.arange(first, minute_of(end) + 1, step, dtype=np.int64)


def compute_features_range(
    store: Store,
    start: datetime,
    end: datetime,
    step: int = 5,
    feature_set: FeatureSet | None = None,
) -> pl.DataFrame:
    """Features for training: every `step` minutes from `start` to `end`, long format."""
    return _collect(iter_feature_blocks(store, range_minutes(start, end, step), feature_set))


def compute_features_at(
    store: Store, t: datetime, feature_set: FeatureSet | None = None
) -> pl.DataFrame:
    """Features for serving: the values the range path gives for time t (floored to a minute)."""
    return _collect(
        iter_feature_blocks(store, np.array([minute_of(t)], dtype=np.int64), feature_set)
    )
