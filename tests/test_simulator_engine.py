from dataclasses import replace
from pathlib import Path

import duckdb
import numpy as np
import pytest

from contracts import MetricRecord, ObjectType, ScenarioType, Variant
from simulator.engine import iter_metric_records, simulate
from simulator.run import main
from simulator.scenario import ScenarioVariant, find_scenario, parse_scenario
from simulator.store import (
    LABELS_TABLE,
    METRICS_TABLE,
    default_labels_path,
    metric_row_count,
    read_labels,
    write_run,
)

# A tiny scenario defined only here: no shipped file is involved. It borrows an existing
# scenario type for its label, and adds a trident_backend object the healthy cluster lacks.
TINY_YAML = """
scenario: snapmirror_lag_creep
description: tiny test scenario
duration_minutes: 240
extra_objects:
  - object_type: trident_backend
    object_id: backend1
    metrics:
      - {name: pvc.count, unit: count, base: 100}
effects:
  - target: {object_type: snapmirror, object_id: sm2, metric: lag_seconds}
    pattern: step
    start_minute: 60
    magnitude: 1000
  - target: {object_type: trident_backend, object_id: backend1, metric: pvc.count}
    pattern: linear_ramp
    start_minute: 120
    magnitude: $burst
labels:
  - {object_id: sm2, start_minute: 60, end_minute: 200}
variants:
  mild: {split: train, params: {burst: 10}}
  severe: {split: validation, params: {burst: 100}}
"""


def tiny(variant: Variant = Variant.SEVERE) -> ScenarioVariant:
    import yaml

    return parse_scenario(yaml.safe_load(TINY_YAML)).variant(variant)


def dump(path: Path) -> list[tuple[object, ...]]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return con.execute(
            f"SELECT CAST(ts AS VARCHAR), cluster, object_type, object_id, metric, value, unit "
            f"FROM {METRICS_TABLE} ORDER BY ALL"
        ).fetchall()
    finally:
        con.close()


def test_engine_runs_a_scenario_defined_only_in_the_test() -> None:
    result = simulate(tiny(), seed=1)
    healthy = simulate(replace(tiny(), effects=()), seed=1)
    lag = (ObjectType.SNAPMIRROR, "sm2", "lag_seconds")
    # Noise streams are keyed by identity, so the only difference is the injected step.
    diff = result.series[lag] - healthy.series[lag]
    assert np.allclose(diff[:60], 0) and np.allclose(diff[60:], 1000)
    pvc = result.series[(ObjectType.TRIDENT_BACKEND, "backend1", "pvc.count")]
    assert pvc[119] == 100 and pvc[-1] == pytest.approx(200)
    assert result.units[(ObjectType.TRIDENT_BACKEND, "backend1", "pvc.count")] == "count"


def test_label_window_comes_from_the_scenario() -> None:
    (label,) = simulate(tiny(), seed=4).labels
    assert label.scenario == ScenarioType.SNAPMIRROR_LAG_CREEP
    assert label.variant == Variant.SEVERE
    assert label.object_id == "sm2"
    assert (label.end - label.start).total_seconds() == 140 * 60
    assert label.label_id == "snapmirror_lag_creep-severe-s4-sm2"


def test_same_seed_gives_identical_output_and_other_seeds_differ() -> None:
    first, again, other = (simulate(tiny(), seed=s) for s in (3, 3, 4))
    assert first.series.keys() == again.series.keys()
    assert all(np.array_equal(first.series[k], again.series[k]) for k in first.series)
    assert any(not np.array_equal(first.series[k], other.series[k]) for k in first.series)


def test_same_seed_gives_identical_database_contents(tmp_path: Path) -> None:
    a, b, c = (tmp_path / f"{n}.duckdb" for n in "abc")
    labels = [write_run(p, simulate(tiny(), seed=s)) for p, s in ((a, 3), (b, 3), (c, 4))]
    assert dump(a) == dump(b)
    assert dump(a) != dump(c)
    assert read_labels(labels[0]) == read_labels(labels[1])


def test_healthy_series_do_not_depend_on_the_scenario() -> None:
    plain = simulate(replace(tiny(), effects=(), extra_objects=()), seed=9)
    with_extra = simulate(replace(tiny(), effects=()), seed=9)
    key = (ObjectType.VOLUME, "vol01", "iops")
    assert np.array_equal(plain.series[key], with_extra.series[key])


def test_cluster_shape_and_one_record_per_object_metric_minute() -> None:
    result = simulate(replace(tiny(), extra_objects=(), effects=()), seed=1)
    ids = {t: {k[1] for k in result.series if k[0] == t} for t in ObjectType}
    assert [len(ids[t]) for t in (ObjectType.AGGREGATE, ObjectType.VOLUME)] == [2, 10]
    assert [len(ids[t]) for t in (ObjectType.SNAPMIRROR, ObjectType.QOS)] == [2, 2]
    assert not ids[ObjectType.TRIDENT_BACKEND]

    records = list(iter_metric_records(result))  # validates every row as a MetricRecord
    assert len(records) == result.minutes * len(result.series)
    seen = {(r.ts, r.object_type, r.object_id, r.metric) for r in records}
    assert len(seen) == len(records)
    assert {r.ts for r in records[: len(result.series)]} == {result.start}


def tables_of(path: Path) -> set[str]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    finally:
        con.close()


def test_metrics_file_contains_no_label_data(tmp_path: Path) -> None:
    db = tmp_path / "run.duckdb"
    result = simulate(tiny(), seed=2)
    labels_db = write_run(db, result)

    # Labels land in their own file, next to the metrics by default.
    assert labels_db == default_labels_path(db) == tmp_path / "run.labels.duckdb"
    assert tables_of(db) == {METRICS_TABLE}
    assert tables_of(labels_db) == {LABELS_TABLE}
    assert read_labels(labels_db) == result.labels

    # The metrics file has exactly the MetricRecord columns and no scenario or variant names.
    assert "scenario" not in MetricRecord.model_fields and "label" not in MetricRecord.model_fields
    con = duckdb.connect(str(db), read_only=True)
    try:
        columns = [
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns ORDER BY ordinal_position"
            ).fetchall()
        ]
        assert columns == list(MetricRecord.model_fields)
        names = {s.value for s in ScenarioType} | {v.value for v in Variant}
        for column in ("cluster", "object_type", "object_id", "metric", "unit"):
            values = {
                r[0]
                for r in con.execute(f"SELECT DISTINCT {column} FROM {METRICS_TABLE}").fetchall()
            }
            assert not values & names
    finally:
        con.close()

    # Nothing label-shaped anywhere in the raw file either (table name, ids, scenario names).
    raw = db.read_bytes()
    forbidden = [LABELS_TABLE, *(lab.label_id for lab in result.labels), result.labels[0].scenario]
    assert not any(str(token).encode() in raw for token in forbidden)


def test_labels_must_not_share_the_metrics_file(tmp_path: Path) -> None:
    db = tmp_path / "run.duckdb"
    with pytest.raises(ValueError, match="different file"):
        write_run(db, simulate(tiny(), seed=1), labels_path=db)


def test_cli_writes_two_files_and_guards_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenarios = tmp_path / "scn"
    scenarios.mkdir()
    (scenarios / "snapmirror_lag_creep.yaml").write_text(TINY_YAML, encoding="utf-8")
    db = tmp_path / "out" / "run.duckdb"
    labels = tmp_path / "out" / "run.labels.duckdb"
    args = [
        "--scenario", "snapmirror_lag_creep", "--variant", "severe", "--seed", "3",
        "--out", str(db), "--scenarios-dir", str(scenarios),
    ]  # fmt: skip

    assert main(args) == 0
    assert metric_row_count(db) == 240 * len(simulate(tiny(), seed=3).series)
    assert len(read_labels(labels)) == 1
    assert "split=validation" in capsys.readouterr().out

    assert main(args) == 2
    assert "already exists" in capsys.readouterr().err
    labels.unlink()  # one file left behind still blocks a rerun, and nothing is half-written
    before = db.read_bytes()
    assert main(args) == 2
    assert db.read_bytes() == before and not labels.exists()
    assert main([*args, "--overwrite"]) == 0 and labels.exists()

    custom = tmp_path / "elsewhere" / "truth.duckdb"
    assert main([*args, "--overwrite", "--labels-out", str(custom)]) == 0
    assert tables_of(custom) == {LABELS_TABLE}
    assert main([*args, "--overwrite", "--labels-out", str(db)]) == 2

    bad = [a if a != "snapmirror_lag_creep" else "no_such_scenario" for a in args]
    assert main(bad) == 2


def test_cli_runs_a_shipped_scenario(tmp_path: Path) -> None:
    db = tmp_path / "shipped.duckdb"
    args = ["--scenario", "aggregate_filling", "--variant", "moderate", "--seed", "1"]
    assert main([*args, "--out", str(db)]) == 0
    (label,) = read_labels(default_labels_path(db))
    assert label.scenario == ScenarioType.AGGREGATE_FILLING and label.object_id == "aggr1"
    variant = find_scenario("aggregate_filling").variant(Variant.MODERATE)
    assert metric_row_count(db) == variant.duration_minutes * len(simulate(variant, 1).series)
