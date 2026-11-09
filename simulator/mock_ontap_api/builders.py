"""Build ONTAP-shaped records from a replay snapshot.

Only fields in the ONTAP 9.19.1 REST reference are used (see docs/mock_ontap_subset.md).
Simulator metrics with no documented field are not served.
"""

from __future__ import annotations

from typing import Any

from contracts import ObjectType
from simulator.cluster import build_cluster
from simulator.engine import AUTOGROW_TRIGGER
from simulator.mock_ontap_api.identity import QOS_PGID_BASE, uuid_for
from simulator.mock_ontap_api.replay import Snapshot
from simulator.topology import get_topology

COLLECTION_PATHS = {
    "cluster": "/api/cluster",
    "aggregates": "/api/storage/aggregates",
    "volumes": "/api/storage/volumes",
    "snapmirror": "/api/snapmirror/relationships",
    "qos": "/api/storage/qos/policies",
}

# Properties the reference lists as expensive for each endpoint; returned only on request.
VOLUME_EXPENSIVE = (
    "autosize.*",
    "efficiency.*",
    "qos.*",
    "metric.*",
    "space.logical_space.*",
    "space.snapshot.*",
    "space.physical_used",
    "space.physical_used_percent",
    "space.percent_used",
)
AGGREGATE_EXPENSIVE = (
    "metric.*",
    "space.block_storage.inactive_user_data",
    "space.block_storage.inactive_user_data_percent",
    "space.footprint",
    "is_spare_low",
    "statistics.*",
)
SNAPMIRROR_EXPENSIVE = (
    "source.consistency_group_volumes.name",
    "destination.consistency_group_volumes.name",
    "svmdr_volumes.name",
)
NO_EXPENSIVE: tuple[str, ...] = ()

SNAPSHOT_RESERVE_PERCENT = {
    o.object_id: int(o.attrs["snapshot_reserve_percent"])
    for o in build_cluster()
    if o.object_type == ObjectType.VOLUME
}
# Our own wording. The field structure (arguments, code, message) follows the reference; the
# code is a mock identifier, not an ONTAP error code.
UNHEALTHY_CODE = "MOCK-SM-0001"
UNHEALTHY_MESSAGE = "The latest SnapMirror update did not finish; transfer errors were recorded."


def _int(value: float) -> int:
    return int(round(value))


def sample_time(snapshot: Snapshot) -> str:
    """Timestamp text in the format the reference shows, e.g. 2017-01-25 11:20:13 +0000."""
    return snapshot.ts.strftime("%Y-%m-%d %H:%M:%S +0000")


def iso_duration(seconds: float) -> str:
    """Whole seconds as an ISO-8601 duration such as PT8H35M42S or P1DT2H."""
    total = max(int(round(seconds)), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    time_part = "".join(f"{n}{u}" for n, u in ((hours, "H"), (minutes, "M"), (secs, "S")) if n)
    if not days and not time_part:
        return "PT0S"
    return "P" + (f"{days}D" if days else "") + (f"T{time_part}" if time_part else "")


def build_cluster_record(snapshot: Snapshot) -> dict[str, Any]:
    """The GET /cluster object (a single object, not a collection)."""
    return {
        "_links": {"self": {"href": COLLECTION_PATHS["cluster"]}},
        "name": snapshot.cluster,
        "uuid": uuid_for("cluster", snapshot.cluster),
        "version": {
            "full": "NetApp Release 9.19.1 (mock)",
            "generation": 9,
            "major": 19,
            "minor": 1,
        },
    }


def build_aggregates(snapshot: Snapshot) -> list[dict[str, Any]]:
    """Aggregate records with block-storage space."""
    topo = get_topology()
    volumes = snapshot.objects("volume")
    records = []
    for name in snapshot.objects("aggregate"):
        m = snapshot.values[("aggregate", name)]
        size, used = _int(m["space.size_bytes"]), _int(m["space.used_bytes"])
        records.append(
            {
                "uuid": uuid_for("aggregate", name),
                "name": name,
                "space": {
                    "block_storage": {
                        "size": size,
                        "used": used,
                        "available": size - used,
                        "used_percent": _int(m["space.used_percent"]),
                    }
                },
                "volume-count": len([v for v in topo.volumes_on(name) if v in volumes]),
            }
        )
    return records


def _volume_record(snapshot: Snapshot, name: str) -> dict[str, Any]:
    topo = get_topology()
    m = snapshot.values[("volume", name)]
    size, used = _int(m["space.size_bytes"]), _int(m["space.used_bytes"])
    snapshot_used = _int(m["snapshot.used_bytes"])
    reserve_percent = SNAPSHOT_RESERVE_PERCENT[name]
    aggregate = topo.aggregate_of(name)
    policy = topo.qos_policy_of(name)
    return {
        "uuid": uuid_for("volume", name),
        "name": name,
        "state": "online",
        "type": "rw",
        "size": size,
        "svm": {"name": topo.svm, "uuid": uuid_for("svm", topo.svm)},
        "aggregates": [{"name": aggregate, "uuid": uuid_for("aggregate", aggregate)}],
        "space": {
            "size": size,
            "used": used,
            "available": size - used,
            "percent_used": _int(m["space.used_percent"]),
            "physical_used": used,
            "physical_used_percent": _int(m["space.used_percent"]),
            "logical_space": {"used": _int(m["space.logical_used_bytes"])},
            "snapshot": {
                "used": snapshot_used,
                "reserve_percent": reserve_percent,
                "reserve_size": _int(size * reserve_percent / 100),
                "space_used_percent": _int(m["snapshot.reserve_used_percent"]),
            },
        },
        "autosize": {
            "mode": "grow",
            "maximum": _int(m["space.autogrow_max_bytes"]),
            "grow_threshold": _int(AUTOGROW_TRIGGER * 100),
        },
        "efficiency": {"ratio": round(m["efficiency.ratio"], 2)},
        "qos": {"policy": {"name": policy, "uuid": uuid_for("qos", policy)}},
        "metric": {
            "status": "ok",
            "timestamp": sample_time(snapshot),
            "iops": {"total": _int(m["iops"])},
        },
    }


def build_volumes(snapshot: Snapshot) -> list[dict[str, Any]]:
    """Volume records, including the expensive properties (the caller prunes them)."""
    return [_volume_record(snapshot, name) for name in snapshot.objects("volume")]


def _snapmirror_record(snapshot: Snapshot, name: str) -> dict[str, Any]:
    topo = get_topology()
    m = snapshot.values[("snapmirror", name)]
    volume = topo.source_volume_of(name)
    healthy = m["transfer_errors"] <= 0
    record: dict[str, Any] = {
        "uuid": uuid_for("snapmirror", name),
        "source": {"path": f"{topo.svm}:{volume}", "svm": {"name": topo.svm}},
        "destination": {
            "path": f"{topo.dr_svm}:{volume}_dr",
            "svm": {"name": topo.dr_svm},
        },
        "state": "snapmirrored",
        "group_type": "none",
        "healthy": healthy,
        "lag_time": iso_duration(m["lag_seconds"]),
        "policy": {
            "name": "Asynchronous",
            "type": "async",
            "uuid": uuid_for("snapmirror-policy", "Asynchronous"),
        },
    }
    if not healthy:
        record["unhealthy_reason"] = [
            {"arguments": [], "code": UNHEALTHY_CODE, "message": UNHEALTHY_MESSAGE}
        ]
    return record


def build_snapmirror(snapshot: Snapshot) -> list[dict[str, Any]]:
    """SnapMirror relationship records."""
    return [_snapmirror_record(snapshot, n) for n in snapshot.objects("snapmirror")]


def build_qos_policies(snapshot: Snapshot) -> list[dict[str, Any]]:
    """QoS policy records (configuration only; the reference documents no live metrics here)."""
    topo = get_topology()
    volumes = snapshot.objects("volume")
    records = []
    for index, name in enumerate(snapshot.objects("qos")):
        records.append(
            {
                "uuid": uuid_for("qos", name),
                "name": name,
                "scope": "svm",
                "pgid": QOS_PGID_BASE + index,
                "object_count": len([v for v in topo.volumes_using(name) if v in volumes]),
                "svm": {
                    "name": topo.svm,
                    "uuid": uuid_for("svm", topo.svm),
                },
                "fixed": {
                    "capacity_shared": False,
                    "max_throughput_iops": topo.max_throughput_iops(name),
                },
            }
        )
    return records
