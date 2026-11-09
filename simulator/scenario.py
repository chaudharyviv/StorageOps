"""Scenario definitions: load and validate YAML. The engine never hard-codes a scenario."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from contracts import ObjectType, ScenarioType, Variant
from simulator.cluster import MetricSpec, ObjectSpec
from simulator.patterns import PATTERNS, PatternSpec

SCENARIO_DIRS = (
    Path(__file__).parent / "scenarios",
    Path(__file__).parent / "scenarios_heldout",
)
DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)
# Derived metrics (used %, autogrow size, ...) are only worked out for the built-in
# aggregates and volumes, so a scenario may only add objects of these types.
EXTRA_OBJECT_TYPES = (ObjectType.QOS, ObjectType.SNAPMIRROR, ObjectType.TRIDENT_BACKEND)
MODES = ("add", "multiply")


class ScenarioError(ValueError):
    """A scenario file is malformed."""


class Split(StrEnum):
    """Which part of the evaluation a variant belongs to."""

    TRAIN = "train"
    VALIDATION = "validation"
    HELDOUT = "heldout"


@dataclass(frozen=True)
class Target:
    """The one series an effect changes."""

    object_type: ObjectType
    object_id: str
    metric: str


@dataclass(frozen=True)
class Effect:
    """A pattern applied to one series, either added to it or multiplying it."""

    target: Target
    pattern: PatternSpec
    mode: str = "add"
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class LabelSpec:
    """Ground truth for one affected object; end defaults to the end of the run."""

    object_id: str
    start_minute: int
    end_minute: int | None = None


@dataclass(frozen=True)
class ScenarioVariant:
    """One fully resolved variant: parameters already substituted."""

    scenario: ScenarioType
    variant: Variant
    split: Split
    description: str
    start: datetime
    duration_minutes: int
    effects: tuple[Effect, ...]
    labels: tuple[LabelSpec, ...]
    extra_objects: tuple[ObjectSpec, ...] = ()


@dataclass(frozen=True)
class ScenarioDefinition:
    """A scenario file: one failure type with its variants."""

    scenario: ScenarioType
    description: str
    variants: dict[Variant, ScenarioVariant]
    path: Path | None = None

    def variant(self, variant: Variant) -> ScenarioVariant:
        """Return one variant, or raise ScenarioError naming the ones that exist."""
        if variant not in self.variants:
            have = ", ".join(sorted(self.variants))
            raise ScenarioError(f"{self.scenario} has no variant {variant!r}; has: {have}")
        return self.variants[variant]


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _check_keys(
    data: Mapping[str, Any], allowed: Iterable[str], required: Iterable[str], where: str
) -> None:
    allowed_set = set(allowed)
    unknown = sorted(set(data) - allowed_set)
    if unknown:
        raise ScenarioError(f"{where}: unknown keys {unknown}; allowed: {sorted(allowed_set)}")
    missing = sorted(set(required) - set(data))
    if missing:
        raise ScenarioError(f"{where}: missing required keys {missing}")


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool):
        raise ScenarioError(f"{where}: expected a number, got a boolean")
    try:
        return float(value)  # also accepts "2e12", which YAML 1.1 reads as a string
    except (TypeError, ValueError):
        raise ScenarioError(f"{where}: expected a number, got {value!r}") from None


def _minutes(value: Any, where: str, minimum: int = 0) -> int:
    number = _number(value, where)
    if number != int(number) or number < minimum:
        raise ScenarioError(f"{where}: expected a whole number >= {minimum}, got {value!r}")
    return int(number)


def _substitute(value: Any, params: Mapping[str, Any], used: set[str]) -> Any:
    """Replace "$name" strings with the variant's parameter of that name."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in params:
            raise ScenarioError(f"unknown parameter {value!r}; variant defines {sorted(params)}")
        used.add(name)
        return params[name]
    if isinstance(value, Mapping):
        return {k: _substitute(v, params, used) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params, used) for v in value]
    return value


_EFFECT_KEYS = (
    "target pattern mode start_minute duration_minutes magnitude shape width_minutes "
    "period_minutes sigma hold min max"
).split()


def _parse_effect(raw: Any, where: str, duration: int) -> Effect:
    data = _mapping(raw, where)
    _check_keys(data, _EFFECT_KEYS, ["target", "pattern"], where)
    target = _mapping(data["target"], f"{where}.target")
    _check_keys(
        target,
        ["object_type", "object_id", "metric"],
        ["object_type", "object_id", "metric"],
        f"{where}.target",
    )
    try:
        object_type = ObjectType(target["object_type"])
    except ValueError:
        raise ScenarioError(
            f"{where}.target: unknown object_type {target['object_type']!r}"
        ) from None
    kind = str(data["pattern"])
    if kind not in PATTERNS:
        raise ScenarioError(f"{where}: unknown pattern {kind!r}; expected one of {PATTERNS}")
    mode = str(data.get("mode", "add"))
    if mode not in MODES:
        raise ScenarioError(f"{where}: mode must be one of {MODES}, got {mode!r}")
    if kind == "noise":
        if _number(data.get("sigma", 0), f"{where}.sigma") <= 0:
            raise ScenarioError(f"{where}: noise needs sigma > 0")
    elif "magnitude" not in data:
        raise ScenarioError(f"{where}: {kind} needs a magnitude")
    start = _minutes(data.get("start_minute", 0), f"{where}.start_minute")
    if start >= duration:
        raise ScenarioError(
            f"{where}: start_minute {start} is not inside the {duration}-minute run"
        )
    spec = PatternSpec(
        kind=kind,
        start_minute=start,
        duration_minutes=(
            _minutes(data["duration_minutes"], f"{where}.duration_minutes", 1)
            if data.get("duration_minutes") is not None
            else None
        ),
        magnitude=_number(data.get("magnitude", 0), f"{where}.magnitude"),
        shape=_number(data.get("shape", 4.0), f"{where}.shape"),
        width_minutes=_minutes(data.get("width_minutes", 30), f"{where}.width_minutes", 1),
        period_minutes=(
            _minutes(data["period_minutes"], f"{where}.period_minutes", 1)
            if data.get("period_minutes") is not None
            else None
        ),
        sigma=_number(data.get("sigma", 0), f"{where}.sigma"),
        hold=bool(data.get("hold", True)),
    )
    if spec.shape == 0:
        raise ScenarioError(f"{where}: shape must not be 0")
    return Effect(
        target=Target(object_type, str(target["object_id"]), str(target["metric"])),
        pattern=spec,
        mode=mode,
        minimum=_number(data["min"], f"{where}.min") if data.get("min") is not None else None,
        maximum=_number(data["max"], f"{where}.max") if data.get("max") is not None else None,
    )


def _parse_label(raw: Any, where: str, duration: int) -> LabelSpec:
    data = _mapping(raw, where)
    _check_keys(
        data, ["object_id", "start_minute", "end_minute"], ["object_id", "start_minute"], where
    )
    start = _minutes(data["start_minute"], f"{where}.start_minute")
    end = (
        _minutes(data["end_minute"], f"{where}.end_minute")
        if data.get("end_minute") is not None
        else None
    )
    if start >= duration or (end is not None and not start <= end <= duration):
        raise ScenarioError(f"{where}: label window must sit inside the {duration}-minute run")
    return LabelSpec(str(data["object_id"]), start, end)


_METRIC_KEYS = "name unit base trend_per_day diurnal_amplitude noise_sigma floor ceiling".split()


def _parse_extra_object(raw: Any, where: str) -> ObjectSpec:
    data = _mapping(raw, where)
    _check_keys(
        data,
        ["object_type", "object_id", "metrics"],
        ["object_type", "object_id", "metrics"],
        where,
    )
    try:
        object_type = ObjectType(data["object_type"])
    except ValueError:
        raise ScenarioError(f"{where}: unknown object_type {data['object_type']!r}") from None
    if object_type not in EXTRA_OBJECT_TYPES:
        allowed = [t.value for t in EXTRA_OBJECT_TYPES]
        raise ScenarioError(f"{where}: extra objects must be one of {allowed}")
    metrics = []
    for i, item in enumerate(data["metrics"] or []):
        mwhere = f"{where}.metrics[{i}]"
        spec = _mapping(item, mwhere)
        _check_keys(spec, _METRIC_KEYS, ["name", "unit", "base"], mwhere)
        kwargs: dict[str, Any] = {
            key: _number(spec[key], f"{mwhere}.{key}")
            for key in ("base", "trend_per_day", "diurnal_amplitude", "noise_sigma")
            if key in spec
        }
        for key in ("floor", "ceiling"):
            if key in spec:
                kwargs[key] = None if spec[key] is None else _number(spec[key], f"{mwhere}.{key}")
        metrics.append(MetricSpec(str(spec["name"]), str(spec["unit"]), **kwargs))
    if not metrics:
        raise ScenarioError(f"{where}: an extra object needs at least one metric")
    return ObjectSpec(object_type, str(data["object_id"]), tuple(metrics))


def _parse_start(value: Any, where: str) -> datetime:
    if value is None:
        return DEFAULT_START
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        raise ScenarioError(f"{where}: cannot read {value!r} as an ISO timestamp") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_variant(
    name: str, raw: Any, scenario: ScenarioType, doc: Mapping[str, Any], where: str
) -> ScenarioVariant:
    block = _mapping(raw, where)
    _check_keys(block, ["split", "params"], ["split"], where)
    try:
        variant, split = Variant(name), Split(block["split"])
    except ValueError as err:
        raise ScenarioError(f"{where}: {err}") from None
    params = _mapping(block.get("params") or {}, f"{where}.params")
    used: set[str] = set()
    resolved = _substitute(
        {k: doc.get(k) or [] for k in ("effects", "labels", "extra_objects")}, params, used
    )
    if unused := sorted(set(params) - used):
        raise ScenarioError(f"{where}: parameters {unused} are never used")
    duration = _minutes(doc["duration_minutes"], "duration_minutes", 1)
    effects = tuple(
        _parse_effect(e, f"{where}.effects[{i}]", duration)
        for i, e in enumerate(resolved["effects"])
    )
    labels = tuple(
        _parse_label(lab, f"{where}.labels[{i}]", duration)
        for i, lab in enumerate(resolved["labels"])
    )
    if not labels:
        raise ScenarioError(f"{where}: a scenario needs at least one label")
    extras = tuple(
        _parse_extra_object(x, f"{where}.extra_objects[{i}]")
        for i, x in enumerate(resolved["extra_objects"])
    )
    return ScenarioVariant(
        scenario=scenario,
        variant=variant,
        split=split,
        description=str(doc.get("description", "")).strip(),
        start=_parse_start(doc.get("start"), "start"),
        duration_minutes=duration,
        effects=effects,
        labels=labels,
        extra_objects=extras,
    )


def parse_scenario(doc: Any, path: Path | None = None) -> ScenarioDefinition:
    """Validate a parsed YAML document and resolve every variant."""
    where = path.name if path else "scenario"
    data = _mapping(doc, where)
    _check_keys(
        data,
        [
            "scenario",
            "description",
            "start",
            "duration_minutes",
            "extra_objects",
            "effects",
            "labels",
            "variants",
        ],
        ["scenario", "duration_minutes", "effects", "labels", "variants"],
        where,
    )
    try:
        scenario = ScenarioType(data["scenario"])
    except ValueError:
        raise ScenarioError(f"{where}: unknown scenario type {data['scenario']!r}") from None
    variants_raw = _mapping(data["variants"], f"{where}.variants")
    if not variants_raw:
        raise ScenarioError(f"{where}: no variants defined")
    if bad := sorted(set(variants_raw) - {v.value for v in Variant}):
        raise ScenarioError(f"{where}: unknown variants {bad}; expected mild, moderate, severe")
    variants = {
        Variant(name): _parse_variant(name, raw, scenario, data, f"{where}.variants.{name}")
        for name, raw in variants_raw.items()
    }
    return ScenarioDefinition(scenario, str(data.get("description", "")).strip(), variants, path)


def load_scenario(path: Path) -> ScenarioDefinition:
    """Load and validate one scenario YAML file."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ScenarioError(f"{path.name}: invalid YAML: {err}") from None
    return parse_scenario(doc, path)


def scenario_files(dirs: Iterable[Path] = SCENARIO_DIRS) -> list[Path]:
    """List every scenario YAML file in the given directories."""
    return sorted(p for d in dirs if d.is_dir() for p in d.glob("*.yaml"))


def find_scenario(name: str, dirs: Iterable[Path] = SCENARIO_DIRS) -> ScenarioDefinition:
    """Find `<name>.yaml` in the scenario directories and load it."""
    for directory in dirs:
        candidate = directory / f"{name}.yaml"
        if candidate.is_file():
            return load_scenario(candidate)
    searched = ", ".join(str(d) for d in dirs)
    raise ScenarioError(f"no scenario file {name}.yaml in: {searched}")
