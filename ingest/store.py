"""The ingest store: clean_metrics, quarantine and validation_runs in one DuckDB file.

Loading is idempotent: clean rows have a primary key of series plus timestamp and are
inserted with "ignore on conflict"; quarantine entries have a deterministic key. Running the
same ingest twice therefore leaves both tables unchanged and only adds a validation_runs row.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from types import TracebackType

import duckdb
import polars as pl

from ingest.report import Quarantined, RunReport

CLEAN_TABLE = "clean_metrics"
QUARANTINE_TABLE = "quarantine"
RUNS_TABLE = "validation_runs"

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {CLEAN_TABLE} (
        ts TIMESTAMPTZ NOT NULL,
        cluster VARCHAR NOT NULL,
        object_type VARCHAR NOT NULL,
        object_id VARCHAR NOT NULL,
        metric VARCHAR NOT NULL,
        value DOUBLE NOT NULL,
        unit VARCHAR NOT NULL,
        PRIMARY KEY (cluster, object_type, object_id, metric, ts)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE} (
        quarantine_key VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        batch_id BIGINT,
        reason VARCHAR NOT NULL,
        reasons VARCHAR NOT NULL,
        detail VARCHAR NOT NULL,
        object_type VARCHAR,
        object_id VARCHAR,
        metric VARCHAR,
        ts TIMESTAMPTZ,
        payload VARCHAR
    )""",
    f"""CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
        run_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        metrics_path VARCHAR NOT NULL,
        batch_minutes INTEGER NOT NULL,
        batches INTEGER,
        rows_read BIGINT,
        rows_clean BIGINT,
        rows_quarantined BIGINT,
        rows_loaded BIGINT,
        rows_already_present BIGINT,
        fault_counts VARCHAR,
        report VARCHAR
    )""",
)
_QUARANTINE_SCHEMA = {
    "quarantine_key": pl.String, "run_id": pl.String, "batch_id": pl.Int64,
    "reason": pl.String, "reasons": pl.String, "detail": pl.String,
    "object_type": pl.String, "object_id": pl.String, "metric": pl.String,
    "ts_us": pl.Int64, "payload": pl.String,
}  # fmt: skip


class IngestStore:
    """Writes validated metrics, quarantined findings and run records."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._con = duckdb.connect(str(path))
        self._con.execute("SET TimeZone='UTC'")
        for statement in _DDL:
            self._con.execute(statement)

    def _count(self, table: str) -> int:
        row = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    def start_run(
        self, run_id: str, started: datetime, metrics_path: str, batch_minutes: int
    ) -> None:
        """Record that a run has begun."""
        self._con.execute(
            f"INSERT INTO {RUNS_TABLE} (run_id, started_at, status, metrics_path, batch_minutes) "
            "VALUES (?, ?, 'running', ?, ?)",
            [run_id, started, metrics_path, batch_minutes],
        )

    def load_batch(self, run_id: str, clean: pl.DataFrame, quarantined: list[Quarantined]) -> int:
        """Load one batch atomically; return how many clean rows were new to the store."""
        before = self._count(CLEAN_TABLE)
        self._con.execute("BEGIN TRANSACTION")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if clean.height:
                    staged = Path(tmp) / "clean.parquet"
                    clean.write_parquet(staged)
                    self._con.execute(
                        f"INSERT OR IGNORE INTO {CLEAN_TABLE} SELECT make_timestamptz(ts_us), "
                        "cluster, object_type, object_id, metric, value, unit FROM read_parquet(?)",
                        [staged.as_posix()],
                    )
                if quarantined:
                    staged = Path(tmp) / "quarantine.parquet"
                    self._quarantine_frame(run_id, quarantined).write_parquet(staged)
                    self._con.execute(
                        f"INSERT OR IGNORE INTO {QUARANTINE_TABLE} SELECT quarantine_key, run_id, "
                        "batch_id, reason, reasons, detail, object_type, object_id, metric, "
                        "CASE WHEN ts_us IS NULL THEN NULL ELSE make_timestamptz(ts_us) END, "
                        "payload FROM read_parquet(?)",
                        [staged.as_posix()],
                    )
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        return self._count(CLEAN_TABLE) - before

    @staticmethod
    def _quarantine_frame(run_id: str, entries: list[Quarantined]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "quarantine_key": [e.key for e in entries],
                "run_id": [run_id] * len(entries),
                "batch_id": [e.batch_id for e in entries],
                "reason": [e.reason.value for e in entries],
                "reasons": [",".join(r.value for r in e.reasons) for e in entries],
                "detail": [e.detail for e in entries],
                "object_type": [e.object_type for e in entries],
                "object_id": [e.object_id for e in entries],
                "metric": [e.metric for e in entries],
                "ts_us": [e.ts_us for e in entries],
                "payload": [e.payload for e in entries],
            },
            schema=_QUARANTINE_SCHEMA,
        )

    def finish_run(self, report: RunReport, fault_counts: str) -> None:
        """Record the outcome of a run, with its full report as JSON."""
        self._con.execute(
            f"UPDATE {RUNS_TABLE} SET finished_at = ?, status = ?, batches = ?, rows_read = ?, "
            "rows_clean = ?, rows_quarantined = ?, rows_loaded = ?, rows_already_present = ?, "
            "fault_counts = ?, report = ? WHERE run_id = ?",
            [
                report.finished_at, report.status, report.batches, report.rows_read,
                report.rows_clean, report.rows_quarantined, report.rows_loaded,
                report.rows_already_present, fault_counts, report.model_dump_json(), report.run_id,
            ],
        )  # fmt: skip

    def close(self) -> None:
        """Close the database connection."""
        self._con.close()

    def __enter__(self) -> IngestStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
