"""Read the simulator's metrics file and return the cluster state at one replay minute.

This module opens the metrics file only. It refuses a file that contains scenario labels,
and it never looks for the labels file that sits next to the metrics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

# Same name as simulator.store.METRICS_TABLE; a test keeps them equal. Not imported, so
# this module cannot reach the label-reading helpers that live next to it.
METRICS_TABLE = "metrics"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_US = timedelta(microseconds=1)


class NoSampleError(LookupError):
    """The replay time is earlier than the first sample."""


@dataclass(frozen=True)
class Snapshot:
    """Every series value at one sample minute."""

    ts: datetime
    cluster: str
    values: dict[tuple[str, str], dict[str, float]]

    def objects(self, object_type: str) -> list[str]:
        """Sorted ids of one object type present in this snapshot."""
        return sorted(oid for (otype, oid) in self.values if otype == object_type)


def _to_us(moment: datetime) -> int:
    return (moment.astimezone(UTC) - EPOCH) // ONE_US


def _from_us(micros: int) -> datetime:
    return EPOCH + micros * ONE_US


class Replay:
    """Read-only view of a metrics file, answered at a chosen replay time."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"metrics file not found: {path}")
        self._con = duckdb.connect(str(path), read_only=True)
        self._lock = threading.Lock()
        try:
            self._check_tables(path)
            self._con.execute("SET TimeZone='UTC'")
            row = self._con.execute(
                f"SELECT epoch_us(min(ts)), epoch_us(max(ts)) FROM {METRICS_TABLE}"
            ).fetchone()
        except Exception:
            self._con.close()
            raise
        if row is None or row[0] is None:
            self._con.close()
            raise ValueError(f"{path} has no metrics")
        self.first = _from_us(int(row[0]))
        self.last = _from_us(int(row[1]))

    def _check_tables(self, path: Path) -> None:
        tables = {r[0] for r in self._con.execute("SHOW TABLES").fetchall()}
        if METRICS_TABLE not in tables:
            raise ValueError(f"{path} has no {METRICS_TABLE!r} table; pass the metrics file")
        if tables - {METRICS_TABLE}:
            extra = sorted(tables - {METRICS_TABLE})
            raise ValueError(
                f"{path} holds tables other than {METRICS_TABLE!r}: {extra}; "
                "the mock API reads the metrics file only"
            )

    def snapshot(self, at: datetime | None) -> Snapshot:
        """State at the latest sample at or before `at`; the last sample when `at` is None."""
        target = self.last if at is None else at
        if target < self.first:
            raise NoSampleError(f"no sample at or before {target.isoformat()}")
        with self._lock:
            found = self._con.execute(
                f"SELECT epoch_us(max(ts)) FROM {METRICS_TABLE} WHERE ts <= make_timestamptz(?)",
                [_to_us(target)],
            ).fetchone()
            sample_us = int(found[0]) if found and found[0] is not None else _to_us(self.first)
            rows = self._con.execute(
                "SELECT cluster, object_type, object_id, metric, value "
                f"FROM {METRICS_TABLE} WHERE ts = make_timestamptz(?)",
                [sample_us],
            ).fetchall()
        values: dict[tuple[str, str], dict[str, float]] = {}
        for _, object_type, object_id, metric, value in rows:
            values.setdefault((object_type, object_id), {})[metric] = float(value)
        return Snapshot(_from_us(sample_us), str(rows[0][0]), values)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._con.close()
