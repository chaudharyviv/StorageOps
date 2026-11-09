"""Metric rules: which metrics exist (docs/METRICS.md) and what values are plausible.

The catalogue in docs/METRICS.md says which metric names, units and object types are valid.
docs/metric_rules.yaml adds inclusive value bounds. Loading fails if the two disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DOCS = Path(__file__).resolve().parents[1] / "docs"
CATALOGUE_PATH = DOCS / "METRICS.md"
RULES_PATH = DOCS / "metric_rules.yaml"


class RulesError(ValueError):
    """The rules file and the metric catalogue disagree, or a file is malformed."""


@dataclass(frozen=True)
class MetricRule:
    """What one metric must look like to be accepted."""

    metric: str
    unit: str
    object_types: frozenset[str]
    minimum: float | None
    maximum: float | None


def parse_catalogue(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    """Read the metric table in docs/METRICS.md: metric -> (unit, object types)."""
    found: dict[str, tuple[str, frozenset[str]]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`") and cells[0].endswith("`"):
            found[cells[0].strip("`")] = (
                cells[1],
                frozenset(t.strip() for t in cells[2].split(",")),
            )
    return found


def _bound(value: Any, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RulesError(f"{where}: expected a number or null, got {value!r}")
    return float(value)


def build_rules(
    catalogue: dict[str, tuple[str, frozenset[str]]], raw: Any
) -> dict[str, MetricRule]:
    """Combine the catalogue with parsed rules; every metric needs exactly one rule."""
    if not isinstance(raw, dict) or not isinstance(raw.get("metrics"), dict):
        raise RulesError("rules file must have a 'metrics' mapping")
    entries: dict[str, Any] = raw["metrics"]
    if missing := sorted(set(catalogue) - set(entries)):
        raise RulesError(f"metrics in METRICS.md with no rule: {missing}")
    if extra := sorted(set(entries) - set(catalogue)):
        raise RulesError(f"rules for metrics not in METRICS.md: {extra}")
    rules = {}
    for metric, entry in entries.items():
        unit, types = catalogue[metric]
        if not isinstance(entry, dict) or set(entry) - {"unit", "min", "max"}:
            raise RulesError(f"{metric}: expected keys unit, min, max")
        if entry.get("unit") != unit:
            raise RulesError(f"{metric}: rule unit {entry.get('unit')!r} != catalogue {unit!r}")
        low, high = (
            _bound(entry.get("min"), f"{metric}.min"),
            _bound(entry.get("max"), f"{metric}.max"),
        )
        if low is not None and high is not None and low > high:
            raise RulesError(f"{metric}: min {low} is above max {high}")
        rules[metric] = MetricRule(metric, unit, types, low, high)
    return rules


def load_rules(
    catalogue_path: Path = CATALOGUE_PATH, rules_path: Path = RULES_PATH
) -> dict[str, MetricRule]:
    """Load and cross-check the metric rules."""
    catalogue = parse_catalogue(catalogue_path.read_text(encoding="utf-8"))
    return build_rules(catalogue, yaml.safe_load(rules_path.read_text(encoding="utf-8")))
