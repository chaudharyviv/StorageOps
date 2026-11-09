"""Wire it together: reader, file queue, validator, store.

`produce` and `consume` are separate so the two halves can run in different processes later.
`run_ingest` runs both in one process, draining the queue after every batch so it stays small.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ingest.file_queue import FileQueue
from ingest.report import (
    DESCRIPTIONS,
    BatchResult,
    BatchSummary,
    CheckSummary,
    FaultCode,
    RunReport,
)
from ingest.rules import MetricRule, load_rules
from ingest.store import IngestStore
from ingest.validate import BatchValidator, StreamValidator


class Totals:
    """Running totals for one run."""

    def __init__(self) -> None:
        self.batches = 0
        self.rows_read = 0
        self.rows_clean = 0
        self.rows_quarantined = 0
        self.rows_loaded = 0
        self.counts: dict[FaultCode, int] = {}
        self.faulty: list[BatchSummary] = []

    def add(self, result: BatchResult, loaded: int) -> None:
        self.rows_read += result.rows
        self.rows_clean += result.clean.height
        self.rows_quarantined += result.rows_quarantined
        self.rows_loaded += loaded
        for code, count in result.counts.items():
            self.counts[code] = self.counts.get(code, 0) + count
        if result.counts and result.batch_id is not None:
            self.faulty.append(
                BatchSummary(
                    batch_id=result.batch_id,
                    rows=result.rows,
                    clean=result.clean.height,
                    quarantined=result.rows_quarantined,
                    faults={c.value: n for c, n in result.counts.items()},
                )
            )


def produce(frames: Iterable[pl.DataFrame], queue: FileQueue) -> int:
    """Put every batch on the queue; return how many were queued."""
    queued = 0
    for frame in frames:
        queue.put(frame)
        queued += 1
    return queued


def consume(
    queue: FileQueue,
    validator: BatchValidator,
    store: IngestStore,
    run_id: str,
    totals: Totals,
    rejected_dir: Path,
) -> None:
    """Validate and load every queued batch in order, acknowledging each when done."""
    for seq, path in queue.pending():
        try:
            frame = pl.read_parquet(path)
        except pl.exceptions.PolarsError as err:
            result = validator.reject_unreadable(seq, f"unreadable batch file: {err}")
        else:
            result = validator.validate(seq, frame)
        if result.rejected:
            rejected_dir.mkdir(parents=True, exist_ok=True)
            kept = rejected_dir / f"batch-{seq:09d}.parquet"
            shutil.copyfile(path, kept)
            result.quarantined = [
                replace(e, detail=f"{e.detail} (batch kept at {kept.name})")
                for e in result.quarantined
            ]
        loaded = store.load_batch(run_id, result.clean, result.quarantined)
        totals.batches += 1
        totals.add(result, loaded)
        queue.ack(path)


def run_ingest(
    frames: Iterable[pl.DataFrame],
    store_path: Path,
    metrics_label: str,
    batch_minutes: int,
    rules: Mapping[str, MetricRule] | None = None,
    queue_dir: Path | None = None,
    validator: BatchValidator | None = None,
) -> RunReport:
    """Validate and load a stream of batches; return the run report."""
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    validator = validator or StreamValidator(rules or load_rules())
    totals = Totals()
    scratch = tempfile.TemporaryDirectory() if queue_dir is None else None
    queue = FileQueue(queue_dir or Path(scratch.name if scratch else ""))
    try:
        with IngestStore(store_path) as store:
            store.start_run(run_id, started, metrics_label, batch_minutes)
            rejected = store_path.with_suffix(".rejected")
            for frame in frames:
                produce([frame], queue)
                consume(queue, validator, store, run_id, totals, rejected)
            consume(queue, validator, store, run_id, totals, rejected)
            tail = validator.finish()
            store.load_batch(run_id, tail.clean, tail.quarantined)
            totals.add(tail, 0)
            report = _report(run_id, started, metrics_label, batch_minutes, totals)
            store.finish_run(report, json.dumps({c.value: n for c, n in totals.counts.items()}))
    finally:
        if scratch:
            scratch.cleanup()
    return report


def _report(
    run_id: str, started: datetime, metrics_label: str, batch_minutes: int, totals: Totals
) -> RunReport:
    return RunReport(
        run_id=run_id,
        status="failed" if totals.counts else "passed",
        metrics_path=metrics_label,
        started_at=started,
        finished_at=datetime.now(UTC),
        batch_minutes=batch_minutes,
        batches=totals.batches,
        rows_read=totals.rows_read,
        rows_clean=totals.rows_clean,
        rows_quarantined=totals.rows_quarantined,
        rows_loaded=totals.rows_loaded,
        rows_already_present=totals.rows_clean - totals.rows_loaded,
        checks=[
            CheckSummary(code=c.value, description=DESCRIPTIONS[c], failed=totals.counts.get(c, 0))
            for c in FaultCode
        ],
        faulty_batches=totals.faulty,
    )
