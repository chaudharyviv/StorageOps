"""Shared Pydantic contracts. Every component imports records from here."""

from contracts.audit import GENESIS_HASH, AuditRecord, ChainCheck, Decision, verify_chain
from contracts.labels import ScenarioLabel, ScenarioType, Variant
from contracts.records import (
    ActionStatus,
    ActionType,
    Alert,
    AlertSource,
    EvidenceWindow,
    Incident,
    MetricRecord,
    ObjectType,
    ProposedAction,
    Risk,
    Severity,
)

__all__ = [
    "GENESIS_HASH",
    "ActionStatus",
    "ActionType",
    "Alert",
    "AlertSource",
    "AuditRecord",
    "ChainCheck",
    "Decision",
    "EvidenceWindow",
    "Incident",
    "MetricRecord",
    "ObjectType",
    "ProposedAction",
    "Risk",
    "ScenarioLabel",
    "ScenarioType",
    "Severity",
    "Variant",
    "verify_chain",
]
