from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import ObjectType, Variant
from simulator.cluster import CLUSTER, build_cluster
from simulator.mock_ontap_api import builders
from simulator.mock_ontap_api.app import create_app
from simulator.scenario import (
    ScenarioDefinition,
    load_scenario,
    parse_scenario,
    scenario_files,
)
from simulator.store import write_run
from simulator.topology import (
    TOPOLOGY_PATH,
    Topology,
    TopologyError,
    get_topology,
    load_topology,
    parse_topology,
)

TOPO = get_topology()


def missing_objects(definition: ScenarioDefinition, topo: Topology) -> list[str]:
    """Objects a scenario names that neither the topology nor its own extra_objects define."""
    known = topo.object_ids()
    problems = []
    for variant in definition.variants.values():
        extras = {(o.object_type, o.object_id) for o in variant.extra_objects}
        available = known | extras
        for effect in variant.effects:
            target = (effect.target.object_type, effect.target.object_id)
            if target not in available:
                problems.append(f"{variant.variant}: effect target {target[0]}/{target[1]}")
        ids = {object_id for _, object_id in available}
        for label in variant.labels:
            if label.object_id not in ids:
                problems.append(f"{variant.variant}: label object {label.object_id}")
    return problems


def test_there_are_scenario_files_to_check() -> None:
    assert len(scenario_files()) >= 6


@pytest.mark.parametrize("path", scenario_files(), ids=lambda p: p.stem)
def test_every_object_a_scenario_names_exists_in_the_topology(path: Path) -> None:
    assert missing_objects(load_scenario(path), TOPO) == []


def test_the_check_catches_a_scenario_naming_a_missing_object() -> None:
    doc = {
        "scenario": "snapmirror_lag_creep",
        "duration_minutes": 100,
        "effects": [
            {
                "target": {"object_type": "volume", "object_id": "vol99", "metric": "iops"},
                "pattern": "step",
                "magnitude": 1,
            }
        ],
        "labels": [{"object_id": "vol98", "start_minute": 1}],
        "variants": {"mild": {"split": "train"}},
    }
    problems = missing_objects(parse_scenario(doc), TOPO)
    assert any("vol99" in p for p in problems) and any("vol98" in p for p in problems)


def test_the_simulator_builds_exactly_the_topology_objects() -> None:
    built = {(o.object_type, o.object_id) for o in build_cluster()}
    assert built == TOPO.object_ids()
    assert CLUSTER == TOPO.cluster
    assert [o.object_id for o in build_cluster() if o.object_type == ObjectType.VOLUME] == [
        v.id for v in TOPO.volumes
    ]


def test_the_mock_and_the_simulator_share_one_topology_object() -> None:
    assert vars(builders)["get_topology"] is get_topology
    assert load_topology() == TOPO and TOPOLOGY_PATH.name == "topology.yaml"


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, Any]]]:
    from fastapi.testclient import TestClient

    from simulator.engine import simulate
    from simulator.scenario import find_scenario

    db = tmp_path_factory.mktemp("topo") / "m.duckdb"
    write_run(db, simulate(find_scenario("snapmirror_lag_creep").variant(Variant.MILD), seed=1))
    out = {}
    with TestClient(create_app(db)) as http:
        for key, path in builders.COLLECTION_PATHS.items():
            if key != "cluster":
                body = http.get(path, params={"fields": "**"}).json()
                out[key] = body["records"]
    return out


def test_the_mock_serves_the_topology_relations(served: dict[str, list[dict[str, Any]]]) -> None:
    volumes = {v["name"]: v for v in served["volumes"]}
    assert set(volumes) == {v.id for v in TOPO.volumes}
    for entry in TOPO.volumes:
        record = volumes[entry.id]
        assert [a["name"] for a in record["aggregates"]] == [entry.aggregate]
        assert record["qos"]["policy"]["name"] == entry.qos_policy
        assert record["svm"]["name"] == TOPO.svm

    for record in served["aggregates"]:
        assert record["volume-count"] == len(TOPO.volumes_on(record["name"]))
    assert {a["name"] for a in served["aggregates"]} == set(TOPO.aggregates)

    for record in served["qos"]:
        assert record["object_count"] == len(TOPO.volumes_using(record["name"]))
        assert record["fixed"]["max_throughput_iops"] == TOPO.max_throughput_iops(record["name"])
        assert record["svm"]["name"] == TOPO.svm

    assert len(served["snapmirror"]) == len(TOPO.snapmirror)
    for mirror, record in zip(TOPO.snapmirror, served["snapmirror"], strict=True):
        assert record["source"]["path"] == f"{TOPO.svm}:{mirror.source_volume}"
        assert record["source"]["svm"]["name"] == TOPO.svm
        assert record["destination"]["path"] == f"{TOPO.dr_svm}:{mirror.source_volume}_dr"
        assert record["destination"]["svm"]["name"] == TOPO.dr_svm


# --- the loader rejects a topology that points at nothing ---------------------------------


def raw() -> dict[str, Any]:
    data = yaml.safe_load(TOPOLOGY_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_the_shipped_topology_parses() -> None:
    assert parse_topology(raw()) == TOPO


def change_volume(data: dict[str, Any], **fields: str) -> None:
    data["volumes"][0].update(fields)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: change_volume(d, aggregate="aggr9"), "unknown aggregate"),
        (lambda d: change_volume(d, qos_policy="qos9"), "unknown QoS policy"),
        (lambda d: d["snapmirror"][0].update(source_volume="vol99"), "unknown volume"),
        (lambda d: d["volumes"].append(dict(d["volumes"][0])), "duplicate volume"),
        (lambda d: d.pop("svm"), "missing or invalid"),
        (lambda d: d.update(qos_policies=[]), "non-empty list"),
    ],
    ids=["aggregate", "qos", "snapmirror-volume", "duplicate", "missing-key", "empty-list"],
)
def test_topology_errors(mutate: Any, message: str) -> None:
    data = raw()
    mutate(data)
    with pytest.raises(TopologyError, match=message):
        parse_topology(data)
