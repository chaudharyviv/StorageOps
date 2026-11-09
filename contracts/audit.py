"""Hash-chained audit records written by the approval gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from enum import StrEnum

from pydantic import AwareDatetime, Field

from contracts.records import Contract

GENESIS_HASH = "0" * 64
"""prev_hash of the first record in a chain."""


class Decision(StrEnum):
    """Outcome recorded for a proposed action."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AuditRecord(Contract):
    """One decision on a proposed action, linked to the previous record by hash."""

    audit_id: str = Field(min_length=1)
    ts: AwareDatetime
    actor: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    decision: Decision
    reason: str
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def compute_hash(self) -> str:
        """Return the SHA-256 hex digest of this record's canonical JSON."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChainCheck(Contract):
    """Result of verifying an audit chain."""

    ok: bool
    broken_index: int | None = None
    reason: str | None = None


def verify_chain(records: Sequence[AuditRecord], head_hash: str | None = None) -> ChainCheck:
    """Verify an audit chain and report the first broken link.

    Pass head_hash (the stored hash of the last record) to also detect edits to the last record.
    """
    expected = GENESIS_HASH
    for index, record in enumerate(records):
        if record.prev_hash != expected:
            return ChainCheck(
                ok=False, broken_index=index, reason="prev_hash does not match previous record"
            )
        expected = record.compute_hash()
    if head_hash is not None and records and expected != head_hash:
        return ChainCheck(
            ok=False, broken_index=len(records) - 1, reason="last record does not match head_hash"
        )
    return ChainCheck(ok=True)
