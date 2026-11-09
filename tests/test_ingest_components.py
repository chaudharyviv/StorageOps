import ast
import re
from dataclasses import replace
from pathlib import Path

import duckdb
import polars as pl
import pytest

import ingest
from contracts import Variant
from ingest import rules as rules_module
from ingest.file_queue import FileQueue
from ingest.pipeline import run_ingest
from ingest.reader import BATCH_SCHEMA, ReplayReader, SourceError
from ingest.report import DESCRIPTIONS, FaultCode
from ingest.rules import RulesError, build_rules, load_rules, parse_catalogue
from ingest.run import main
from ingest.validate import StreamValidator
from simulator.catalogue import emitted_metrics, parse
from simulator.engine import simulate
from simulator.scenario import LabelSpec, ScenarioVariant, find_scenario
from simulator.store import default_labels_path, write_run

PACKAGE = Path(ingest.__file__).parent


def frame(rows: int = 3) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_us": [i * 60_000_000 for i in range(rows)],
            "cluster": ["c"] * rows,
            "object_type": ["qos"] * rows,
            "object_id": ["qos1"] * rows,
            "metric": ["iops"] * rows,
            "value": [float(i) for i in range(rows)],
            "unit": ["iops"] * rows,
        },
        schema=BATCH_SCHEMA,
    )


def short_variant(minutes: int) -> ScenarioVariant:
    base = find_scenario("aggregate_filling").variant(Variant.MILD)
    return replace(base, duration_minutes=minutes, effects=(), labels=(LabelSpec("aggr1", 0),))


# --- rules ----------------------------------------------------------------------------------


def test_rules_cover_exactly_the_metric_catalogue() -> None:
    assert set(load_rules()) == set(emitted_metrics())


def test_ingest_parses_the_catalogue_the_same_way_the_simulator_does() -> None:
    text = rules_module.CATALOGUE_PATH.read_text(encoding="utf-8")
    ours = {m: (u, set(t)) for m, (u, t) in parse_catalogue(text).items()}
    assert ours == parse(text)


def test_rules_units_and_types_come_from_the_catalogue() -> None:
    rules = load_rules()
    assert rules["iops"].object_types == {"qos", "volume"} and rules["iops"].unit == "iops"
    assert rules["space.used_percent"].maximum == 100


CATALOGUE = {"a": ("bytes", frozenset({"volume"}))}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"metrics": {}}, "no rule"),
        ({"metrics": {"a": {"unit": "bytes"}, "b": {"unit": "x"}}}, "not in METRICS.md"),
        ({"metrics": {"a": {"unit": "GB"}}}, "catalogue"),
        ({"metrics": {"a": {"unit": "bytes", "min": 5, "max": 1}}}, "above max"),
        ({"metrics": {"a": {"unit": "bytes", "min": "low"}}}, "number or null"),
        ({"metrics": {"a": {"unit": "bytes", "limit": 1}}}, "expected keys"),
        ({"nope": 1}, "'metrics' mapping"),
    ],
)
def test_bad_rules_are_rejected(raw: object, message: str) -> None:
    with pytest.raises(RulesError, match=message):
        build_rules(CATALOGUE, raw)


# --- reader ---------------------------------------------------------------------------------


def test_reader_refuses_a_labels_file_missing_files_and_empty_files(tmp_path: Path) -> None:
    db = tmp_path / "m.duckdb"
    labels = write_run(db, simulate(short_variant(5), seed=1))
    assert labels == default_labels_path(db)
    with pytest.raises(SourceError, match="only the 'metrics' table"):
        ReplayReader(labels)
    with pytest.raises(SourceError, match="not found"):
        ReplayReader(tmp_path / "nope.duckdb")
    with pytest.raises(SourceError, match="at least 1"):
        ReplayReader(db, batch_minutes=0)
    empty = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(empty))
    con.execute(
        "CREATE TABLE metrics (ts TIMESTAMPTZ, cluster VARCHAR, object_type VARCHAR, "
        "object_id VARCHAR, metric VARCHAR, value DOUBLE, unit VARCHAR)"
    )
    con.close()
    with pytest.raises(SourceError, match="no metrics"):
        ReplayReader(empty)


# --- file queue -----------------------------------------------------------------------------


def test_queue_is_fifo_atomic_and_acknowledged(tmp_path: Path) -> None:
    queue = FileQueue(tmp_path / "q")
    ids = [queue.put(frame(n)) for n in (1, 2, 3)]
    assert ids == [1, 2, 3] and len(queue) == 3
    assert not list((tmp_path / "q").glob("*.tmp"))  # no half-written files left visible
    items = list(queue.pending())
    assert [seq for seq, _ in items] == [1, 2, 3]
    assert [pl.read_parquet(p).height for _, p in items] == [1, 2, 3]
    queue.ack(items[0][1])
    assert [seq for seq, _ in queue.pending()] == [2, 3]


def test_queue_keeps_unacknowledged_batches_and_continues_numbering(tmp_path: Path) -> None:
    first = FileQueue(tmp_path / "q")
    first.put(frame())
    first.put(frame())
    restarted = FileQueue(tmp_path / "q")  # a new process after a crash
    assert [seq for seq, _ in restarted.pending()] == [1, 2]
    assert restarted.put(frame()) == 3


def test_a_redelivered_batch_does_not_duplicate_rows(tmp_path: Path) -> None:
    """At-least-once delivery is safe because loading ignores rows it already has."""
    queue_dir = tmp_path / "q"
    FileQueue(queue_dir).put(frame(5))
    report_a = run_ingest(iter([]), tmp_path / "s.duckdb", "x", 30, queue_dir=queue_dir)
    FileQueue(queue_dir).put(frame(5))  # the same rows again, as if the acknowledgement was lost
    report_b = run_ingest(iter([]), tmp_path / "s.duckdb", "x", 30, queue_dir=queue_dir)
    assert report_a.rows_loaded == 5 and report_b.rows_loaded == 0
    assert report_b.rows_already_present == 5


# --- validator ------------------------------------------------------------------------------


def test_validator_state_carries_across_batches() -> None:
    validator = StreamValidator(load_rules())
    first = validator.validate(1, frame(3))
    again = validator.validate(2, frame(3))  # the same minutes again
    assert first.counts == {} and first.clean.height == 3
    assert again.counts == {FaultCode.DUPLICATE: 1, FaultCode.OUT_OF_ORDER: 2}
    assert again.clean.height == 0


def test_a_row_with_two_faults_is_counted_twice_but_quarantined_once() -> None:
    bad = frame(1).with_columns(pl.lit("GB").alias("unit"), pl.lit(-1.0).alias("value"))
    result = StreamValidator(load_rules()).validate(1, bad)
    assert result.counts == {FaultCode.UNIT_MISMATCH: 1, FaultCode.OUT_OF_RANGE: 1}
    assert result.rows_quarantined == 1 and len(result.quarantined) == 1
    assert result.quarantined[0].reason == FaultCode.UNIT_MISMATCH  # first in precedence
    assert result.quarantined[0].reasons == (FaultCode.UNIT_MISMATCH, FaultCode.OUT_OF_RANGE)


def test_every_fault_code_has_a_description() -> None:
    assert set(DESCRIPTIONS) == set(FaultCode)


# --- labels are never touched ---------------------------------------------------------------


def test_ingest_source_has_no_route_to_the_labels_or_the_simulator() -> None:
    forbidden = (
        "scenario_labels",
        "read_labels",
        "LABELS_TABLE",
        ".labels.duckdb",
        "ScenarioLabel",
    )
    for source in PACKAGE.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{source.name} mentions {token}"
        for node in ast.walk(ast.parse(text)):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            if isinstance(node, ast.Import):
                modules += [a.name for a in node.names]
            for module in modules:
                assert not module.startswith("simulator"), f"{source.name} imports {module}"
                assert not re.match(r"(eval|models|features)(\.|$)", module), module


def test_ingest_opens_only_the_metrics_and_store_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "m.duckdb"
    labels = write_run(db, simulate(short_variant(10), seed=1))
    assert labels.exists()

    opened: list[str] = []
    real_connect = duckdb.connect

    def spy(database: str, *args: object, **kwargs: object) -> object:
        opened.append(Path(database).name)
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(duckdb, "connect", spy)
    assert main(["--metrics", str(db), "--store", str(tmp_path / "s.duckdb")]) == 0
    assert set(opened) == {"m.duckdb", "s.duckdb"}
    assert labels.name not in opened
