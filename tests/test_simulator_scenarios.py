from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from contracts import ScenarioType, Variant
from simulator.engine import simulate
from simulator.scenario import (
    SCENARIO_DIRS,
    ScenarioError,
    Split,
    load_scenario,
    parse_scenario,
    scenario_files,
)

SHIPPED = sorted(SCENARIO_DIRS[0].glob("*.yaml"))
TRAINING_SCENARIOS = {
    "aggregate_filling",
    "snapshot_bloat",
    "autogrow_ceiling",
    "latency_storm",
    "snapmirror_lag_creep",
    "efficiency_regression",
}


def test_scenarios_one_to_six_are_shipped() -> None:
    assert {p.stem for p in SHIPPED} == TRAINING_SCENARIOS


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_shipped_file_has_three_variants_and_one_validation(path: Path) -> None:
    definition = load_scenario(path)
    assert definition.scenario == ScenarioType(path.stem)
    assert set(definition.variants) == set(Variant)
    splits = [v.split for v in definition.variants.values()]
    assert splits.count(Split.VALIDATION) == 1
    assert splits.count(Split.TRAIN) == 2


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_shipped_file_runs_and_changes_its_labelled_objects(path: Path) -> None:
    definition = load_scenario(path)
    for variant in definition.variants.values():
        result = simulate(variant, seed=5)
        healthy = simulate(replace(variant, effects=()), seed=5)
        assert result.labels and all(np.isfinite(v).all() for v in result.series.values())
        for label in result.labels:
            changed = [
                key
                for key in result.series
                if key[1] == label.object_id
                and not np.array_equal(result.series[key], healthy.series[key])
            ]
            assert changed, f"{path.stem}/{variant.variant}: nothing changed on {label.object_id}"


def test_heldout_scenarios_load_if_present() -> None:
    for path in scenario_files([SCENARIO_DIRS[1]]):
        load_scenario(path)  # vacuous until the author adds scenarios 7 and 8


def valid_doc() -> dict[str, Any]:
    return {
        "scenario": "snapmirror_lag_creep",
        "duration_minutes": 100,
        "effects": [
            {
                "target": {
                    "object_type": "snapmirror",
                    "object_id": "sm1",
                    "metric": "lag_seconds",
                },
                "pattern": "step",
                "start_minute": 10,
                "magnitude": "$jump",
            }
        ],
        "labels": [{"object_id": "sm1", "start_minute": 10}],
        "variants": {"mild": {"split": "train", "params": {"jump": 5}}},
    }


def test_valid_doc_parses_and_resolves_parameters() -> None:
    variant = parse_scenario(valid_doc()).variant(Variant.MILD)
    assert variant.effects[0].pattern.magnitude == 5.0


def test_numeric_strings_are_accepted() -> None:
    doc = valid_doc()
    doc["variants"]["mild"]["params"]["jump"] = "2e12"  # YAML 1.1 reads 2e12 as a string
    assert parse_scenario(doc).variant(Variant.MILD).effects[0].pattern.magnitude == 2e12


def mutate(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = valid_doc()
    change(doc)
    return doc


BAD_DOCS = {
    "unknown top-level key": mutate(lambda d: d.update(colour="red")),
    "unknown scenario type": mutate(lambda d: d.update(scenario="disk_fire")),
    "unknown parameter": mutate(lambda d: d["variants"]["mild"]["params"].clear()),
    "unused parameter": mutate(lambda d: d["variants"]["mild"]["params"].update(extra=1)),
    "unknown pattern": mutate(lambda d: d["effects"][0].update(pattern="sawtooth")),
    "unknown effect key": mutate(lambda d: d["effects"][0].update(speed=3)),
    "bad mode": mutate(lambda d: d["effects"][0].update(mode="power")),
    "start after run": mutate(lambda d: d["effects"][0].update(start_minute=100)),
    "noise without sigma": mutate(
        lambda d: d["effects"][0].update(pattern="noise", magnitude=None)
    ),
    "no labels": mutate(lambda d: d.update(labels=[])),
    "unknown variant": mutate(lambda d: d["variants"].update(extreme={"split": "train"})),
    "unknown split": mutate(lambda d: d["variants"]["mild"].update(split="test")),
    "extra aggregate": mutate(
        lambda d: d.update(
            extra_objects=[
                {
                    "object_type": "aggregate",
                    "object_id": "aggr9",
                    "metrics": [{"name": "x", "unit": "u", "base": 1}],
                }
            ]
        )
    ),
}


@pytest.mark.parametrize("doc", BAD_DOCS.values(), ids=BAD_DOCS.keys())
def test_malformed_scenarios_are_rejected(doc: dict[str, Any]) -> None:
    with pytest.raises(ScenarioError):
        parse_scenario(doc)


def test_effect_on_missing_metric_names_the_valid_ones() -> None:
    doc = valid_doc()
    doc["effects"][0]["target"]["metric"] = "space.used_percent"  # derived, not raw
    variant = parse_scenario(doc).variant(Variant.MILD)
    with pytest.raises(ScenarioError, match="lag_seconds"):
        simulate(variant, seed=1)


def test_label_on_missing_object_is_rejected() -> None:
    doc = valid_doc()
    doc["labels"][0]["object_id"] = "nope"
    with pytest.raises(ScenarioError, match="nope"):
        simulate(parse_scenario(doc).variant(Variant.MILD), seed=1)
