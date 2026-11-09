from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from contracts import GENESIS_HASH, AuditRecord, Decision, verify_chain

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def build_chain(length: int) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    prev = GENESIS_HASH
    for i in range(length):
        record = AuditRecord(
            audit_id=f"aud-{i}",
            ts=T0 + timedelta(minutes=i),
            actor="approver@example.test",
            action_id=f"act-{i}",
            decision=Decision.APPROVED if i % 2 == 0 else Decision.REJECTED,
            reason="synthetic test decision",
            prev_hash=prev,
        )
        records.append(record)
        prev = record.compute_hash()
    return records


def test_hash_is_deterministic_and_hex() -> None:
    record = build_chain(1)[0]
    assert record.compute_hash() == record.compute_hash()
    assert len(record.compute_hash()) == 64


def test_hash_changes_with_any_field() -> None:
    record = build_chain(1)[0]
    edited = record.model_copy(update={"reason": "edited"})
    assert edited.compute_hash() != record.compute_hash()


def test_valid_chain_verifies() -> None:
    chain = build_chain(5)
    assert verify_chain(chain).ok
    assert verify_chain(chain, head_hash=chain[-1].compute_hash()).ok


def test_empty_chain_verifies() -> None:
    assert verify_chain([]).ok


def test_edited_middle_record_is_reported() -> None:
    chain = build_chain(5)
    chain[2] = chain[2].model_copy(update={"decision": Decision.APPROVED, "reason": "forged"})
    result = verify_chain(chain)
    assert not result.ok
    assert result.broken_index == 3


def test_deleted_record_is_reported() -> None:
    chain = build_chain(5)
    del chain[1]
    result = verify_chain(chain)
    assert not result.ok
    assert result.broken_index == 1


def test_reordered_records_are_reported() -> None:
    chain = build_chain(4)
    chain[1], chain[2] = chain[2], chain[1]
    assert verify_chain(chain).broken_index == 1


def test_first_record_must_point_at_genesis() -> None:
    chain = build_chain(3)
    assert verify_chain(chain[1:]).broken_index == 0


def test_edited_last_record_needs_head_hash() -> None:
    chain = build_chain(3)
    head = chain[-1].compute_hash()
    chain[-1] = chain[-1].model_copy(update={"reason": "forged"})
    assert verify_chain(chain).ok
    result = verify_chain(chain, head_hash=head)
    assert not result.ok
    assert result.broken_index == 2


def test_bad_prev_hash_format_rejected() -> None:
    with pytest.raises(ValidationError):
        AuditRecord(
            audit_id="aud-0",
            ts=T0,
            actor="approver@example.test",
            action_id="act-0",
            decision="approved",
            reason="x",
            prev_hash="not-a-hash",
        )


def test_unknown_decision_rejected() -> None:
    with pytest.raises(ValidationError):
        AuditRecord(
            audit_id="aud-0",
            ts=T0,
            actor="approver@example.test",
            action_id="act-0",
            decision="executed",
            reason="x",
            prev_hash=GENESIS_HASH,
        )
