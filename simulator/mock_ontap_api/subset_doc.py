"""Generate docs/mock_ontap_subset.md: every endpoint and field the mock serves.

Regenerate with `python -m simulator.mock_ontap_api.subset_doc` after changing a builder.
A test fails if the committed file differs from this output, if a served field is missing
from the ONTAP documented-field lists, or if a field has no documented simulator source.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts import ScenarioType, Variant
from simulator.catalogue import emitted_metrics
from simulator.engine import simulate
from simulator.mock_ontap_api import builders
from simulator.mock_ontap_api.fields import is_expensive
from simulator.mock_ontap_api.replay import Snapshot
from simulator.scenario import DEFAULT_START, LabelSpec, ScenarioVariant, Split

REPO = Path(__file__).resolve().parents[2]
DOC_PATH = REPO / "docs" / "mock_ontap_subset.md"
DOCUMENTED_DIR = REPO / "tests" / "fixtures" / "ontap" / "documented_fields"
DOC_BASE = "https://docs.netapp.com/us-en/ontap-restapi/"
ONTAP_RELEASE = "9.19.1"

SOURCES: dict[str, dict[str, str]] = {
    "cluster": {
        "_links.self.href": "constant",
        "name": "`cluster` column of the metrics file",
        "uuid": "derived from the cluster name",
        "version.full": "constant (mock)",
        "version.generation": "constant",
        "version.major": "constant",
        "version.minor": "constant",
    },
    "aggregates": {
        "uuid": "derived from the aggregate name",
        "name": "object id",
        "space.block_storage.size": "`space.size_bytes`",
        "space.block_storage.used": "`space.used_bytes`",
        "space.block_storage.available": "`space.size_bytes` minus `space.used_bytes`",
        "space.block_storage.used_percent": "`space.used_percent`, rounded",
        "volume-count": "`simulator/topology.yaml` (volumes on the aggregate)",
    },
    "volumes": {
        "uuid": "derived from the volume name",
        "name": "object id",
        "state": "constant",
        "type": "constant",
        "size": "`space.size_bytes`",
        "svm.name": "`simulator/topology.yaml`",
        "svm.uuid": "`simulator/topology.yaml`",
        "aggregates[].name": "`simulator/topology.yaml`",
        "aggregates[].uuid": "`simulator/topology.yaml`",
        "space.size": "`space.size_bytes`",
        "space.used": "`space.used_bytes` (physical; see Unclear points)",
        "space.available": "`space.size_bytes` minus `space.used_bytes`",
        "space.percent_used": "`space.used_percent`, rounded",
        "space.physical_used": "`space.used_bytes`",
        "space.physical_used_percent": "`space.used_percent`, rounded",
        "space.logical_space.used": "`space.logical_used_bytes`",
        "space.snapshot.used": "`snapshot.used_bytes`",
        "space.snapshot.reserve_percent": "simulator cluster configuration",
        "space.snapshot.reserve_size": "reserve percent of `space.size_bytes`",
        "space.snapshot.space_used_percent": "`snapshot.reserve_used_percent`, rounded",
        "autosize.mode": "constant (`grow`)",
        "autosize.maximum": "`space.autogrow_max_bytes`",
        "autosize.grow_threshold": "simulator autogrow trigger (85)",
        "efficiency.ratio": "`efficiency.ratio`, 2 decimals",
        "qos.policy.name": "`simulator/topology.yaml`",
        "qos.policy.uuid": "`simulator/topology.yaml`",
        "metric.status": "constant (`ok`)",
        "metric.timestamp": "sample time",
        "metric.iops.total": "`iops`",
    },
    "snapmirror": {
        "uuid": "derived from the relationship id",
        "source.path": "`simulator/topology.yaml`",
        "source.svm.name": "`simulator/topology.yaml`",
        "destination.path": "`simulator/topology.yaml`",
        "destination.svm.name": "`simulator/topology.yaml`",
        "state": "constant (`snapmirrored`)",
        "group_type": "constant (`none`)",
        "healthy": "false when `transfer_errors` is above 0",
        "lag_time": "`lag_seconds` as an ISO-8601 duration",
        "policy.name": "constant",
        "policy.type": "constant (`async`)",
        "policy.uuid": "derived from the policy name",
        "unhealthy_reason[].arguments": "constant; present only when `transfer_errors` is above 0",
        "unhealthy_reason[].code": "constant, from the reference's example",
        "unhealthy_reason[].message": "constant, from the reference's example",
    },
    "qos": {
        "uuid": "derived from the policy name",
        "name": "object id",
        "scope": "constant (`svm`)",
        "pgid": "constant base plus position",
        "object_count": "`simulator/topology.yaml` (volumes using the policy)",
        "svm.name": "`simulator/topology.yaml`",
        "svm.uuid": "`simulator/topology.yaml`",
        "fixed.capacity_shared": "constant",
        "fixed.max_throughput_iops": "`simulator/topology.yaml` (synthetic ceiling)",
    },
}

UNSERVED_REASONS = {
    "data.change_rate_percent": "No documented volume field for a change-rate percentage.",
    "data.entropy_proxy": (
        "No documented volume field; entropy appears only inside anti-ransomware peak statistics."
    ),
    "latency.read_p95_ms": "The QoS policies endpoint documents configuration only.",
    "latency.write_p95_ms": "The QoS policies endpoint documents configuration only.",
    "snapshot.to_active_ratio": (
        "Derived ratio; no documented field. "
        "Compute it from `space.snapshot.used` and `space.used`."
    ),
    "throughput_mbps": "The QoS policies endpoint documents configuration only.",
}


# Where a documented field's type or unit differs from the simulator metric that feeds it.
# (metric, simulator unit, documented type, conversion, evidence on the reference page)
Conversion = tuple[str, str, str, str, str]
CONVERSIONS: dict[str, Conversion] = {
    "snapmirror: lag_time": (
        "lag_seconds", "seconds", "string",
        "whole seconds as an ISO-8601 duration, e.g. 120 -> PT2M, 93784 -> P1DT2H3M4S",
        "type and description only; the format comes from examples (PT8H35M42S, P2DT, PT10H)",
    ),
    "snapmirror: healthy": (
        "transfer_errors", "count", "boolean",
        "false when the error count is above 0, otherwise true",
        "type and description only (\"Is the relationship healthy?\")",
    ),
    "all byte fields": (
        "*_bytes", "bytes", "integer",
        "float rounded to a whole number of bytes",
        "descriptions say \"in bytes\"",
    ),
    "all percent fields": (
        "*_percent", "percent", "integer",
        "float rounded to a whole percent",
        "descriptions say \"as a percent\" or \"percentage\"",
    ),
    "volumes: efficiency.ratio": (
        "efficiency.ratio", "ratio", "number",
        "float rounded to 2 decimals",
        "documented as number",
    ),
    "volumes: metric.iops.total": (
        "iops", "iops", "integer",
        "float rounded to a whole number",
        "documented as integer",
    ),
    "volumes: metric.timestamp": (
        "(sample time)", "UTC time", "string",
        "formatted as YYYY-MM-DD HH:MM:SS +0000",
        "examples only (\"2017-01-25 11:20:13 +0000\")",
    ),
}  # fmt: skip

# Not served today, but their units differ from the documented ones, so serving them later
# needs a conversion: latency is documented in microseconds, throughput in bytes per second.
UNIT_MISMATCH_IF_SERVED = {
    "latency.read_p95_ms": "simulator ms; the reference says latency is in microseconds",
    "latency.write_p95_ms": "simulator ms; the reference says latency is in microseconds",
    "throughput_mbps": "simulator MB/s; the reference says throughput is bytes per second",
}


@dataclass(frozen=True)
class Endpoint:
    """One served endpoint and where its documentation lives."""

    key: str
    path: str
    page: str
    documented: str
    expensive: tuple[str, ...]
    collection: bool


_B = builders
ENDPOINTS = (
    Endpoint("cluster", "/api/cluster", "get-cluster.html", "cluster.txt", _B.NO_EXPENSIVE, False),
    Endpoint(
        "aggregates", "/api/storage/aggregates", "get-storage-aggregates.html",
        "aggregates.txt", _B.AGGREGATE_EXPENSIVE, True,
    ),
    Endpoint(
        "volumes", "/api/storage/volumes", "get-storage-volumes.html",
        "volumes.txt", _B.VOLUME_EXPENSIVE, True,
    ),
    Endpoint(
        "snapmirror", "/api/snapmirror/relationships", "get-snapmirror-relationships.html",
        "snapmirror_relationships.txt", _B.SNAPMIRROR_EXPENSIVE, True,
    ),
    Endpoint(
        "qos", "/api/storage/qos/policies", "get-storage-qos-policies.html",
        "qos_policies.txt", _B.NO_EXPENSIVE, True,
    ),
)  # fmt: skip


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf paths of a record; '[]' marks a list of objects; scalar lists are leaves."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            out.update(flatten(child, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        for item in value:
            out.update(flatten(item, f"{prefix}[]"))
    else:
        out[prefix] = value
    return out


def documented_types(endpoint: Endpoint) -> dict[str, str]:
    """Documented path -> type for an endpoint, read from the committed field lists."""
    types = {}
    for line in (DOCUMENTED_DIR / endpoint.documented).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            path, kind = line.split("\t")
            types[path] = kind
    return types


def probe_snapshot(transfer_errors: float = 0.0) -> Snapshot:
    """A snapshot of the healthy cluster's first minute, built without a database."""
    variant = ScenarioVariant(
        scenario=ScenarioType.AGGREGATE_FILLING, variant=Variant.MILD, split=Split.TRAIN,
        description="doc probe", start=DEFAULT_START, duration_minutes=2, effects=(),
        labels=(LabelSpec("aggr1", 0),),
    )  # fmt: skip
    result = simulate(variant, seed=0)
    values: dict[tuple[str, str], dict[str, float]] = {}
    for (object_type, object_id, metric), series in result.series.items():
        values.setdefault((object_type.value, object_id), {})[metric] = float(series[0])
    for key, metrics in values.items():
        if key[0] == "snapmirror":
            metrics["transfer_errors"] = transfer_errors
    return Snapshot(result.start, result.cluster, values)


def served_records(key: str) -> list[dict[str, Any]]:
    """Every record the builders produce for an endpoint, healthy and unhealthy variants."""
    healthy, unhealthy = probe_snapshot(), probe_snapshot(transfer_errors=1.0)
    if key == "cluster":
        return [builders.build_cluster_record(healthy)]
    build = {
        "aggregates": builders.build_aggregates,
        "volumes": builders.build_volumes,
        "snapmirror": builders.build_snapmirror,
        "qos": builders.build_qos_policies,
    }[key]
    return build(healthy) + build(unhealthy)


def served_paths(key: str) -> dict[str, Any]:
    """Leaf path -> an example value, over all records an endpoint can return."""
    paths: dict[str, Any] = {}
    for record in served_records(key):
        for path, value in flatten(record).items():
            paths.setdefault(path, value)
    return paths


def unserved_metrics() -> list[str]:
    """Simulator metrics that no served field draws on."""
    text = " ".join(s for fields in SOURCES.values() for s in fields.values())
    return sorted(m for m in emitted_metrics() if f"`{m}`" not in text)


def render() -> str:
    """The full markdown document."""
    lines = [
        "# Mock ONTAP API: served subset",
        "",
        f"The mock follows the NetApp ONTAP **{ONTAP_RELEASE}** REST API reference "
        "(read 2026-11-09). It serves read-only GET requests over a simulator metrics file, "
        "using only field names that appear in the reference pages below. All data is synthetic.",
        "",
        "This file is generated by `python -m simulator.mock_ontap_api.subset_doc`; do not edit it "
        "by hand. A test fails if it drifts from the code.",
        "",
        "## Behaviour",
        "",
        "- **Read-only.** Only GET is served. POST, PUT, PATCH and DELETE return 405 in ONTAP's "
        '`{"error": {"code", "message"}}` envelope.',
        "- **Replay time.** The `MOCK_NOW` environment variable (ISO-8601, UTC if no zone) sets "
        "the replay time, and `?mock_now=` overrides it per request. Responses show the latest "
        "sample at or before that time; with neither set, the last sample in the file. A time "
        "before the first sample returns 404. `mock_now` is not an ONTAP parameter.",
        "- **`fields`.** Supported as in the reference: a comma-separated or repeated list of "
        "paths, `*` for the default fields, `**` for everything. Properties the reference lists "
        "as expensive are returned only when requested; they are marked below.",
        "- **Shape.** Collections return `_links`, `num_records` and `records`. `GET /api/cluster` "
        "returns a single object, as the reference documents. Per-record `_links` are left out "
        "because the instance endpoints they would point to are not served.",
        "- **Not supported.** Every other ONTAP query parameter (filters, `max_records`, "
        "`return_records`, `order_by`, ...) is ignored.",
        "- **Labels.** The mock opens the metrics file only; it never opens the labels file.",
        "",
    ]
    for endpoint in ENDPOINTS:
        types = documented_types(endpoint)
        paths = served_paths(endpoint.key)
        shape = "collection (`records`)" if endpoint.collection else "single object"
        lines += [
            f"## GET {endpoint.path}",
            "",
            f"- Reference: <{DOC_BASE}{endpoint.page}> "
            f"(the `{DOC_BASE}ontap/{endpoint.page}` form redirects here)",
            f"- Response: {shape}",
            "",
            "| Field | Documented type | Returned | Source |",
            "| --- | --- | --- | --- |",
        ]
        for path in sorted(paths):
            plain = path.replace("[]", "")
            expensive = is_expensive(plain, endpoint.expensive)
            lines.append(
                f"| `{path}` | {types.get(path, '?')} | {'on request' if expensive else 'default'} "
                f"| {SOURCES[endpoint.key].get(path, '?')} |"
            )
        lines.append("")
    lines += [
        "## Type and unit conversions",
        "",
        "Where a documented field's type or unit differs from the simulator metric behind it, "
        "the mock converts. These conversions are tested.",
        "",
        "| Field | Simulator metric | Simulator unit | Documented type | Conversion | "
        "Evidence on the reference page |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for field, (metric, unit, kind, how, evidence) in CONVERSIONS.items():
        lines.append(f"| {field} | `{metric}` | {unit} | {kind} | {how} | {evidence} |")
    lines += ["", "Not served, but would need a unit conversion if it were:", ""]
    lines += [f"- `{m}`: {why}" for m, why in UNIT_MISMATCH_IF_SERVED.items()]
    lines.append("")
    lines += ["## Simulator metrics with no served field", "", "| Metric | Why |", "| --- | --- |"]
    lines += [f"| `{m}` | {UNSERVED_REASONS.get(m, '?')} |" for m in unserved_metrics()]
    lines.append(
        "| `iops` (QoS policies only) | Volume `iops` is served as `metric.iops.total`; "
        "the QoS policies endpoint documents configuration only. |"
    )
    lines += [
        "",
        "## Unclear points in the reference",
        "",
        "- `metric.duration` is documented as an ISO-8601 duration with a list of allowed values, "
        "but the list is absent from the page text; only `PT15S` appears, in an example. "
        "The mock omits `metric.duration` rather than state a sampling window it cannot honour "
        "(simulator samples are one minute apart).",
        '- `space.used` is described as virtual space "before storage efficiency", while '
        "`space.physical_used` is the physical figure. The simulator has one used figure, "
        "after efficiency, so both are served from `space.used_bytes`.",
        "- `state` for aggregates is a plain string with no listed values, so it is not served. "
        "Volume `state` is served as `online`, a value named in the volume `state` description.",
        "- The reference documents no per-object metrics endpoints on these pages; performance "
        "numbers appear as the embedded `metric` object (volumes, aggregates, cluster). "
        "Separate metrics endpoints were not checked and are not served.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write the document; return a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m simulator.mock_ontap_api.subset_doc")
    parser.add_argument("--out", type=Path, default=DOC_PATH)
    args = parser.parse_args(argv)
    args.out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
