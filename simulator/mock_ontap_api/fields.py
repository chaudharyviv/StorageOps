"""The `fields` query parameter: default fields, "expensive" fields, `*` and `**`.

The ONTAP reference marks some properties as expensive: they are left out of GET results
unless asked for with `fields`. `**` asks for everything, as the reference describes.
"""

from __future__ import annotations

from typing import Any


def is_expensive(path: str, expensive: tuple[str, ...]) -> bool:
    """Whether a field path matches one of the endpoint's expensive-property patterns."""
    return any(
        path.startswith(p[:-1]) if p.endswith(".*") else path == p or path.startswith(p + ".")
        for p in expensive
    )


def _wanted(path: str, tokens: list[str], expensive: tuple[str, ...]) -> bool:
    if "**" in tokens:
        return True
    if (not tokens or "*" in tokens) and not is_expensive(path, expensive):
        return True
    for token in tokens:
        base = token[:-2] if token.endswith(".*") else token
        if base not in ("*", "") and (path == base or path.startswith(base + ".")):
            return True
    return False


def _prune(value: Any, prefix: str, keep: Any) -> Any:
    """Keep only the leaves that `keep` accepts, dropping objects that end up empty."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            pruned = _prune(child, path, keep)
            if pruned is not _DROP:
                out[key] = pruned
        return out if out else _DROP
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        items = [_prune(v, prefix, keep) for v in value]
        kept = [i for i in items if i is not _DROP]
        return kept if kept else _DROP
    return value if keep(prefix) else _DROP


_DROP = object()


def select_fields(
    record: dict[str, Any], requested: list[str], expensive: tuple[str, ...]
) -> dict[str, Any]:
    """Apply `fields` to one record; `requested` holds the raw (possibly comma-joined) values."""
    tokens = [t.strip() for raw in requested for t in raw.split(",") if t.strip()]
    result = _prune(record, "", lambda path: _wanted(path, tokens, expensive))
    return {} if result is _DROP else dict(result)
