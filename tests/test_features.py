import ast
import math
import re
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

import features
from contracts import Variant
from features import catalogue as catalogue_module
from features.compute import (
    CLEAN_TABLE,
    FeaturesError,
    compute_features_at,
    compute_features_range,
    minute_of,
)
from features.functions import FUNCTIONS
from features.run import main
from features.spec import SpecError, get_features, load_features, matching, parse_features
from features.table import DDL, FEATURES_TABLE, pivot_features, read_features, write_features
from ingest.pipeline import run_ingest
from ingest.reader import ReplayReader
from ingest.rules import CATALOGUE_PATH, parse_catalogue
from ingest.store import CLEAN_TABLE as INGEST_CLEAN_TABLE
from ingest.store import IngestStore
from simulator.engine import simulate
from simulator.scenario import find_scenario
from simulator.store import write_run

PACKAGE = Path(features.__file__).parent
BASE = datetime(2026, 1, 1, tzinfo=UTC)
B = minute_of(BASE)
FS = get_features()
SIM_MINUTES = 1560  # 26 hours: every window is full by the end


def at_minute(m: int) -> datetime:
    return BASE + timedelta(minutes=m)


# --- building stores ---


Series = dict[tuple[str, str, str], list[tuple[int, float]]]


def make_store(path: Path, series: Series) -> Path:
    """An ingest-shaped store whose clean_metrics holds exactly the given samples."""
    IngestStore(path).close()
    rows = [
        ((B + m) * 60_000_000, "c1", otype, oid, metric, value, "x")
        for (otype, oid, metric), samples in series.items()
        for m, value in samples
    ]
    if rows:
        frame = pl.DataFrame(
            rows,
            schema=["ts_us", "cluster", "object_type", "object_id", "metric", "value", "unit"],
            orient="row",
        )  # fmt: skip
        staged = path.with_suffix(".parquet")
        frame.write_parquet(staged)
        con = duckdb.connect(str(path))
        con.execute(
            f"INSERT INTO {CLEAN_TABLE} SELECT make_timestamptz(ts_us), cluster, object_type, "
            "object_id, metric, value, unit FROM read_parquet(?)",
            [staged.as_posix()],
        )
        con.close()
        staged.unlink()
    return path


def epochs(column: pl.Series) -> list[int]:
    """Timestamps as epoch microseconds (avoids needing a tz database to build datetimes)."""
    return [int(v) for v in column.dt.epoch("us").to_list()]


def micros(m: int) -> int:
    return (B + m) * 60_000_000


def value_of(frame: pl.DataFrame, otype: str, oid: str, name: str) -> float | None:
    found = frame.filter(
        (pl.col("object_type") == otype)
        & (pl.col("object_id") == oid)
        & (pl.col("feature_name") == name)
    )["value"].to_list()
    assert len(found) == 1, f"{otype}/{oid}/{name}: {len(found)} rows"
    return found[0]  # type: ignore[no-any-return]


VOL = ("volume", "vol01", "space.used_percent")


def ramp(start: int, stop: int, base: float = 40.0, rate: float = 0.01) -> list[tuple[int, float]]:
    return [(m, base + rate * m) for m in range(start, stop + 1)]


@pytest.fixture(scope="session")
def sim_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real ingested store: 26 hours of a growing aggregate."""
    work = tmp_path_factory.mktemp("features")
    variant = replace(
        find_scenario("aggregate_filling").variant(Variant.MODERATE), duration_minutes=SIM_MINUTES
    )
    write_run(work / "m.duckdb", simulate(variant, seed=1))
    with ReplayReader(work / "m.duckdb", 60) as reader:
        report = run_ingest(reader.batches(), work / "store.duckdb", "m", 60)
    assert report.status == "passed"
    return work / "store.duckdb"


def copy_store(source: Path, target_dir: Path) -> Path:
    target = target_dir / "store.duckdb"
    shutil.copy(source, target)
    return target


# --- hand-checked values ---


def test_a_linear_ramp_gives_hand_checked_slope_level_and_time_to_full(tmp_path: Path) -> None:
    # value = 40 + 0.01 * minute, one sample a minute: slope 0.6 per hour exactly
    store = make_store(tmp_path / "s.duckdb", {VOL: ramp(0, 1500)})
    frame = compute_features_at(store, at_minute(1440))

    def get(name: str) -> float | None:
        return value_of(frame, VOL[0], VOL[1], name)

    assert get("space.used_percent__level") == pytest.approx(54.4)
    for window in ("15m", "1h", "6h", "24h"):
        assert get(f"space.used_percent__slope_{window}") == pytest.approx(0.6, rel=1e-9)

    # 15m window = minutes 1426..1440: mean 54.33, min 54.26, max 54.40,
    # std 0.01 * sqrt((15^2-1)/12)
    assert get("space.used_percent__mean_15m") == pytest.approx(54.33)
    assert get("space.used_percent__min_15m") == pytest.approx(54.26)
    assert get("space.used_percent__max_15m") == pytest.approx(54.40)
    assert get("space.used_percent__std_15m") == pytest.approx(0.01 * math.sqrt((15**2 - 1) / 12))
    # 24h window = minutes 1..1440: mean 40 + 0.01 * 720.5
    assert get("space.used_percent__mean_24h") == pytest.approx(47.205)

    # (100 - 54.4) / (0.01 per minute) = 4560 minutes, whichever window gives the slope
    for window in ("1h", "6h", "24h"):
        assert get(f"space.used_percent__ttf_{window}") == pytest.approx(4560.0, rel=1e-9)


def test_time_to_full_edge_cases(tmp_path: Path) -> None:
    cap = 43200.0
    flat = [(m, 50.0) for m in range(0, 400)]
    falling = [(m, 80.0 - 0.01 * m) for m in range(0, 400)]
    full = [(m, 99.0 + 0.01 * m) for m in range(0, 400)]  # passes 100 at minute 100
    store = make_store(
        tmp_path / "s.duckdb",
        {
            ("volume", "flat", "space.used_percent"): flat,
            ("volume", "falling", "space.used_percent"): falling,
            ("volume", "full", "space.used_percent"): full,
            ("volume", "slow", "space.used_percent"): [(m, 10.0 + 1e-6 * m) for m in range(0, 400)],
        },
    )
    frame = compute_features_at(store, at_minute(399))
    ttf = "space.used_percent__ttf_1h"
    assert value_of(frame, "volume", "flat", ttf) == cap  # not filling: the documented cap
    assert value_of(frame, "volume", "falling", ttf) == cap
    assert value_of(frame, "volume", "full", ttf) == 0.0  # already at or past 100
    assert value_of(frame, "volume", "slow", ttf) == cap  # the estimate is longer than the cap
    assert value_of(frame, "volume", "flat", "space.used_percent__slope_1h") == 0.0
    assert value_of(frame, "volume", "flat", "space.used_percent__std_1h") == 0.0


# --- no future data ---


def test_features_at_t_are_identical_when_all_later_data_is_removed(
    sim_store: Path, tmp_path: Path
) -> None:
    t = at_minute(1500)
    truncated = copy_store(sim_store, tmp_path)
    con = duckdb.connect(str(truncated))
    con.execute(
        f"DELETE FROM {CLEAN_TABLE} WHERE ts > make_timestamptz(?)", [minute_of(t) * 60_000_000]
    )
    assert con.execute(f"SELECT count(*) FROM {CLEAN_TABLE}").fetchone()[0] < 215280  # type: ignore[index]
    con.close()

    full_at, cut_at = compute_features_at(sim_store, t), compute_features_at(truncated, t)
    assert full_at.height > 0 and full_at.equals(cut_at)

    start = t - timedelta(minutes=60)
    full_range = compute_features_range(sim_store, start, t + timedelta(minutes=60), step=5)
    cut_range = compute_features_range(truncated, start, t, step=5)
    assert full_range.filter(pl.col("ts") <= t).equals(cut_range)


def test_a_series_with_no_sample_yet_has_no_row(tmp_path: Path) -> None:
    store = make_store(
        tmp_path / "s.duckdb",
        {VOL: ramp(0, 600), ("volume", "late", "space.used_percent"): ramp(300, 600)},
    )
    early = compute_features_at(store, at_minute(100))
    assert set(early["object_id"].unique()) == {"vol01"}  # `late` has not started
    in_range = compute_features_range(store, at_minute(100), at_minute(500), step=60)
    late_times = epochs(in_range.filter(pl.col("object_id") == "late")["ts"].unique().sort())
    assert late_times == [micros(m) for m in (300, 360, 420, 480)]  # steps are epoch-aligned


# --- one implementation, two paths ---


def test_training_and_serving_paths_give_identical_values(sim_store: Path) -> None:
    end = at_minute(1555)
    times = [end - timedelta(minutes=5 * k) for k in range(5)]
    ranged = compute_features_range(sim_store, times[-1], end, step=5)
    keys = ["object_type", "object_id", "feature_name", "ts"]
    served = pl.concat([compute_features_at(sim_store, t) for t in times]).sort(keys)
    assert ranged.height == served.height > 5 * 138
    assert ranged.equals(served)  # same rows, bit-for-bit the same values


def test_serving_floors_the_time_to_the_minute(sim_store: Path) -> None:
    exact = compute_features_at(sim_store, at_minute(1500))
    assert compute_features_at(sim_store, at_minute(1500) + timedelta(seconds=45)).equals(exact)


def test_the_range_does_not_depend_on_its_start(sim_store: Path) -> None:
    wide = compute_features_range(sim_store, at_minute(1400), at_minute(1500), step=5)
    narrow = compute_features_range(sim_store, at_minute(1480), at_minute(1500), step=5)
    keys = ["object_type", "object_id", "feature_name", "ts"]
    assert wide.filter(pl.col("ts") >= at_minute(1480)).sort(keys).equals(narrow)


# --- idempotency ---


def stored(store: Path) -> list[tuple[Any, ...]]:
    con = duckdb.connect(str(store), read_only=True)
    try:
        return con.execute(
            "SELECT epoch_us(ts), object_type, object_id, feature_name, value "
            f"FROM {FEATURES_TABLE} ORDER BY ALL"
        ).fetchall()
    finally:
        con.close()


def test_rerunning_changes_nothing(sim_store: Path, tmp_path: Path) -> None:
    store = copy_store(sim_store, tmp_path)
    first = write_features(store, at_minute(1440), at_minute(1500), step=10)
    before = stored(store)
    second = write_features(store, at_minute(1440), at_minute(1500), step=10)
    assert before and stored(store) == before and first == second
    assert len(before) == first.rows
    # a rebuild gives the same table too
    write_features(store, at_minute(1440), at_minute(1500), step=10, rebuild=True)
    assert stored(store) == before
    # and the stored rows are what the range path computes
    computed = compute_features_range(store, at_minute(1440), at_minute(1500), step=10)
    assert computed.height == len(before)


def test_overlapping_runs_agree_where_they_overlap(sim_store: Path, tmp_path: Path) -> None:
    store = copy_store(sim_store, tmp_path)
    write_features(store, at_minute(1440), at_minute(1480), step=10)
    write_features(store, at_minute(1460), at_minute(1500), step=10)
    (tmp_path / "once").mkdir()
    once = copy_store(sim_store, tmp_path / "once")
    write_features(once, at_minute(1440), at_minute(1500), step=10)
    assert stored(store) == stored(once)


# --- missing data: null or a documented fallback, never a crash ---


def names(frame: pl.DataFrame, oid: str = "vol01") -> dict[str, float | None]:
    sub = frame.filter(pl.col("object_id") == oid)
    return dict(zip(sub["feature_name"].to_list(), sub["value"].to_list(), strict=True))


def test_a_partial_gap_still_computes_from_the_present_samples(tmp_path: Path) -> None:
    present = [
        (m, v) for m, v in ramp(0, 1500) if not 1390 <= m <= 1409
    ]  # 20 of the last 60 minutes
    store = make_store(tmp_path / "s.duckdb", {VOL: present})
    got = names(compute_features_at(store, at_minute(1440)))
    window = [v for m, v in present if 1381 <= m <= 1440]
    assert len(window) == 40
    assert got["space.used_percent__mean_1h"] == pytest.approx(sum(window) / 40)
    assert got["space.used_percent__slope_1h"] == pytest.approx(0.6)  # a straight line still fits
    assert got["space.used_percent__ttf_1h"] == pytest.approx(4560.0)


def test_too_little_coverage_gives_null_but_keeps_the_level(tmp_path: Path) -> None:
    present = [
        (m, v) for m, v in ramp(0, 1500) if not 1381 <= m <= 1430
    ]  # 10 of the last 60 minutes
    store = make_store(tmp_path / "s.duckdb", {VOL: present})
    got = names(compute_features_at(store, at_minute(1440)))
    for fn in ("mean", "min", "max", "std", "slope", "ttf"):
        assert got[f"space.used_percent__{fn}_1h"] is None, fn
    assert got["space.used_percent__level"] == pytest.approx(54.4)  # the latest sample is fresh
    assert got["space.used_percent__mean_15m"] is not None  # 10 of 15 present: enough


def test_a_stale_series_has_no_level_and_a_single_sample_has_no_slope(tmp_path: Path) -> None:
    store = make_store(
        tmp_path / "s.duckdb",
        {VOL: ramp(0, 100), ("volume", "one", "space.used_percent"): [(1000, 42.0)]},
    )
    frame = compute_features_at(store, at_minute(1000))
    stale = names(frame, "vol01")
    assert stale["space.used_percent__level"] is None  # last sample is 900 minutes old
    assert all(v is None for v in stale.values())
    one = names(frame, "one")
    assert one["space.used_percent__level"] == 42.0
    assert (
        one["space.used_percent__slope_15m"] is None and one["space.used_percent__std_15m"] is None
    )
    assert one["space.used_percent__ttf_1h"] is None


def test_before_any_data_and_for_empty_stores_nothing_crashes(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.duckdb", {VOL: ramp(0, 100)})
    assert compute_features_at(store, at_minute(-500)).is_empty()
    assert compute_features_range(store, at_minute(-500), at_minute(-400), step=5).is_empty()
    empty = make_store(tmp_path / "e.duckdb", {})
    assert compute_features_at(empty, at_minute(10)).is_empty()
    with pytest.raises(FeaturesError, match="empty"):
        write_features(empty)


def test_a_store_without_clean_metrics_is_a_clear_error(tmp_path: Path) -> None:
    other = tmp_path / "other.duckdb"
    con = duckdb.connect(str(other))
    con.execute("CREATE TABLE something (x INTEGER)")
    con.close()
    with pytest.raises(FeaturesError, match="clean_metrics"):
        compute_features_at(other, at_minute(0))
    with pytest.raises(FeaturesError, match="not found"):
        compute_features_at(tmp_path / "nope.duckdb", at_minute(0))


def test_mixed_clusters_are_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path / "s.duckdb", {VOL: ramp(0, 50)})
    con = duckdb.connect(str(store))
    con.execute(
        f"UPDATE {CLEAN_TABLE} SET cluster = 'c2' WHERE ts = make_timestamptz(?)", [B * 60_000_000]
    )
    con.close()
    with pytest.raises(FeaturesError, match="more than one cluster"):
        compute_features_at(store, at_minute(40))


# --- pivot and reading back ---


def test_pivot_gives_a_frame_per_object_type(sim_store: Path) -> None:
    long = compute_features_range(sim_store, at_minute(1500), at_minute(1510), step=5)
    volumes = pivot_features(long, "volume")
    assert volumes.height == 3 * 10  # three times, ten volumes
    assert volumes.columns[:2] == ["ts", "object_id"]
    wanted = sorted(s.name for s in FS.specs if s.object_type == "volume")
    assert volumes.columns[2:] == wanted
    assert volumes["object_id"].n_unique() == 10
    aggregates = pivot_features(long, "aggregate")
    assert aggregates.height == 3 * 2 and len(aggregates.columns) == 2 + len(
        {s.name for s in FS.specs if s.object_type == "aggregate"}
    )
    assert pivot_features(long, "trident_backend").is_empty()


def test_stored_features_read_back_the_same_as_computed(sim_store: Path, tmp_path: Path) -> None:
    store = copy_store(sim_store, tmp_path)
    write_features(store, at_minute(1500), at_minute(1510), step=5)
    computed = pivot_features(
        compute_features_range(store, at_minute(1500), at_minute(1510), 5), "qos"
    )
    assert read_features(store, "qos").equals(computed)
    only_last = read_features(store, "qos", start=at_minute(1510))
    assert only_last.height == 2 and epochs(only_last["ts"].unique()) == [micros(1510)]
    with pytest.raises(FeaturesError, match="no features table"):
        read_features(sim_store, "qos")


# --- the catalogue, the docs and the implementation agree ---


def test_every_object_type_has_every_requested_kind_of_feature() -> None:
    for object_type in ("aggregate", "volume", "snapmirror", "qos"):
        have = {s.function for s in FS.specs if s.object_type == object_type}
        assert {"last", "mean", "min", "max", "std", "slope"} <= have, object_type
    windows = {s.window for s in FS.specs if s.function == "mean"}
    assert windows == {"15m", "1h", "6h", "24h"}
    capacity = {s.metric for s in FS.specs if s.function == "time_to_full"}
    assert capacity == {"space.used_percent", "snapshot.reserve_used_percent"}


def test_every_coverage_entry_resolves_to_features() -> None:
    assert set(FS.coverage) >= {
        "snapshot_to_active_ratio_trend", "efficiency_ratio_trend",
        "latency_p95_trend", "snapmirror_lag_trend", "time_to_full",
    }  # fmt: skip
    for name, rule in FS.coverage.items():
        assert matching(FS, rule), name


def test_the_registry_and_the_yaml_use_the_same_functions() -> None:
    assert {s.function for s in FS.specs} == set(FUNCTIONS)


def test_the_computed_features_are_exactly_the_catalogued_ones(sim_store: Path) -> None:
    frame = compute_features_at(sim_store, at_minute(1500))
    for (otype, oid), group in frame.group_by(["object_type", "object_id"]):
        expected = {s.name for s in FS.specs if s.object_type == otype}
        got = group["feature_name"].to_list()
        assert len(got) == len(set(got)) and set(got) == expected, (otype, oid)


def test_features_md_is_generated_from_features_yaml() -> None:
    committed = catalogue_module.DOC_PATH.read_text(encoding="utf-8")
    assert committed == catalogue_module.render(), (
        "docs/FEATURES.md is stale; run `python -m features.catalogue`"
    )
    for spec in FS.specs:
        assert f"`{spec.name}`" in committed


def test_the_documented_features_table_matches_the_real_one(tmp_path: Path) -> None:
    text = catalogue_module.DOC_PATH.read_text(encoding="utf-8")
    section = text.split("## The features table")[1].split("\n## ")[0]
    documented = re.findall(r"^\| `(\w+)` \| ([A-Z ]+?) \| (YES|NO) \|", section, re.M)
    key = re.findall(r"`(\w+)`", re.search(r"\*\*Primary key:\*\* (.+)", section).group(1))  # type: ignore[union-attr]
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute(DDL)
    real = con.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'features' ORDER BY ordinal_position"
    ).fetchall()
    real_key = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() WHERE table_name = 'features' "
        "AND constraint_type = 'PRIMARY KEY'"
    ).fetchall()[0][0]
    con.close()
    assert [tuple(r) for r in real] == documented
    assert key == list(real_key)


def test_the_clean_table_name_matches_ingest() -> None:
    assert CLEAN_TABLE == INGEST_CLEAN_TABLE


# --- features.yaml is validated ---


CATALOGUE = parse_catalogue(CATALOGUE_PATH.read_text(encoding="utf-8"))


def doc(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "windows": {"15m": 15, "1h": 60},
        "features": [
            {
                "name": "{metric}__mean_{window}",
                "metrics": ["iops"],
                "function": "mean",
                "windows": "all",
            }
        ],
    }
    base.update(changes)
    return base


def entry(**changes: Any) -> dict[str, Any]:
    return {**doc()["features"][0], **changes}


def test_a_valid_small_doc_expands() -> None:
    feature_set = parse_features(doc(), CATALOGUE)
    assert {s.name for s in feature_set.specs} == {"iops__mean_15m", "iops__mean_1h"}
    assert {s.object_type for s in feature_set.specs} == {"qos", "volume"}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (doc(features=[entry(function="median")]), "unknown function"),
        (doc(features=[entry(windows=["3d"])]), "unknown window"),
        (doc(features=[entry(name="mean_{window}")]), "must contain"),
        (doc(features=[entry(name="{metric}__mean")]), "belongs in the name"),
        (doc(features=[entry(metrics=["nope"])]), "not in docs/METRICS.md"),
        (doc(features=[entry(), entry()]), "duplicate feature"),
        (doc(features=[entry(window="1h")]), "exactly one"),
        (doc(defaults={"min_coverage": 0}), "min_coverage"),
        (doc(defaults={"level_window": "9h"}), "level_window"),
        (doc(windows={}), "at least one window"),
        (doc(extra=1), "unknown keys"),
        (doc(coverage={"x": {"function": "slope"}}), "matches no feature"),
    ],
)
def test_bad_feature_files_are_rejected(bad: dict[str, Any], message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_features(bad, CATALOGUE)


def test_the_shipped_file_loads_with_its_settings() -> None:
    loaded = load_features()
    assert loaded.min_coverage == 0.5 and loaded.level_minutes == 15 and loaded.max_window == 1440


# --- reads clean_metrics only ---


def test_features_source_has_no_route_to_labels_quarantine_or_the_simulator() -> None:
    forbidden = (
        "scenario_labels",
        "read_labels",
        "LABELS_TABLE",
        ".labels.duckdb",
        "quarantine",
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
                assert not re.match(r"(simulator|eval|models)(\.|$)", module), (source.name, module)


def test_only_the_store_file_is_opened(sim_store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    real_connect = duckdb.connect

    def spy(database: str, *args: object, **kwargs: object) -> Any:
        opened.append(Path(database).name)
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(duckdb, "connect", spy)
    compute_features_at(sim_store, at_minute(1500))
    assert set(opened) == {sim_store.name}


# --- the command line ---


def test_cli_writes_the_table(
    sim_store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = copy_store(sim_store, tmp_path)
    args = [
        "--store",
        str(store),
        "--step",
        "10",
        "--start",
        "2026-01-02T01:00:00Z",
        "--end",
        "2026-01-02T01:30:00Z",
    ]
    assert main(args) == 0
    assert "feature rows for 138 series at 4 times" in capsys.readouterr().out
    first = stored(store)
    assert main([*args, "--rebuild"]) == 0 and stored(store) == first


@pytest.mark.parametrize("bad", [["--step", "0"], ["--start", "yesterday"]])
def test_cli_rejects_bad_arguments(sim_store: Path, tmp_path: Path, bad: list[str]) -> None:
    store = copy_store(sim_store, tmp_path)
    try:
        code = main(["--store", str(store), *bad])
    except SystemExit as err:  # argparse exits 2 on a value it cannot parse
        code = int(err.code or 0)
    assert code == 2


def test_cli_reports_a_missing_or_empty_store(tmp_path: Path) -> None:
    assert main(["--store", str(tmp_path / "missing.duckdb")]) == 2
    assert main(["--store", str(make_store(tmp_path / "e.duckdb", {}))]) == 2
