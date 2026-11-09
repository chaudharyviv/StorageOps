"""Load features.yaml and expand its templates into concrete feature specs.

The metric list and the object types that emit each metric come from docs/METRICS.md, so a
new metric in the catalogue gets features without editing this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from features.functions import FUNCTIONS
from ingest.rules import CATALOGUE_PATH, parse_catalogue

FEATURES_PATH = Path(__file__).parent / "features.yaml"


class SpecError(ValueError):
    """features.yaml is malformed or names something that does not exist."""


@dataclass(frozen=True)
class FeatureSpec:
    """One concrete feature of one object type."""

    name: str
    object_type: str
    metric: str
    function: str
    window: str | None
    window_minutes: int
    params: tuple[tuple[str, Any], ...]

    @property
    def param_dict(self) -> dict[str, Any]:
        """The function parameters as a dict."""
        return dict(self.params)


@dataclass(frozen=True)
class FeatureSet:
    """All features, plus the settings that apply to every one of them."""

    specs: tuple[FeatureSpec, ...]
    windows: Mapping[str, int]
    min_coverage: float
    level_minutes: int
    coverage: Mapping[str, Mapping[str, Any]]

    def for_series(self, object_type: str, metric: str) -> list[FeatureSpec]:
        """The features computed for one metric of one object type."""
        return [s for s in self.specs if s.object_type == object_type and s.metric == metric]

    @property
    def max_window(self) -> int:
        """The longest look-back any feature needs, in minutes."""
        return max((s.window_minutes for s in self.specs), default=1)

    def metrics_for(self, object_type: str) -> list[str]:
        """Metrics that have features for an object type."""
        return sorted({s.metric for s in self.specs if s.object_type == object_type})

    def object_types(self) -> list[str]:
        """Object types that have features."""
        return sorted({s.object_type for s in self.specs})


def _as_list(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise SpecError(f"{where}: expected a string or a list of strings, got {value!r}")


def parse_features(doc: Any, catalogue: Mapping[str, tuple[str, frozenset[str]]]) -> FeatureSet:
    """Validate a parsed features.yaml and expand it against the metric catalogue."""
    if not isinstance(doc, Mapping):
        raise SpecError("features.yaml: expected a mapping at the top level")
    unknown = set(doc) - {"version", "windows", "defaults", "features", "coverage"}
    if unknown:
        raise SpecError(f"features.yaml: unknown keys {sorted(unknown)}")
    windows = {str(k): int(v) for k, v in dict(doc.get("windows") or {}).items()}
    if not windows or any(v < 1 for v in windows.values()):
        raise SpecError("windows: need at least one window of 1 minute or more")
    defaults = dict(doc.get("defaults") or {})
    coverage_floor = float(defaults.get("min_coverage", 0.5))
    if not 0 < coverage_floor <= 1:
        raise SpecError("defaults.min_coverage must be in (0, 1]")
    level_name = str(defaults.get("level_window", "15m"))
    if level_name not in windows:
        raise SpecError(f"defaults.level_window {level_name!r} is not a defined window")

    specs: list[FeatureSpec] = []
    for index, entry in enumerate(doc.get("features") or []):
        where = f"features[{index}]"
        if not isinstance(entry, Mapping):
            raise SpecError(f"{where}: expected a mapping")
        bad = set(entry) - {"name", "metrics", "exclude", "function", "window", "windows", "params"}
        if bad:
            raise SpecError(f"{where}: unknown keys {sorted(bad)}")
        template, function = str(entry.get("name", "")), str(entry.get("function", ""))
        if "{metric}" not in template:
            raise SpecError(f"{where}: name must contain {{metric}}")
        if function not in FUNCTIONS:
            raise SpecError(f"{where}: unknown function {function!r}; have {sorted(FUNCTIONS)}")
        if ("window" in entry) == ("windows" in entry):
            raise SpecError(f"{where}: give exactly one of `window` or `windows`")
        chosen = (
            [str(entry["window"])]
            if "window" in entry
            else list(windows)
            if entry["windows"] == "all"
            else _as_list(entry["windows"], where)
        )
        for name in chosen:
            if name not in windows:
                raise SpecError(f"{where}: unknown window {name!r}; have {sorted(windows)}")
        if ("{window}" in template) != ("windows" in entry):
            raise SpecError(
                f"{where}: {{window}} belongs in the name exactly when `windows` is used"
            )
        wanted = entry.get("metrics")
        metrics = sorted(catalogue) if wanted == "all" else _as_list(wanted, f"{where}.metrics")
        for metric in metrics:
            if metric not in catalogue:
                raise SpecError(f"{where}: metric {metric!r} is not in docs/METRICS.md")
        skip = set(_as_list(entry.get("exclude", []), f"{where}.exclude"))
        params = tuple(sorted(dict(entry.get("params") or {}).items()))
        for metric in metrics:
            if metric in skip:
                continue
            for object_type in sorted(catalogue[metric][1]):
                for window in chosen:
                    specs.append(
                        FeatureSpec(
                            name=template.format(metric=metric, window=window),
                            object_type=object_type, metric=metric, function=function,
                            window=window, window_minutes=windows[window], params=params,
                        )
                    )  # fmt: skip

    seen: set[tuple[str, str]] = set()
    for spec in specs:
        key = (spec.object_type, spec.name)
        if key in seen:
            raise SpecError(f"duplicate feature {spec.name!r} for {spec.object_type}")
        seen.add(key)
    feature_set = FeatureSet(
        specs=tuple(sorted(specs, key=lambda s: (s.object_type, s.metric, s.name))),
        windows=windows,
        min_coverage=coverage_floor,
        level_minutes=windows[level_name],
        coverage=dict(doc.get("coverage") or {}),
    )
    _check_coverage(feature_set)
    return feature_set


def matching(feature_set: FeatureSet, rule: Mapping[str, Any]) -> list[FeatureSpec]:
    """Specs selected by one `coverage` entry (a function or list of them, optional metrics)."""
    functions = set(_as_list(rule.get("function", []), "coverage.function"))
    metrics = set(_as_list(rule["metrics"], "coverage.metrics")) if "metrics" in rule else None
    return [
        s for s in feature_set.specs
        if s.function in functions and (metrics is None or s.metric in metrics)
    ]  # fmt: skip


def _check_coverage(feature_set: FeatureSet) -> None:
    for name, rule in feature_set.coverage.items():
        if not matching(feature_set, rule):
            raise SpecError(f"coverage.{name} matches no feature")


def load_features(path: Path = FEATURES_PATH, catalogue_path: Path = CATALOGUE_PATH) -> FeatureSet:
    """Read features.yaml and expand it against docs/METRICS.md."""
    catalogue = parse_catalogue(catalogue_path.read_text(encoding="utf-8"))
    return parse_features(yaml.safe_load(path.read_text(encoding="utf-8")), catalogue)


@cache
def get_features() -> FeatureSet:
    """The shipped feature set, loaded once."""
    return load_features()
