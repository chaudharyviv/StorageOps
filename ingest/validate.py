"""Custom batch validation: schema, rules, duplicates, ordering and gaps.

`BatchValidator` is the seam for a Great Expectations adapter later: anything that turns a
batch into a `BatchResult` (see report.py) can replace `StreamValidator`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import polars as pl
from pydantic import TypeAdapter, ValidationError

from contracts import MetricRecord
from ingest.reader import BATCH_SCHEMA, MINUTE_US
from ingest.report import BatchResult, FaultCode, Quarantined
from ingest.rules import MetricRule

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ADAPTER = TypeAdapter(list[MetricRecord])
Series = tuple[str, str, str]


class BatchValidator(Protocol):
    """What the pipeline needs from a validator."""

    def validate(self, batch_id: int, frame: pl.DataFrame) -> BatchResult:
        """Check one batch; batches arrive in queue order."""
        ...

    def reject_unreadable(self, batch_id: int, why: str) -> BatchResult:
        """Quarantine a batch that could not be read at all."""
        ...

    def finish(self) -> BatchResult:
        """Run end-of-stream checks, such as series that start late or stop early."""
        ...


def schema_problems(frame: pl.DataFrame) -> list[str]:
    """Differences between a batch and the wire layout, empty when it matches."""
    problems = []
    if missing := [c for c in BATCH_SCHEMA if c not in frame.columns]:
        problems.append(f"missing columns {missing}")
    if extra := [c for c in frame.columns if c not in BATCH_SCHEMA]:
        problems.append(f"unexpected columns {extra}")
    for name, expected in BATCH_SCHEMA.items():
        if name in frame.columns and frame.schema[name] != expected:
            problems.append(f"column {name} is {frame.schema[name]}, expected {expected}")
    return problems


def _record(
    ts_us: Any, cluster: Any, otype: Any, oid: Any, metric: Any, value: Any, unit: Any
) -> dict[str, Any]:
    try:
        ts = None if ts_us is None else EPOCH + timedelta(microseconds=ts_us)
    except OverflowError:
        ts = None
    return {
        "ts": ts, "cluster": cluster, "object_type": otype, "object_id": oid,
        "metric": metric, "value": value, "unit": unit,
    }  # fmt: skip


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    return f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"


def _bad_rows(records: list[dict[str, Any]]) -> dict[int, str]:
    """Rows that fail the MetricRecord contract. Fast path first, per row only on failure."""
    try:
        _ADAPTER.validate_python(records)
        return {}
    except ValidationError:
        pass
    bad = {}
    for index, record in enumerate(records):
        try:
            MetricRecord.model_validate(record)
        except ValidationError as err:
            bad[index] = _first_error(err)
    return bad


def _payload(values: Mapping[str, Any]) -> str:
    clean = {
        k: (str(v) if isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in values.items()
    }
    return json.dumps(clean, sort_keys=True, default=str)


class StreamValidator:
    """Stateful checks over a stream of batches: state carries across batches."""

    def __init__(self, rules: Mapping[str, MetricRule]) -> None:
        self._rules = rules
        # series -> [first timestamp, latest timestamp] (microseconds)
        self._series: dict[Series, list[int]] = {}
        self._key_counts: dict[str, int] = {}

    def _key(self, *parts: object) -> str:
        base = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]
        n = self._key_counts.get(base, 0)
        self._key_counts[base] = n + 1
        return base if n == 0 else hashlib.sha256(f"{base}|{n}".encode()).hexdigest()[:32]

    def _gap(
        self, batch_id: int | None, series: Series, after_us: int, missing: int, why: str
    ) -> Quarantined:
        start = after_us + MINUTE_US
        stamp = (EPOCH + timedelta(microseconds=start)).isoformat()
        return Quarantined(
            key=self._key("gap", *series, start),
            batch_id=batch_id, reason=FaultCode.GAP, reasons=(FaultCode.GAP,),
            detail=f"{missing} missing minute(s) from {stamp}: {why}",
            object_type=series[0], object_id=series[1], metric=series[2], ts_us=start,
        )  # fmt: skip

    def _reject(self, batch_id: int, rows: int, why: str) -> BatchResult:
        entry = Quarantined(
            key=self._key("schema_batch", batch_id, why),
            batch_id=batch_id, reason=FaultCode.SCHEMA_BATCH, reasons=(FaultCode.SCHEMA_BATCH,),
            detail=why,
        )  # fmt: skip
        return BatchResult(
            batch_id, rows, pl.DataFrame(schema=BATCH_SCHEMA), [entry],
            {FaultCode.SCHEMA_BATCH: max(rows, 1)}, rejected=True, rows_quarantined=rows,
        )  # fmt: skip

    def reject_unreadable(self, batch_id: int, why: str) -> BatchResult:
        """Quarantine a batch file that could not be read at all."""
        return self._reject(batch_id, 0, why)

    def validate(self, batch_id: int, frame: pl.DataFrame) -> BatchResult:
        """Check one batch; return its clean rows and what was quarantined."""
        if problems := schema_problems(frame):
            return self._reject(batch_id, frame.height, "; ".join(problems))
        ts_l, cl_l, ot_l, oid_l, m_l, v_l, u_l = (frame[name].to_list() for name in BATCH_SCHEMA)
        n = frame.height
        schema_bad = _bad_rows(
            [_record(*row) for row in zip(ts_l, cl_l, ot_l, oid_l, m_l, v_l, u_l, strict=True)]
        )
        codes: list[list[FaultCode]] = [[] for _ in range(n)]
        notes: list[list[str]] = [[] for _ in range(n)]
        gaps: list[Quarantined] = []
        gap_minutes = 0
        for i in range(n):
            bad = i in schema_bad
            if bad:
                codes[i].append(FaultCode.SCHEMA_ROW)
                notes[i].append(schema_bad[i])
            rule = self._rules.get(m_l[i])
            if rule is None or ot_l[i] not in rule.object_types:
                if not bad:
                    codes[i].append(FaultCode.UNKNOWN_METRIC)
                    notes[i].append(f"{ot_l[i]}/{m_l[i]} is not in the metric catalogue")
                continue
            if not bad:
                if u_l[i] != rule.unit:
                    codes[i].append(FaultCode.UNIT_MISMATCH)
                    notes[i].append(f"unit {u_l[i]!r}, expected {rule.unit!r}")
                value = v_l[i]
                if (rule.minimum is not None and value < rule.minimum) or (
                    rule.maximum is not None and value > rule.maximum
                ):
                    codes[i].append(FaultCode.OUT_OF_RANGE)
                    notes[i].append(f"value {value} outside [{rule.minimum}, {rule.maximum}]")
            ts = ts_l[i]
            if ts is None:
                continue
            # A row with a bad value or unit still holds its minute in the series, so a bad
            # sample is never also reported as a gap.
            series = (ot_l[i], oid_l[i], m_l[i])
            state = self._series.get(series)
            if state is None:
                self._series[series] = [ts, ts]
            elif ts == state[1]:
                codes[i].append(FaultCode.DUPLICATE)
                notes[i].append("same timestamp as the latest sample of the series")
            elif ts < state[1]:
                codes[i].append(FaultCode.OUT_OF_ORDER)
                late = (state[1] - ts) // MINUTE_US
                notes[i].append(f"older than the latest sample (by {late} min)")
            else:
                missing = (ts - state[1]) // MINUTE_US - 1
                if missing > 0:
                    gaps.append(self._gap(batch_id, series, state[1], missing, "between samples"))
                    gap_minutes += missing
                state[1] = ts

        keep = [not c for c in codes]
        quarantined = list(gaps)
        counts: dict[FaultCode, int] = {FaultCode.GAP: gap_minutes} if gap_minutes else {}
        for i in range(n):
            if not codes[i]:
                continue
            for code in codes[i]:
                counts[code] = counts.get(code, 0) + 1
            row = {
                name: col[i]
                for name, col in zip(
                    BATCH_SCHEMA, (ts_l, cl_l, ot_l, oid_l, m_l, v_l, u_l), strict=True
                )
            }
            payload = _payload(row)
            quarantined.append(
                Quarantined(
                    key=self._key(codes[i][0], ot_l[i], oid_l[i], m_l[i], ts_l[i], payload),
                    batch_id=batch_id,
                    reason=codes[i][0],
                    reasons=tuple(codes[i]),
                    detail="; ".join(notes[i]),
                    object_type=ot_l[i],
                    object_id=oid_l[i],
                    metric=m_l[i],
                    ts_us=ts_l[i],
                    payload=payload,
                )  # fmt: skip
            )
        clean = frame.filter(pl.Series(keep, dtype=pl.Boolean)) if n else frame
        return BatchResult(
            batch_id, n, clean, quarantined, counts, rows_quarantined=n - clean.height
        )

    def finish(self) -> BatchResult:
        """Flag series that start after, or stop before, the rest of the stream."""
        empty = pl.DataFrame(schema=BATCH_SCHEMA)
        if not self._series:
            return BatchResult(None, 0, empty)
        earliest = min(s[0] for s in self._series.values())
        latest = max(s[1] for s in self._series.values())
        entries: list[Quarantined] = []
        minutes = 0
        for series in sorted(self._series):
            first, last = self._series[series]
            if first > earliest:
                missing = (first - earliest) // MINUTE_US
                entries.append(
                    self._gap(None, series, earliest - MINUTE_US, missing, "series starts late")
                )
                minutes += missing
            if last < latest:
                missing = (latest - last) // MINUTE_US
                entries.append(self._gap(None, series, last, missing, "series stops early"))
                minutes += missing
        return BatchResult(None, 0, empty, entries, {FaultCode.GAP: minutes} if minutes else {})
