import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import polars as pl
import pytest

from contracts import Variant
from ingest.pipeline import run_ingest
from ingest.reader import MINUTE_US, ReplayReader
from ingest.report import FaultCode, RunReport
from ingest.rules import load_rules
from ingest.run import main
from simulator.engine import simulate
from simulator.scenario import LabelSpec, find_scenario, scenario_files
from simulator.store import write_run

MINUTES = 120
BATCH = 30  # minutes per batch: four batches
SERIES = 138  # series in the healthy cluster
TOTAL = MINUTES * SERIES
T0 = "TIMESTAMPTZ '2026-01-01 00:00:00+00'"


# --- helpers that build the SQL for each injected fault ---------------------------------------


def at(minute: int) -> str:
    return f"{T0} + INTERVAL {minute} MINUTE"


def series(obj: str, metric: str) -> str:
    return f"object_id = '{obj}' AND metric = '{metric}'"


def set_column(obj: str, metric: str, minute: int, column: str, value: str) -> str:
    """UPDATE one existing sample."""
    return (
        f"UPDATE metrics SET {column} = {value} WHERE {series(obj, metric)} AND ts = {at(minute)}"
    )


def drop_samples(obj: str, metric: str, start: int, end: int) -> str:
    """DELETE samples from minute `start` up to but not including `end`."""
    return (
        f"DELETE FROM metrics WHERE {series(obj, metric)} AND ts >= {at(start)} AND ts < {at(end)}"
    )


def copy_sample(obj: str, metric: str, minute: int) -> str:
    """INSERT a second copy of one existing sample."""
    return (
        "INSERT INTO metrics SELECT * FROM metrics "
        f"WHERE {series(obj, metric)} AND ts = {at(minute)}"
    )


def add_row(otype: str, obj: str, metric: str, unit: str = "count", cluster: str = "c1") -> str:
    """INSERT a brand-new row at minute 0."""
    values = f"{T0}, '{cluster}', '{otype}', '{obj}', '{metric}', 1.0, '{unit}'"
    return f"INSERT INTO metrics VALUES ({values})"


@pytest.fixture(scope="session")
def clean_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ingest") / "clean.duckdb"
    base = find_scenario("aggregate_filling").variant(Variant.MILD)
    variant = replace(base, duration_minutes=MINUTES, effects=(), labels=(LabelSpec("aggr1", 0),))
    write_run(path, simulate(variant, seed=3))
    return path


def faulty_copy(clean_db: Path, tmp_path: Path, *statements: str) -> Path:
    """Copy the clean metrics file and apply SQL faults to the copy."""
    path = tmp_path / "metrics.duckdb"
    shutil.copy(clean_db, path)
    con = duckdb.connect(str(path))
    for statement in statements:
        con.execute(statement)
    con.close()
    return path


def ingest(metrics: Path, workdir: Path, batch_minutes: int = BATCH) -> tuple[RunReport, Path]:
    store = workdir / "store.duckdb"
    with ReplayReader(metrics, batch_minutes) as reader:
        report = run_ingest(reader.batches(), store, str(metrics), batch_minutes)
    return report, store


def counts(report: RunReport) -> dict[str, int]:
    return {c.code: c.failed for c in report.checks if c.failed}


def query(store: Path, sql: str) -> list[tuple[Any, ...]]:
    con = duckdb.connect(str(store), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def table_count(store: Path, table: str) -> int:
    return int(query(store, f"SELECT count(*) FROM {table}")[0][0])


COLUMNS = "epoch_us(ts), object_type, object_id, metric, value, unit"


# --- a clean run ----------------------------------------------------------------------------


def test_clean_run_passes_with_nothing_quarantined(clean_db: Path, tmp_path: Path) -> None:
    report, store = ingest(clean_db, tmp_path)
    assert report.status == "passed" and counts(report) == {}
    assert report.batches == MINUTES // BATCH
    assert report.rows_read == report.rows_clean == report.rows_loaded == TOTAL
    assert report.rows_quarantined == 0 and report.rows_already_present == 0
    assert table_count(store, "clean_metrics") == TOTAL
    assert table_count(store, "quarantine") == 0
    ((status, run_rows),) = query(store, "SELECT status, rows_clean FROM validation_runs")
    assert status == "passed" and run_rows == TOTAL
    assert list(tmp_path.glob("*.rejected")) == []


def test_clean_rows_round_trip_the_source(clean_db: Path, tmp_path: Path) -> None:
    _, store = ingest(clean_db, tmp_path)
    source = query(clean_db, f"SELECT {COLUMNS} FROM metrics ORDER BY ALL")
    loaded = query(store, f"SELECT {COLUMNS} FROM clean_metrics ORDER BY ALL")
    assert source == loaded


def test_batches_are_whole_minutes_in_time_order(clean_db: Path) -> None:
    with ReplayReader(clean_db, BATCH) as reader:
        frames = list(reader.batches())
    assert len(frames) == 4
    last_max = -1
    for frame in frames:
        low, high = frame["ts_us"].min(), frame["ts_us"].max()
        assert isinstance(low, int) and isinstance(high, int)
        assert low > last_max and (high - low) < BATCH * MINUTE_US
        assert frame["ts_us"].is_sorted()
        last_max = high
    assert sum(f.height for f in frames) == TOTAL


# --- injected faults: each is caught and counted ------------------------------------------------


def test_a_gap_is_caught_and_counted_in_missing_minutes(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(clean_db, tmp_path, drop_samples("vol01", "iops", 10, 15))
    report, store = ingest(metrics, tmp_path)
    assert report.status == "failed" and counts(report) == {"gap": 5}
    assert report.rows_read == TOTAL - 5 and report.rows_quarantined == 0  # no row to quarantine
    ((reason, who, detail),) = query(
        store, "SELECT reason, object_id || '/' || metric, detail FROM quarantine"
    )
    assert (reason, who) == ("gap", "vol01/iops") and "5 missing minute(s)" in detail
    assert table_count(store, "clean_metrics") == TOTAL - 5


def test_a_series_that_stops_early_or_starts_late_is_a_gap(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        drop_samples("qos1", "iops", 100, 1000),  # stops 20 minutes early
        drop_samples("qos2", "iops", 0, 3),  # starts 3 minutes late
    )
    report, store = ingest(metrics, tmp_path)
    assert counts(report) == {"gap": 20 + 3}
    details = [r[0] for r in query(store, "SELECT detail FROM quarantine")]
    assert any("stops early" in d for d in details) and any("starts late" in d for d in details)


def test_a_duplicate_is_caught_and_the_first_copy_is_kept(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(clean_db, tmp_path, copy_sample("vol01", "iops", 20))
    report, store = ingest(metrics, tmp_path)
    assert counts(report) == {"duplicate": 1}
    assert report.rows_read == TOTAL + 1 and report.rows_quarantined == 1
    assert table_count(store, "clean_metrics") == TOTAL
    assert query(store, "SELECT reason FROM quarantine") == [("duplicate",)]


def test_bad_values_are_caught_by_the_range_rules(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        set_column("aggr1", "space.used_percent", 30, "value", "250"),
        set_column("vol02", "space.used_bytes", 31, "value", "-5"),
    )
    report, store = ingest(metrics, tmp_path)
    assert counts(report) == {"out_of_range": 2}  # and no gap: a bad sample holds its minute
    assert report.rows_quarantined == 2 and table_count(store, "clean_metrics") == TOTAL - 2
    details = query(store, "SELECT detail FROM quarantine")
    assert all("outside" in d[0] for d in details)


def test_a_wrong_unit_is_caught(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db, tmp_path, set_column("aggr2", "space.size_bytes", 5, "unit", "'GB'")
    )
    report, _ = ingest(metrics, tmp_path)
    assert counts(report) == {"unit_mismatch": 1}


def test_unknown_metrics_and_object_types_are_caught(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        add_row("volume", "vol01", "made.up"),  # metric not in the catalogue
        add_row("qos", "qos1", "space.size_bytes", "bytes"),  # metric not valid for qos
    )
    report, _ = ingest(metrics, tmp_path)
    assert counts(report) == {"unknown_metric": 2}


def test_wrong_schema_rows_are_caught_by_the_contract(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        add_row("bogus", "x1", "iops", "iops"),  # object type is not a real one
        add_row("volume", "vol01", "", "iops"),  # empty metric name
        set_column("vol03", "iops", 7, "cluster", "''"),  # empty cluster
        set_column("vol04", "iops", 8, "value", "'NaN'::DOUBLE"),  # NaN is not allowed
    )
    report, store = ingest(metrics, tmp_path)
    assert counts(report) == {"schema_row": 4}  # the two updated rows keep their minute: no gap
    assert report.rows_quarantined == 4 and table_count(store, "clean_metrics") == TOTAL - 2


def run_with_frames(frames: list[pl.DataFrame], workdir: Path) -> tuple[RunReport, Path]:
    store = workdir / "store.duckdb"
    return run_ingest(iter(frames), store, "in-memory", BATCH), store


def batches_of(clean_db: Path) -> list[pl.DataFrame]:
    with ReplayReader(clean_db, BATCH) as reader:
        return list(reader.batches())


def test_out_of_order_batches_are_caught(clean_db: Path, tmp_path: Path) -> None:
    b0, b1, b2, b3 = batches_of(clean_db)
    report, store = run_with_frames([b0, b2, b1, b3], tmp_path)
    # b2 jumps 30 minutes ahead of b0 (a gap), then b1 arrives behind it (out of order).
    assert counts(report) == {"out_of_order": b1.height, "gap": BATCH * SERIES}
    assert report.rows_quarantined == b1.height
    assert table_count(store, "clean_metrics") == TOTAL - b1.height


def test_out_of_order_rows_inside_a_batch_are_caught(clean_db: Path, tmp_path: Path) -> None:
    b0, b1, b2, b3 = batches_of(clean_db)
    rows = b1.with_row_index("i")
    mine = (pl.col("object_id") == "vol01") & (pl.col("metric") == "iops")
    idx = rows.filter(mine)["i"].to_list()
    order = list(range(b1.height))
    order[idx[3]], order[idx[4]] = order[idx[4]], order[idx[3]]  # minute 4 now arrives before 3
    report, _ = run_with_frames([b0, b1[order], b2, b3], tmp_path)
    assert counts(report) == {"out_of_order": 1, "gap": 1}  # the late row, and the hole it left


def test_a_batch_with_the_wrong_columns_is_rejected_whole(clean_db: Path, tmp_path: Path) -> None:
    b0, b1, b2, b3 = batches_of(clean_db)
    report, store = run_with_frames([b0, b1.drop("unit"), b2, b3], tmp_path)
    assert counts(report)["schema_batch"] == b1.height
    assert report.rows_quarantined == b1.height
    ((detail,),) = query(store, "SELECT detail FROM quarantine WHERE reason = 'schema_batch'")
    assert "missing columns ['unit']" in detail and "batch kept at" in detail
    assert len(list((tmp_path / "store.rejected").glob("*.parquet"))) == 1


def test_a_batch_with_the_wrong_types_is_rejected_whole(clean_db: Path, tmp_path: Path) -> None:
    b0, b1, b2, b3 = batches_of(clean_db)
    wrong = b1.with_columns(pl.col("value").cast(pl.String))
    report, _ = run_with_frames([b0, wrong, b2, b3], tmp_path)
    assert counts(report)["schema_batch"] == b1.height


def test_an_unreadable_batch_file_is_quarantined(clean_db: Path, tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "000000001.parquet").write_bytes(b"not parquet")
    store = tmp_path / "store.duckdb"
    report = run_ingest(iter(batches_of(clean_db)), store, "x", BATCH, queue_dir=queue_dir)
    assert report.checks[0].code == "schema_batch" and report.checks[0].failed == 1
    assert report.rows_clean == TOTAL  # the good batches still load
    assert list(queue_dir.glob("*.parquet")) == []  # everything acknowledged


def test_every_fault_code_is_always_listed_in_the_report(clean_db: Path, tmp_path: Path) -> None:
    report, _ = ingest(clean_db, tmp_path)
    assert [c.code for c in report.checks] == [c.value for c in FaultCode]


# --- idempotency ------------------------------------------------------------------------------


def snapshot_tables(store: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    clean = query(store, f"SELECT {COLUMNS}, cluster FROM clean_metrics ORDER BY ALL")
    quarantine = query(
        store,
        "SELECT quarantine_key, run_id, batch_id, reason, reasons, detail, object_type, "
        "object_id, metric, epoch_us(ts), payload FROM quarantine ORDER BY ALL",
    )
    return clean, quarantine


def test_rerunning_a_clean_ingest_changes_nothing(clean_db: Path, tmp_path: Path) -> None:
    first, store = ingest(clean_db, tmp_path)
    before = snapshot_tables(store)
    second, _ = ingest(clean_db, tmp_path)
    assert snapshot_tables(store) == before
    assert second.rows_loaded == 0 and second.rows_already_present == TOTAL
    runs = query(store, "SELECT run_id, rows_loaded FROM validation_runs ORDER BY started_at")
    assert [r[0] for r in runs] == [first.run_id, second.run_id]
    assert [r[1] for r in runs] == [TOTAL, 0]


def test_rerunning_a_faulty_ingest_changes_nothing(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        drop_samples("vol01", "iops", 10, 11),
        copy_sample("vol02", "iops", 20),
        copy_sample("vol02", "iops", 20),  # copies both rows: four in all, three duplicates
        set_column("aggr1", "space.used_percent", 30, "value", "250"),
    )
    first, store = ingest(metrics, tmp_path)
    assert counts(first) == {"gap": 1, "duplicate": 3, "out_of_range": 1}
    before = snapshot_tables(store)
    assert len(before[1]) == 5  # all three identical duplicates keep their own entry
    second, _ = ingest(metrics, tmp_path)
    assert counts(second) == counts(first)
    assert snapshot_tables(store) == before
    assert table_count(store, "validation_runs") == 2


def test_the_result_does_not_depend_on_the_batch_size(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db,
        tmp_path,
        drop_samples("vol01", "iops", 28, 33),  # a gap that straddles a batch boundary
        set_column("aggr1", "space.used_percent", 59, "value", "250"),
    )
    results = {}
    for minutes in (7, 30, 120):
        workdir = tmp_path / f"batch{minutes}"
        workdir.mkdir()
        report, store = ingest(metrics, workdir, minutes)
        keys = query(store, "SELECT quarantine_key FROM quarantine ORDER BY 1")
        results[minutes] = (counts(report), keys, table_count(store, "clean_metrics"))
    assert results[7] == results[30] == results[120]
    assert results[7][0] == {"gap": 5, "out_of_range": 1}


# --- the report format ----------------------------------------------------------------------


def test_the_report_format_is_pinned(clean_db: Path, tmp_path: Path) -> None:
    report, store = ingest(clean_db, tmp_path)
    assert report.schema_version == 1
    assert list(RunReport.model_fields) == [
        "schema_version", "run_id", "status", "metrics_path", "started_at", "finished_at",
        "batch_minutes", "batches", "rows_read", "rows_clean", "rows_quarantined",
        "rows_loaded", "rows_already_present", "checks", "faulty_batches",
    ]  # fmt: skip
    stored = json.loads(str(query(store, "SELECT report FROM validation_runs")[0][0]))
    assert stored["schema_version"] == 1 and stored["status"] == "passed"
    assert RunReport.model_validate(stored) == report


def test_faulty_batches_are_listed_with_their_fault_counts(clean_db: Path, tmp_path: Path) -> None:
    metrics = faulty_copy(
        clean_db, tmp_path, set_column("aggr1", "space.used_percent", 59, "value", "250")
    )
    report, _ = ingest(metrics, tmp_path)
    (batch,) = report.faulty_batches
    assert batch.batch_id == 2 and batch.faults == {"out_of_range": 1} and batch.quarantined == 1


# --- the command line -------------------------------------------------------------------------


def test_cli_exit_codes_and_report_file(
    clean_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, report_path = tmp_path / "s.duckdb", tmp_path / "report.json"
    args = ["--metrics", str(clean_db), "--store", str(store), "--batch-minutes", "30"]
    assert main([*args, "--report", str(report_path)]) == 0
    assert "PASSED" in capsys.readouterr().out
    assert json.loads(report_path.read_text())["status"] == "passed"

    bad = faulty_copy(clean_db, tmp_path, set_column("qos1", "iops", 0, "value", "-1"))
    assert main(["--metrics", str(bad), "--store", str(tmp_path / "s2.duckdb")]) == 1
    assert "out_of_range" in capsys.readouterr().out


@pytest.mark.parametrize(
    "build",
    [
        lambda m, t: ["--metrics", str(t / "missing.duckdb"), "--store", str(t / "s.duckdb")],
        lambda m, t: ["--metrics", str(m), "--store", str(m)],
        lambda m, t: ["--metrics", str(m), "--store", str(t / "s.duckdb"), "--batch-minutes", "0"],
    ],
    ids=["missing-file", "store-is-metrics", "zero-batch"],
)
def test_cli_usage_errors_exit_2(
    clean_db: Path, tmp_path: Path, build: Callable[[Path, Path], list[str]]
) -> None:
    assert main(build(clean_db, tmp_path)) == 2


SEVERE_SCENARIOS = sorted(p.stem for p in scenario_files() if p.parent.name == "scenarios")


@pytest.mark.parametrize("name", SEVERE_SCENARIOS)
def test_rules_accept_the_extremes_of_every_severe_scenario(name: str) -> None:
    """The bounds reject impossible values, not real behaviour at its most extreme."""
    rules = load_rules()
    result = simulate(find_scenario(name).variant(Variant.SEVERE), seed=2)
    for key, values in result.series.items():
        _, object_id, metric = key
        rule = rules[metric]
        low, high = float(np.min(values)), float(np.max(values))
        assert result.units[key] == rule.unit, (object_id, metric)
        assert rule.minimum is None or low >= rule.minimum, (object_id, metric, low)
        assert rule.maximum is None or high <= rule.maximum, (object_id, metric, high)
