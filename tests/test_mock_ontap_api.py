import ast
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from contracts import Variant
from simulator import store
from simulator.catalogue import emitted_metrics
from simulator.engine import simulate
from simulator.mock_ontap_api import app as app_module
from simulator.mock_ontap_api import builders, identity, replay, subset_doc
from simulator.mock_ontap_api.app import create_app
from simulator.mock_ontap_api.replay import Replay
from simulator.scenario import find_scenario
from simulator.store import write_run

FIXTURES = Path(__file__).parent / "fixtures" / "ontap"
PACKAGE = Path(subset_doc.__file__).parent
EARLY = "2026-01-01T12:00:00Z"  # before the snapmirror scenario starts (day 2)
LATE = "2026-01-07T12:00:00Z"


@pytest.fixture(scope="session")
def snapmirror_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mock") / "snapmirror.duckdb"
    variant = find_scenario("snapmirror_lag_creep").variant(Variant.SEVERE)
    write_run(path, simulate(variant, seed=1))
    return path


@pytest.fixture(scope="session")
def filling_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mock") / "filling.duckdb"
    variant = find_scenario("aggregate_filling").variant(Variant.SEVERE)
    write_run(path, simulate(variant, seed=1))
    return path


@contextmanager
def client(db: Path, now: str | None = None) -> Iterator[TestClient]:
    with TestClient(create_app(db, now)) as test_client:
        yield test_client


def load_fixture(name: str) -> dict[str, Any]:
    data = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def type_ok(value: Any, documented: str) -> bool:
    if documented == "string":
        return isinstance(value, str)
    if documented == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if documented == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if documented == "boolean":
        return isinstance(value, bool)
    return isinstance(value, list)  # array, array[string], ...


ENDPOINT_CASES = [
    ("cluster", "/api/cluster", {}, "cluster.yaml", EARLY),
    ("aggregates", "/api/storage/aggregates", {}, "aggregates.yaml", EARLY),
    ("volumes", "/api/storage/volumes", {}, "volumes.yaml", EARLY),
    ("volumes", "/api/storage/volumes", {"fields": "**"}, "volumes_all_fields.yaml", EARLY),
    ("snapmirror", "/api/snapmirror/relationships", {}, "snapmirror_relationships.yaml", EARLY),
    ("snapmirror", "/api/snapmirror/relationships", {}, "snapmirror_relationships_unhealthy.yaml",
     LATE),
    ("qos", "/api/storage/qos/policies", {}, "qos_policies.yaml", EARLY),
]  # fmt: skip
ENDPOINT_IDS = [c[3] for c in ENDPOINT_CASES]


def first_record(body: dict[str, Any]) -> dict[str, Any]:
    return body["records"][0] if "records" in body else body


@pytest.mark.parametrize(
    ("key", "url", "params", "fixture", "now"), ENDPOINT_CASES, ids=ENDPOINT_IDS
)
def test_response_key_sets_match_the_fixtures(
    snapmirror_db: Path, key: str, url: str, params: dict[str, str], fixture: str, now: str
) -> None:
    with client(snapmirror_db) as http:
        response = http.get(url, params={**params, "mock_now": now})
    assert response.status_code == 200
    body, expected = response.json(), load_fixture(fixture)
    assert set(body) == set(expected)  # _links / num_records / records, or the cluster object
    assert (
        subset_doc.flatten(first_record(body)).keys()
        == subset_doc.flatten(first_record(expected)).keys()
    )
    if "records" in body:
        assert body["_links"]["self"]["href"] == url
        assert body["num_records"] == len(body["records"])


@pytest.mark.parametrize("fixture", sorted({c[3] for c in ENDPOINT_CASES}))
def test_fixtures_only_use_documented_fields(fixture: str) -> None:
    key = next(c[0] for c in ENDPOINT_CASES if c[3] == fixture)
    types = subset_doc.documented_types(next(e for e in subset_doc.ENDPOINTS if e.key == key))
    for path, value in subset_doc.flatten(first_record(load_fixture(fixture))).items():
        assert path in types, f"{fixture}: {path} is not in the 9.19.1 reference"
        assert type_ok(value, types[path]), f"{fixture}: {path}={value!r} is not {types[path]}"


@pytest.mark.parametrize("endpoint", subset_doc.ENDPOINTS, ids=lambda e: e.key)
def test_every_served_field_is_documented_with_a_matching_type(
    endpoint: subset_doc.Endpoint,
) -> None:
    types = subset_doc.documented_types(endpoint)
    for path, value in subset_doc.served_paths(endpoint.key).items():
        assert path in types, f"{endpoint.path}: {path} is not in the 9.19.1 reference"
        assert type_ok(value, types[path]), (
            f"{endpoint.path}: {path}={value!r} is not {types[path]}"
        )


def test_live_responses_use_only_documented_fields(snapmirror_db: Path) -> None:
    with client(snapmirror_db, LATE) as http:
        for endpoint in subset_doc.ENDPOINTS:
            body = http.get(endpoint.path, params={"fields": "**"}).json()
            types = subset_doc.documented_types(endpoint)
            for path, value in subset_doc.flatten(first_record(body)).items():
                assert path in types and type_ok(value, types[path]), (endpoint.path, path)


def test_replay_time_changes_the_values(snapmirror_db: Path, filling_db: Path) -> None:
    with client(snapmirror_db) as http:
        early = http.get("/api/snapmirror/relationships", params={"mock_now": EARLY}).json()
        late = http.get("/api/snapmirror/relationships", params={"mock_now": LATE}).json()
    first_early, first_late = early["records"][0], late["records"][0]
    assert first_early["lag_time"] != first_late["lag_time"]
    assert first_early["healthy"] is True and "unhealthy_reason" not in first_early
    assert first_late["healthy"] is False and first_late["unhealthy_reason"]

    with client(filling_db) as http:

        def used(now: str) -> int:
            records = http.get("/api/storage/aggregates", params={"mock_now": now}).json()[
                "records"
            ]
            return int(records[0]["space"]["block_storage"]["used"])

        assert used(LATE) > used(EARLY) + 1_000_000_000_000  # aggr1 grows several TB


def test_default_replay_time_is_the_last_sample(snapmirror_db: Path) -> None:
    with client(snapmirror_db) as http:
        metric = http.get("/api/storage/volumes", params={"fields": "metric.*"}).json()
    assert metric["records"][0]["metric"]["timestamp"] == "2026-01-07 23:59:00 +0000"


def test_mock_now_setting_and_query_override(
    snapmirror_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stamp(http: TestClient, **params: str) -> str:
        r = http.get("/api/storage/volumes", params={"fields": "metric.*", **params})
        return str(r.json()["records"][0]["metric"]["timestamp"])

    monkeypatch.setenv("MOCK_NOW", "2026-01-03T08:15:42")  # no zone: taken as UTC
    monkeypatch.setenv("MOCK_ONTAP_DB", str(snapmirror_db))
    with TestClient(create_app()) as http:
        assert stamp(http) == "2026-01-03 08:15:00 +0000"  # floored to the sample minute
        assert stamp(http, mock_now="2026-01-04T00:00:00+00:00") == "2026-01-04 00:00:00 +0000"


def test_bad_replay_times_use_the_ontap_error_envelope(snapmirror_db: Path) -> None:
    with client(snapmirror_db) as http:
        before = http.get("/api/cluster", params={"mock_now": "2025-12-31T00:00:00Z"})
        junk = http.get("/api/cluster", params={"mock_now": "not-a-time"})
    assert before.status_code == 404 and before.json()["error"]["code"] == "4"
    assert junk.status_code == 400 and junk.json()["error"]["target"] == "mock_now"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [e.path for e in subset_doc.ENDPOINTS])
def test_write_methods_return_405(snapmirror_db: Path, method: str, path: str) -> None:
    with client(snapmirror_db) as http:
        response = http.request(method, path, json={"name": "x"})
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["error"]["code"] == "405"


def test_unknown_path_is_an_ontap_style_404(snapmirror_db: Path) -> None:
    with client(snapmirror_db) as http:
        response = http.get("/api/storage/luns")
    assert response.status_code == 404 and "error" in response.json()


def test_fields_parameter(snapmirror_db: Path) -> None:
    with client(snapmirror_db, EARLY) as http:

        def keys(**params: Any) -> set[str]:
            body = http.get("/api/storage/volumes", params=params).json()
            return set(subset_doc.flatten(body["records"][0]))

        default = keys()
        assert "space.percent_used" not in default and "metric.iops.total" not in default
        assert {"space.percent_used", "metric.iops.total", "qos.policy.name"} <= keys(fields="**")
        assert keys(fields="*") == default
        assert keys(fields="name,uuid") == {"name", "uuid"}
        assert keys(fields=["name", "space.size"]) == {"name", "space.size"}
        assert "metric.iops.total" in keys(fields="metric.*")
        assert {"name", "metric.status"} <= keys(fields="*,metric.status")


def test_values_are_internally_consistent(snapmirror_db: Path) -> None:
    with client(snapmirror_db, LATE) as http:
        volumes = http.get("/api/storage/volumes", params={"fields": "**"}).json()["records"]
        aggregates = http.get("/api/storage/aggregates").json()["records"]
        qos = http.get("/api/storage/qos/policies").json()["records"]
    for v in volumes:
        assert v["size"] == v["space"]["size"]
        assert v["space"]["used"] + v["space"]["available"] == v["space"]["size"]
        assert v["space"]["snapshot"]["reserve_size"] == v["space"]["size"] // 10
    for a in aggregates:
        block = a["space"]["block_storage"]
        assert block["used"] + block["available"] == block["size"]
    assert sum(a["volume-count"] for a in aggregates) == len(volumes)
    assert sum(p["object_count"] for p in qos) == len(volumes)
    assert {v["qos"]["policy"]["name"] for v in volumes} == {p["name"] for p in qos}


def test_uuids_are_stable_across_replay_times(snapmirror_db: Path) -> None:
    with client(snapmirror_db) as http:
        a = http.get("/api/storage/volumes", params={"mock_now": EARLY}).json()
        b = http.get("/api/storage/volumes", params={"mock_now": LATE}).json()
    assert [r["uuid"] for r in a["records"]] == [r["uuid"] for r in b["records"]]
    assert identity.uuid_for("volume", "vol01") == a["records"][0]["uuid"]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "PT0S"), (59, "PT59S"), (120, "PT2M"), (30942, "PT8H35M42S"), (86400, "P1D"),
     (93784, "P1DT2H3M4S")],
)  # fmt: skip
def test_iso_duration(seconds: int, expected: str) -> None:
    assert builders.iso_duration(seconds) == expected


# --- the mock never reads the labels file -------------------------------------------------


def test_app_never_opens_the_labels_file(
    snapmirror_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels = store.default_labels_path(snapmirror_db)
    assert labels.exists()  # it sits right next to the metrics file
    opened: list[str] = []
    real_connect = duckdb.connect

    def spy(database: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(database).name)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", spy)
    with client(snapmirror_db, LATE) as http:
        for endpoint in subset_doc.ENDPOINTS:
            assert http.get(endpoint.path, params={"fields": "**"}).status_code == 200
    assert opened and set(opened) == {snapmirror_db.name}


def test_app_works_with_the_labels_file_absent(tmp_path: Path) -> None:
    db = tmp_path / "m.duckdb"
    variant = find_scenario("snapmirror_lag_creep").variant(Variant.MILD)
    labels = write_run(db, simulate(variant, seed=2))
    labels.unlink()
    with client(db) as http:
        assert http.get("/api/storage/volumes").status_code == 200


def test_replay_refuses_files_that_hold_labels(snapmirror_db: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no 'metrics' table"):
        Replay(store.default_labels_path(snapmirror_db))
    combined = tmp_path / "combined.duckdb"
    con = duckdb.connect(str(combined))
    con.execute(
        "CREATE TABLE metrics (ts TIMESTAMPTZ, cluster VARCHAR, object_type VARCHAR, "
        "object_id VARCHAR, metric VARCHAR, value DOUBLE, unit VARCHAR)"
    )
    con.execute("CREATE TABLE scenario_labels (label_id VARCHAR)")
    con.close()
    with pytest.raises(ValueError, match="metrics file only"):
        Replay(combined)


def test_package_source_has_no_route_to_the_labels() -> None:
    forbidden_text = ("scenario_labels", "read_labels", "LABELS_TABLE", ".labels.duckdb")
    for source in PACKAGE.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for token in forbidden_text:
            assert token not in text, f"{source.name} mentions {token}"
        for node in ast.walk(ast.parse(text)):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            assert not any(n == "simulator.store" for n in [module, *names]), source.name


def test_replay_table_name_matches_the_store() -> None:
    assert replay.METRICS_TABLE == store.METRICS_TABLE


# --- configuration --------------------------------------------------------------------------


def test_create_app_needs_a_metrics_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOCK_ONTAP_DB", raising=False)
    with pytest.raises(RuntimeError, match="MOCK_ONTAP_DB"):
        create_app()
    with pytest.raises(FileNotFoundError):
        create_app(Path("does-not-exist.duckdb"))


def test_parse_mock_now() -> None:
    assert app_module.parse_mock_now("2026-01-02T03:04:05Z").hour == 3
    assert app_module.parse_mock_now("2026-01-02T03:04:05").tzinfo is not None
    with pytest.raises(ValueError, match="ISO-8601"):
        app_module.parse_mock_now("tomorrow")


# --- documentation stays in step with the code -------------------------------------------------


def test_subset_doc_is_up_to_date() -> None:
    committed = subset_doc.DOC_PATH.read_text(encoding="utf-8")
    assert committed == subset_doc.render(), (
        "docs/mock_ontap_subset.md is stale; run `python -m simulator.mock_ontap_api.subset_doc`"
    )
    assert "ONTAP **9.19.1**" in committed


@pytest.mark.parametrize("endpoint", subset_doc.ENDPOINTS, ids=lambda e: e.key)
def test_every_served_field_has_a_documented_source(endpoint: subset_doc.Endpoint) -> None:
    served = set(subset_doc.served_paths(endpoint.key))
    assert served == set(subset_doc.SOURCES[endpoint.key])


def test_unserved_metrics_are_all_explained() -> None:
    assert set(subset_doc.unserved_metrics()) == set(subset_doc.UNSERVED_REASONS)


# --- type and unit conversions --------------------------------------------------------------

DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def duration_seconds(text: str) -> int:
    """Parse the ISO-8601 durations the mock emits (days, hours, minutes, seconds)."""
    match = DURATION.match(text)
    assert match and text != "P" and not text.endswith("T"), f"not an ISO-8601 duration: {text!r}"
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@pytest.mark.parametrize(
    "seconds", [0, 0.4, 0.6, 1, 59, 60, 61, 3599, 3600, 86399, 86400, 200000.5]
)
def test_lag_time_round_trips_to_whole_seconds(seconds: float) -> None:
    text = builders.iso_duration(seconds)
    assert isinstance(text, str) and text.startswith("P")
    assert duration_seconds(text) == int(round(seconds))


def test_lag_time_in_the_response_matches_the_simulator_metric(snapmirror_db: Path) -> None:
    for now in (EARLY, "2026-01-03T06:07:08Z", LATE):
        with client(snapmirror_db, now) as http:
            records = http.get("/api/snapmirror/relationships").json()["records"]
        snapshot = Replay(snapmirror_db).snapshot(app_module.parse_mock_now(now))
        for record, name in zip(records, snapshot.objects("snapmirror"), strict=True):
            metrics = snapshot.values[("snapmirror", name)]
            assert isinstance(record["lag_time"], str)
            assert duration_seconds(record["lag_time"]) == int(round(metrics["lag_seconds"]))
            assert record["healthy"] is (metrics["transfer_errors"] <= 0)


def test_numeric_fields_are_the_rounded_simulator_values(snapmirror_db: Path) -> None:
    when = app_module.parse_mock_now(LATE)
    snapshot = Replay(snapmirror_db).snapshot(when)
    with client(snapmirror_db, LATE) as http:
        volumes = http.get("/api/storage/volumes", params={"fields": "**"}).json()["records"]
        aggregates = http.get("/api/storage/aggregates").json()["records"]
    for record in volumes:
        m = snapshot.values[("volume", record["name"])]
        assert record["size"] == int(round(m["space.size_bytes"]))
        assert record["space"]["used"] == int(round(m["space.used_bytes"]))
        assert record["space"]["percent_used"] == int(round(m["space.used_percent"]))
        assert record["space"]["logical_space"]["used"] == int(round(m["space.logical_used_bytes"]))
        assert record["space"]["snapshot"]["space_used_percent"] == int(
            round(m["snapshot.reserve_used_percent"])
        )
        assert record["efficiency"]["ratio"] == round(m["efficiency.ratio"], 2)
        assert record["metric"]["iops"]["total"] == int(round(m["iops"]))
        assert record["autosize"]["maximum"] == int(round(m["space.autogrow_max_bytes"]))
    for record in aggregates:
        m = snapshot.values[("aggregate", record["name"])]
        block = record["space"]["block_storage"]
        assert block["size"] == int(round(m["space.size_bytes"]))
        assert block["used_percent"] == int(round(m["space.used_percent"]))


def test_conversion_assumptions_about_simulator_units_still_hold() -> None:
    units = {metric: unit for metric, (unit, _) in emitted_metrics().items()}
    assert units["lag_seconds"] == "seconds"
    assert units["transfer_errors"] == "count"
    assert units["efficiency.ratio"] == "ratio"
    assert units["iops"] == "iops"
    for metric, unit in units.items():
        if metric.endswith("_bytes"):
            assert unit == "bytes", metric
        if metric.endswith("_percent"):
            assert unit == "percent", metric
    # Not served; documented in microseconds and bytes per second, so serving them needs a
    # conversion. If a unit here changes, revisit subset_doc.UNIT_MISMATCH_IF_SERVED.
    assert units["latency.read_p95_ms"] == "ms" and units["latency.write_p95_ms"] == "ms"
    assert units["throughput_mbps"] == "MB/s"


def test_every_conversion_row_names_a_real_field_or_metric() -> None:
    names = set(emitted_metrics())
    for field, (metric, *_rest) in subset_doc.CONVERSIONS.items():
        assert metric.startswith("*") or metric.startswith("(") or metric in names, field
    assert set(subset_doc.UNIT_MISMATCH_IF_SERVED) <= names


# --- the committed documented-field lists hold paths and types only ----------------------------

PATH_AND_TYPE = re.compile(
    r"^[A-Za-z0-9_\-\.\[\]]+\t(string|integer|number|boolean|object|array|array\[(string|integer|number)\])$"
)
DOCUMENTED_DIR = Path(__file__).parent / "fixtures" / "ontap" / "documented_fields"
COMMENT_ALLOWED = re.compile(
    r"^# (Documented response fields: [a-z_]+\.txt"
    r"|Source: https://docs\.netapp\.com/us-en/ontap-restapi/[a-z\-]+\.html"
    r"|ONTAP 9\.19\.1 REST API reference \(fetched \d{4}-\d{2}-\d{2}\)\."
    r" Field paths and types only\."
    r"|Extracted mechanically from the page's response schema; '\[\]' marks an array of objects\.)$"
)


@pytest.mark.parametrize("path", sorted(DOCUMENTED_DIR.iterdir()), ids=lambda p: p.name)
def test_documented_field_lists_hold_only_paths_and_types(path: Path) -> None:
    assert path.suffix == ".txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and sum(1 for line in lines if line.startswith("#")) == 4
    for line in lines:
        if line.startswith("#"):
            assert COMMENT_ALLOWED.match(line), f"unexpected comment text: {line!r}"
        else:
            assert PATH_AND_TYPE.match(line), f"not a path/type pair: {line!r}"
            assert len(line) < 160  # a description sentence would not fit
