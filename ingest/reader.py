"""Replay reader: stream a simulator metrics file in time order, in batches of whole minutes.

It opens the metrics file only, read-only, and refuses a file that holds any other table.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

import duckdb
import polars as pl

METRICS_TABLE = "metrics"
MINUTE_US = 60_000_000

# Wire layout of a batch. Timestamps travel as integer microseconds since the epoch (UTC).
BATCH_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "ts_us": pl.Int64,
    "cluster": pl.String,
    "object_type": pl.String,
    "object_id": pl.String,
    "metric": pl.String,
    "value": pl.Float64,
    "unit": pl.String,
}


class SourceError(ValueError):
    """The metrics file is missing, empty, or not a metrics-only file."""


class ReplayReader:
    """Read-only, time-ordered batches of a metrics file."""

    def __init__(self, path: Path, batch_minutes: int = 60) -> None:
        if batch_minutes < 1:
            raise SourceError("batch_minutes must be at least 1")
        if not path.is_file():
            raise SourceError(f"metrics file not found: {path}")
        self.path = path
        self.batch_minutes = batch_minutes
        self._con = duckdb.connect(str(path), read_only=True)
        try:
            tables = {r[0] for r in self._con.execute("SHOW TABLES").fetchall()}
            if METRICS_TABLE not in tables or tables - {METRICS_TABLE}:
                raise SourceError(
                    f"{path} must hold only the {METRICS_TABLE!r} table (found {sorted(tables)}); "
                    "pass the metrics file"
                )
            self._con.execute("SET TimeZone='UTC'")
            row = self._con.execute(
                f"SELECT epoch_us(min(ts)), epoch_us(max(ts)), count(*) FROM {METRICS_TABLE}"
            ).fetchone()
        except Exception:
            self._con.close()
            raise
        if row is None or row[0] is None:
            self._con.close()
            raise SourceError(f"{path} has no metrics")
        self.first_us, self.last_us, self.rows = int(row[0]), int(row[1]), int(row[2])

    def batches(self) -> Iterator[pl.DataFrame]:
        """Yield time-ordered batches, each covering `batch_minutes` minutes of the file."""
        width = self.batch_minutes * MINUTE_US
        start = self.first_us
        while start <= self.last_us:
            rows = self._con.execute(
                "SELECT epoch_us(ts), cluster, object_type, object_id, metric, value, unit "
                f"FROM {METRICS_TABLE} "
                "WHERE ts >= make_timestamptz(?) AND ts < make_timestamptz(?) "
                "ORDER BY ts, object_type, object_id, metric",
                [start, start + width],
            ).fetchall()
            if rows:
                columns = list(zip(*rows, strict=True))
                yield pl.DataFrame(
                    {name: list(col) for name, col in zip(BATCH_SCHEMA, columns, strict=True)},
                    schema=BATCH_SCHEMA,
                )
            start += width

    def close(self) -> None:
        """Close the database connection."""
        self._con.close()

    def __enter__(self) -> ReplayReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
