"""Stable identifiers for mock objects, derived from names so they survive restarts."""

from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("5b1d6a40-0c1e-5f55-9c52-7a1c4d2e8f10")
QOS_PGID_BASE = 1000


def uuid_for(kind: str, object_id: str) -> str:
    """Stable uuid for an object, so ids do not change between runs or replay times."""
    return str(uuid.uuid5(NAMESPACE, f"{kind}/{object_id}"))
