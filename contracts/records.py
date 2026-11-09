"""Shared records passed between components. Import these; never redefine them locally."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    """Base for all contracts: immutable, and unknown fields are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ObjectType(StrEnum):
    """ONTAP object kinds that emit metrics."""

    AGGREGATE = "aggregate"
    VOLUME = "volume"
    SNAPMIRROR = "snapmirror"
    QOS = "qos"


class AlertSource(StrEnum):
    """What raised an alert."""

    RULE = "rule"
    MODEL = "model"


class Severity(StrEnum):
    """Alert severity."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(StrEnum):
    """The only action types the agent may propose."""

    RESIZE_VOLUME = "resize_volume"
    SNAPSHOT_CLEANUP = "snapshot_cleanup"
    SNAPMIRROR_RESYNC = "snapmirror_resync"


class ActionStatus(StrEnum):
    """Lifecycle state of a proposed action."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Risk(StrEnum):
    """Risk level of a proposed action."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MetricRecord(Contract):
    """One telemetry sample for one ONTAP object. Carries no scenario label."""

    ts: AwareDatetime
    cluster: str = Field(min_length=1)
    object_type: ObjectType
    object_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1)


class EvidenceWindow(Contract):
    """Time range of the data that supports an alert."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _start_before_end(self) -> EvidenceWindow:
        if self.start > self.end:
            raise ValueError("evidence window start must not be after end")
        return self


class Alert(Contract):
    """A rule or model flagged an object."""

    alert_id: str = Field(min_length=1)
    ts: AwareDatetime
    object_id: str = Field(min_length=1)
    source: AlertSource
    severity: Severity
    evidence_window: EvidenceWindow


class Incident(Contract):
    """A summarised incident built from one or more alerts."""

    incident_id: str = Field(min_length=1)
    alert_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    runbook_steps: list[str]
    model_version: str = Field(min_length=1)
    retrieved_docs: list[str] = Field(default_factory=list)


class ProposedAction(Contract):
    """An action the agent proposes; it changes nothing until approved."""

    action_id: str = Field(min_length=1)
    incident_id: str = Field(min_length=1)
    type: ActionType
    target: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    risk: Risk
    status: ActionStatus = ActionStatus.PROPOSED
