"""Load simulator/topology.yaml: the objects of the fake cluster and how they relate.

The simulator builds its cluster from this, and the mock ONTAP API serves the same relations
from it, so the two cannot disagree. It holds configuration only, never labels.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from contracts import ObjectType

TOPOLOGY_PATH = Path(__file__).parent / "topology.yaml"


class TopologyError(ValueError):
    """The topology file is malformed or refers to an object that does not exist."""


@dataclass(frozen=True)
class VolumeEntry:
    """A volume, the aggregate that hosts it and the QoS policy it uses."""

    id: str
    aggregate: str
    qos_policy: str


@dataclass(frozen=True)
class SnapMirrorEntry:
    """A SnapMirror relationship and the volume it protects."""

    id: str
    source_volume: str


@dataclass(frozen=True)
class QosEntry:
    """A QoS policy and its throughput ceiling."""

    id: str
    max_throughput_iops: int


@dataclass(frozen=True)
class Topology:
    """The whole cluster layout."""

    cluster: str
    svm: str
    dr_svm: str
    aggregates: tuple[str, ...]
    volumes: tuple[VolumeEntry, ...]
    snapmirror: tuple[SnapMirrorEntry, ...]
    qos_policies: tuple[QosEntry, ...]

    def object_ids(self) -> set[tuple[ObjectType, str]]:
        """Every (object type, id) the simulator emits for this topology."""
        return (
            {(ObjectType.AGGREGATE, a) for a in self.aggregates}
            | {(ObjectType.VOLUME, v.id) for v in self.volumes}
            | {(ObjectType.SNAPMIRROR, s.id) for s in self.snapmirror}
            | {(ObjectType.QOS, q.id) for q in self.qos_policies}
        )

    def volume(self, volume_id: str) -> VolumeEntry:
        """Look up one volume entry."""
        return next(v for v in self.volumes if v.id == volume_id)

    def aggregate_of(self, volume_id: str) -> str:
        """Name of the aggregate that hosts a volume."""
        return self.volume(volume_id).aggregate

    def qos_policy_of(self, volume_id: str) -> str:
        """Name of the QoS policy a volume uses."""
        return self.volume(volume_id).qos_policy

    def volumes_on(self, aggregate_id: str) -> list[str]:
        """Ids of the volumes hosted on one aggregate."""
        return [v.id for v in self.volumes if v.aggregate == aggregate_id]

    def volumes_using(self, policy_id: str) -> list[str]:
        """Ids of the volumes that use one QoS policy."""
        return [v.id for v in self.volumes if v.qos_policy == policy_id]

    def source_volume_of(self, snapmirror_id: str) -> str:
        """Id of the volume a SnapMirror relationship protects."""
        return next(s.source_volume for s in self.snapmirror if s.id == snapmirror_id)

    def max_throughput_iops(self, policy_id: str) -> int:
        """Throughput ceiling of one QoS policy."""
        return next(q.max_throughput_iops for q in self.qos_policies if q.id == policy_id)


def _entries(data: Mapping[str, Any], key: str) -> list[Any]:
    items = data.get(key)
    if not isinstance(items, list) or not items:
        raise TopologyError(f"topology: {key!r} must be a non-empty list")
    return items


def _unique(ids: list[str], what: str) -> None:
    if len(set(ids)) != len(ids):
        raise TopologyError(f"topology: duplicate {what} ids in {ids}")


def parse_topology(data: Any) -> Topology:
    """Validate a parsed YAML document; every reference must name an object that exists."""
    if not isinstance(data, Mapping):
        raise TopologyError("topology: expected a mapping at the top level")
    try:
        topo = Topology(
            cluster=str(data["cluster"]),
            svm=str(data["svm"]),
            dr_svm=str(data["dr_svm"]),
            aggregates=tuple(str(a) for a in _entries(data, "aggregates")),
            volumes=tuple(
                VolumeEntry(str(v["id"]), str(v["aggregate"]), str(v["qos_policy"]))
                for v in _entries(data, "volumes")
            ),
            snapmirror=tuple(
                SnapMirrorEntry(str(s["id"]), str(s["source_volume"]))
                for s in _entries(data, "snapmirror")
            ),
            qos_policies=tuple(
                QosEntry(str(q["id"]), int(q["max_throughput_iops"]))
                for q in _entries(data, "qos_policies")
            ),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise TopologyError(f"topology: missing or invalid entry ({err!r})") from None
    _unique(list(topo.aggregates), "aggregate")
    _unique([v.id for v in topo.volumes], "volume")
    _unique([s.id for s in topo.snapmirror], "snapmirror")
    _unique([q.id for q in topo.qos_policies], "qos policy")
    policies = {q.id for q in topo.qos_policies}
    volumes = {v.id for v in topo.volumes}
    for v in topo.volumes:
        if v.aggregate not in topo.aggregates:
            raise TopologyError(f"topology: {v.id} is on unknown aggregate {v.aggregate!r}")
        if v.qos_policy not in policies:
            raise TopologyError(f"topology: {v.id} uses unknown QoS policy {v.qos_policy!r}")
    for s in topo.snapmirror:
        if s.source_volume not in volumes:
            raise TopologyError(f"topology: {s.id} protects unknown volume {s.source_volume!r}")
    return topo


def load_topology(path: Path = TOPOLOGY_PATH) -> Topology:
    """Read and validate a topology file."""
    return parse_topology(yaml.safe_load(path.read_text(encoding="utf-8")))


@cache
def get_topology() -> Topology:
    """The shared topology, loaded once from simulator/topology.yaml."""
    return load_topology()
