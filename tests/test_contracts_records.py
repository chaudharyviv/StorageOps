from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from contracts import (
    ActionStatus,
    ActionType,
    Alert,
    Incident,
    MetricRecord,
    ProposedAction,
    ScenarioLabel,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def metric(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": T0,
        "cluster": "sim-cluster-01",
        "object_type": "aggregate",
        "object_id": "aggr1",
        "metric": "space.used_percent",
        "value": 71.5,
        "unit": "percent",
    }
    return base | overrides


def action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action_id": "act-1",
        "incident_id": "inc-1",
        "type": "resize_volume",
        "target": "vol_app01",
        "params": {"new_size_gb": 600},
        "risk": "low",
    }
    return base | overrides


def test_metric_record_valid() -> None:
    record = MetricRecord(**metric())
    assert record.object_type == "aggregate"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ts": datetime(2026, 1, 1)},  # naive timestamp
        {"object_type": "lun"},
        {"cluster": ""},
        {"value": float("nan")},
        {"value": "not-a-number"},
    ],
)
def test_metric_record_rejects_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MetricRecord(**metric(**overrides))


def test_metric_record_has_no_label_field() -> None:
    assert "scenario" not in MetricRecord.model_fields
    with pytest.raises(ValidationError):
        MetricRecord(**metric(scenario="aggregate_filling"))


def test_records_are_immutable() -> None:
    record = MetricRecord(**metric())
    with pytest.raises(ValidationError):
        record.value = 99.0  # type: ignore[misc]


def test_alert_evidence_window_order() -> None:
    fields: dict[str, Any] = {
        "alert_id": "al-1",
        "ts": T0,
        "object_id": "aggr1",
        "source": "model",
        "severity": "warning",
    }
    Alert(**fields, evidence_window={"start": T0 - timedelta(hours=6), "end": T0})
    with pytest.raises(ValidationError):
        Alert(**fields, evidence_window={"start": T0, "end": T0 - timedelta(hours=6)})


def test_alert_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        Alert(
            alert_id="al-1",
            ts=T0,
            object_id="aggr1",
            source="human",
            severity="warning",
            evidence_window={"start": T0, "end": T0},
        )


def test_incident_needs_an_alert() -> None:
    with pytest.raises(ValidationError):
        Incident(
            incident_id="inc-1",
            alert_ids=[],
            summary="Aggregate aggr1 fills in 4 days.",
            runbook_steps=[],
            model_version="v0",
        )


def test_action_type_allow_list_is_exact() -> None:
    assert {t.value for t in ActionType} == {
        "resize_volume",
        "snapshot_cleanup",
        "snapmirror_resync",
    }


def test_proposed_action_defaults_to_proposed() -> None:
    assert ProposedAction(**action()).status is ActionStatus.PROPOSED


@pytest.mark.parametrize("bad_type", ["delete_volume", "RESIZE_VOLUME", "", "offline_aggregate"])
def test_proposed_action_rejects_unknown_type(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        ProposedAction(**action(type=bad_type))


@pytest.mark.parametrize("bad_status", ["executed", "pending", "APPROVED"])
def test_proposed_action_rejects_unknown_status(bad_status: str) -> None:
    with pytest.raises(ValidationError):
        ProposedAction(**action(status=bad_status))


def test_proposed_action_rejects_unknown_risk() -> None:
    with pytest.raises(ValidationError):
        ProposedAction(**action(risk="extreme"))


def test_scenario_label_valid_and_ordered() -> None:
    fields: dict[str, Any] = {
        "label_id": "lbl-1",
        "scenario": "snapshot_bloat",
        "variant": "moderate",
        "cluster": "sim-cluster-01",
        "object_id": "vol_app01",
    }
    ScenarioLabel(**fields, start=T0, end=T0 + timedelta(days=2))
    with pytest.raises(ValidationError):
        ScenarioLabel(**fields, start=T0, end=T0 - timedelta(days=2))
    with pytest.raises(ValidationError):
        ScenarioLabel(**fields | {"variant": "extreme"}, start=T0, end=T0)
