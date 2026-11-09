"""Scenario labels. Stored apart from metrics; only eval/ may join them."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from contracts.records import Contract


class ScenarioType(StrEnum):
    """The eight ONTAP failure types from the design doc."""

    AGGREGATE_FILLING = "aggregate_filling"
    SNAPSHOT_BLOAT = "snapshot_bloat"
    AUTOGROW_CEILING = "autogrow_ceiling"
    LATENCY_STORM = "latency_storm"
    SNAPMIRROR_LAG_CREEP = "snapmirror_lag_creep"
    EFFICIENCY_REGRESSION = "efficiency_regression"
    RANSOMWARE_LIKE_CHANGE = "ransomware_like_change"
    TRIDENT_PVC_BURST = "trident_pvc_burst"


class Variant(StrEnum):
    """Scenario parameter variant."""

    MILD = "mild"
    MODERATE = "moderate"
    SEVERE = "severe"


class ScenarioLabel(Contract):
    """Ground truth for one injected failure on one object over a time range."""

    label_id: str = Field(min_length=1)
    scenario: ScenarioType
    variant: Variant
    cluster: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _start_before_end(self) -> ScenarioLabel:
        if self.start > self.end:
            raise ValueError("label start must not be after end")
        return self
