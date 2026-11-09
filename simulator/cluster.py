"""The fake cluster: which objects exist and what their healthy telemetry looks like.

Scenarios never live here. This module only describes a healthy cluster; failures are
layered on top by the scenario engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from contracts import ObjectType
from simulator.topology import get_topology

CLUSTER = get_topology().cluster
MINUTES_PER_DAY = 1440
TB = 1e12


@dataclass(frozen=True)
class MetricSpec:
    """Healthy behaviour of one raw metric: base level, slow trend, daily cycle and noise."""

    name: str
    unit: str
    base: float
    trend_per_day: float = 0.0
    diurnal_amplitude: float = 0.0
    noise_sigma: float = 0.0
    floor: float | None = 0.0
    ceiling: float | None = None


@dataclass(frozen=True)
class ObjectSpec:
    """One ONTAP object and the raw metrics it emits."""

    object_type: ObjectType
    object_id: str
    metrics: tuple[MetricSpec, ...]
    attrs: dict[str, float] = field(default_factory=dict)


def _aggregate(object_id: str, used_tb: float) -> ObjectSpec:
    return ObjectSpec(
        ObjectType.AGGREGATE,
        object_id,
        (
            MetricSpec("space.size_bytes", "bytes", 20 * TB),
            MetricSpec(
                "space.used_bytes",
                "bytes",
                used_tb * TB,
                trend_per_day=0.01 * TB,
                noise_sigma=0.002 * TB,
            ),
        ),
    )


def _volume(index: int, object_id: str) -> ObjectSpec:
    """Volume sizes and loads vary by index so the ten volumes are not clones."""
    size = float((index % 4) + 1) * TB
    used = size * (0.50 + 0.04 * (index % 5))
    ratio = 1.6 + 0.1 * (index % 5)
    return ObjectSpec(
        ObjectType.VOLUME,
        object_id,
        (
            MetricSpec(
                "space.logical_used_bytes",
                "bytes",
                used * ratio,
                trend_per_day=0.001 * used * ratio,
                noise_sigma=0.0005 * used * ratio,
            ),
            MetricSpec("efficiency.ratio", "ratio", ratio, noise_sigma=0.01, floor=1.0),
            MetricSpec(
                "snapshot.used_bytes",
                "bytes",
                0.04 * used,
                trend_per_day=0.0005 * used,
                noise_sigma=0.0005 * used,
            ),
            MetricSpec("space.autogrow_max_bytes", "bytes", 2 * size),
            MetricSpec(
                "iops", "iops", 500.0 + 50 * index, diurnal_amplitude=150.0, noise_sigma=30.0
            ),
            MetricSpec(
                "data.change_rate_percent", "percent", 1.5 + 0.1 * (index % 3), noise_sigma=0.2
            ),
            MetricSpec("data.entropy_proxy", "ratio", 0.45, noise_sigma=0.03, ceiling=1.0),
        ),
        attrs={"initial_size_bytes": size, "snapshot_reserve_percent": 10.0},
    )


def _snapmirror(object_id: str) -> ObjectSpec:
    return ObjectSpec(
        ObjectType.SNAPMIRROR,
        object_id,
        (
            MetricSpec("lag_seconds", "seconds", 120.0, diurnal_amplitude=20.0, noise_sigma=15.0),
            MetricSpec("transfer_errors", "count", 0.0),
        ),
    )


def _qos(object_id: str, scale: float) -> ObjectSpec:
    return ObjectSpec(
        ObjectType.QOS,
        object_id,
        (
            MetricSpec(
                "latency.read_p95_ms", "ms", 1.2 * scale, diurnal_amplitude=0.3, noise_sigma=0.15
            ),
            MetricSpec(
                "latency.write_p95_ms", "ms", 1.8 * scale, diurnal_amplitude=0.4, noise_sigma=0.2
            ),
            MetricSpec("iops", "iops", 8000.0 * scale, diurnal_amplitude=2500.0, noise_sigma=300.0),
            MetricSpec(
                "throughput_mbps", "MB/s", 250.0 * scale, diurnal_amplitude=80.0, noise_sigma=10.0
            ),
        ),
    )


def build_cluster() -> tuple[ObjectSpec, ...]:
    """Return the healthy cluster described by simulator/topology.yaml.

    The topology says which objects exist. Sizes and loads come from each object's position
    in its list, so the same topology always gives the same cluster.
    """
    topo = get_topology()
    return (
        *(_aggregate(name, 11.0 + position) for position, name in enumerate(topo.aggregates)),
        *(_volume(position + 1, v.id) for position, v in enumerate(topo.volumes)),
        *(_snapmirror(s.id) for s in topo.snapmirror),
        *(_qos(q.id, 1.0 - 0.2 * position) for position, q in enumerate(topo.qos_policies)),
    )
