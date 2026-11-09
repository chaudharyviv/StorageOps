"""Scenario engine: build a healthy cluster, layer a scenario's effects on it, derive the rest."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from contracts import MetricRecord, ObjectType, ScenarioLabel
from simulator.cluster import CLUSTER, MINUTES_PER_DAY, MetricSpec, ObjectSpec, build_cluster
from simulator.patterns import Series, pattern_delta
from simulator.scenario import Effect, ScenarioError, ScenarioVariant

SeriesKey = tuple[ObjectType, str, str]

AUTOGROW_TRIGGER = 0.85
AUTOGROW_FACTOR = 1.2


@dataclass(frozen=True)
class SimulationResult:
    """Metric series and, kept apart, the ground-truth labels of one run."""

    cluster: str
    start: datetime
    minutes: int
    series: dict[SeriesKey, Series]
    units: dict[SeriesKey, str]
    labels: list[ScenarioLabel]

    def timestamps(self) -> NDArray[np.datetime64]:
        """Return one UTC timestamp per minute of the run."""
        origin = np.datetime64(self.start.astimezone(UTC).replace(tzinfo=None), "us")
        return origin + np.arange(self.minutes) * np.timedelta64(1, "m")


def _rng(seed: int, *parts: object) -> np.random.Generator:
    """A generator keyed by seed and identity, so adding an object never shifts other series."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    words = [int.from_bytes(digest[i : i + 4], "big") for i in range(0, 16, 4)]
    return np.random.default_rng(np.random.SeedSequence([seed, *words]))


def _clip(values: Series, low: float | None, high: float | None) -> Series:
    return values if low is None and high is None else np.clip(values, low, high)


def _bounded(values: Series, spec: MetricSpec) -> Series:
    return _clip(values, spec.floor, spec.ceiling)


def baseline_series(spec: MetricSpec, minutes: int, rng: np.random.Generator) -> Series:
    """Healthy series: base level, slow trend, daily cycle and white noise."""
    t = np.arange(minutes, dtype=np.float64)
    values = spec.base + spec.trend_per_day * t / MINUTES_PER_DAY
    if spec.diurnal_amplitude:
        values = values + spec.diurnal_amplitude * np.sin(2 * np.pi * t / MINUTES_PER_DAY)
    if spec.noise_sigma:
        values = values + rng.normal(0.0, spec.noise_sigma, minutes)
    return _bounded(values, spec)


def apply_effect(values: Series, effect: Effect, rng: np.random.Generator) -> Series:
    """Apply one effect to a series, either adding to it or scaling it."""
    delta = pattern_delta(effect.pattern, len(values), rng)
    out = values + delta if effect.mode == "add" else values * (1.0 + delta)
    return _clip(out, effect.minimum, effect.maximum)


def _autogrow(demand: Series, initial: float, ceiling: Series) -> Series:
    """Volume size over time: grows 20% whenever usage passes 85%, up to the autogrow maximum."""
    sizes = np.empty(len(demand))
    size = initial
    for i, (need, cap) in enumerate(zip(demand.tolist(), ceiling.tolist(), strict=True)):
        if need > AUTOGROW_TRIGGER * size and size < cap:
            size = min(cap, size * AUTOGROW_FACTOR)
        sizes[i] = size
    return sizes


def _percent(part: Series, whole: Series) -> Series:
    return np.clip(part / np.maximum(whole, 1.0) * 100.0, 0.0, 100.0)


def _derive_aggregate(raw: dict[str, Series]) -> dict[str, tuple[Series, str]]:
    return {
        "space.used_percent": (
            _percent(raw["space.used_bytes"], raw["space.size_bytes"]),
            "percent",
        )
    }


def _derive_volume(raw: dict[str, Series], spec: ObjectSpec) -> dict[str, tuple[Series, str]]:
    demand = raw["space.logical_used_bytes"] / np.maximum(raw["efficiency.ratio"], 1.0)
    size = _autogrow(demand, spec.attrs["initial_size_bytes"], raw["space.autogrow_max_bytes"])
    used = np.minimum(demand, size)
    reserve = spec.attrs["snapshot_reserve_percent"] / 100.0 * size
    snapshot = raw["snapshot.used_bytes"]
    return {
        "space.size_bytes": (size, "bytes"),
        "space.used_bytes": (used, "bytes"),
        "space.used_percent": (_percent(used, size), "percent"),
        "snapshot.to_active_ratio": (snapshot / np.maximum(used, 1.0), "ratio"),
        "snapshot.reserve_used_percent": (snapshot / np.maximum(reserve, 1.0) * 100.0, "percent"),
    }


def _derive(spec: ObjectSpec, raw: dict[str, Series]) -> dict[str, tuple[Series, str]]:
    if spec.object_type == ObjectType.AGGREGATE:
        return _derive_aggregate(raw)
    if spec.object_type == ObjectType.VOLUME:
        return _derive_volume(raw, spec)
    return {}


def _check_topology(objects: list[ObjectSpec]) -> dict[tuple[ObjectType, str], ObjectSpec]:
    by_key: dict[tuple[ObjectType, str], ObjectSpec] = {}
    for obj in objects:
        key = (obj.object_type, obj.object_id)
        if key in by_key:
            raise ScenarioError(f"object {obj.object_type}/{obj.object_id} is defined twice")
        by_key[key] = obj
    return by_key


def _target_series(
    raw: dict[tuple[ObjectType, str], dict[str, Series]], effect: Effect, where: str
) -> dict[str, Series]:
    target = effect.target
    metrics = raw.get((target.object_type, target.object_id))
    if metrics is None:
        raise ScenarioError(f"{where}: no object {target.object_type}/{target.object_id}")
    if target.metric not in metrics:
        raise ScenarioError(
            f"{where}: {target.object_id} has no raw metric {target.metric!r}; "
            f"effects can target: {sorted(metrics)}"
        )
    return metrics


def _make_labels(
    variant: ScenarioVariant, seed: int, minutes: int, ids: set[str]
) -> list[ScenarioLabel]:
    labels = []
    for spec in variant.labels:
        if spec.object_id not in ids:
            raise ScenarioError(f"label object {spec.object_id!r} does not exist in this run")
        end = minutes - 1 if spec.end_minute is None else min(spec.end_minute, minutes - 1)
        labels.append(
            ScenarioLabel(
                label_id=f"{variant.scenario}-{variant.variant}-s{seed}-{spec.object_id}",
                scenario=variant.scenario,
                variant=variant.variant,
                cluster=CLUSTER,
                object_id=spec.object_id,
                start=variant.start + timedelta(minutes=spec.start_minute),
                end=variant.start + timedelta(minutes=end),
            )
        )
    return labels


def simulate(
    variant: ScenarioVariant, seed: int, cluster: tuple[ObjectSpec, ...] | None = None
) -> SimulationResult:
    """Run one scenario variant. The same seed always gives the same result."""
    if seed < 0:
        raise ScenarioError("seed must be >= 0")
    minutes = variant.duration_minutes
    objects = list(cluster if cluster is not None else build_cluster()) + list(
        variant.extra_objects
    )
    specs = _check_topology(objects)

    raw = {
        key: {
            m.name: baseline_series(m, minutes, _rng(seed, "base", key[0], key[1], m.name))
            for m in obj.metrics
        }
        for key, obj in specs.items()
    }
    for index, effect in enumerate(variant.effects):
        metrics = _target_series(raw, effect, f"effect {index}")
        name = effect.target.metric
        metrics[name] = apply_effect(metrics[name], effect, _rng(seed, "effect", index))
    for key, obj in specs.items():
        for m in obj.metrics:
            raw[key][m.name] = _bounded(raw[key][m.name], m)

    series: dict[SeriesKey, Series] = {}
    units: dict[SeriesKey, str] = {}
    for key, obj in specs.items():
        for m in obj.metrics:
            series[(*key, m.name)] = raw[key][m.name]
            units[(*key, m.name)] = m.unit
        for name, (values, unit) in _derive(obj, raw[key]).items():
            series[(*key, name)] = values
            units[(*key, name)] = unit
    for series_key, values in series.items():
        if not np.all(np.isfinite(values)):
            raise ScenarioError(f"series {series_key} produced a non-finite value")

    labels = _make_labels(variant, seed, minutes, {oid for _, oid in specs})
    return SimulationResult(CLUSTER, variant.start, minutes, series, units, labels)


def iter_metric_records(result: SimulationResult) -> Iterator[MetricRecord]:
    """Yield validated MetricRecords, one per series per minute, in time order."""
    for i in range(result.minutes):
        ts = result.start + timedelta(minutes=i)
        for (object_type, object_id, metric), values in result.series.items():
            yield MetricRecord(
                ts=ts,
                cluster=result.cluster,
                object_type=object_type,
                object_id=object_id,
                metric=metric,
                value=float(values[i]),
                unit=result.units[(object_type, object_id, metric)],
            )
