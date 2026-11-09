"""Generate docs/FEATURES.md from features.yaml.

Regenerate with `python -m features.catalogue` after changing features.yaml or the metric
catalogue. A test fails if the committed file differs from this output.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from features.functions import FUNCTIONS
from features.spec import FEATURES_PATH, FeatureSet, FeatureSpec, get_features, matching

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "FEATURES.md"

# Documented column facts for the features table; a test compares them with the real schema.
TABLE_COLUMNS = (
    ("ts", "TIMESTAMP WITH TIME ZONE", "NO", "Evaluation time (UTC); uses samples at or before it"),
    ("object_type", "VARCHAR", "NO", "One of the `ObjectType` values"),
    ("object_id", "VARCHAR", "NO", ""),
    ("feature_name", "VARCHAR", "NO", "From features.yaml, e.g. `space.used_percent__slope_6h`"),
    ("value", "DOUBLE", "YES", "Null means not computable here (too little data in the window)"),
)  # fmt: skip
TABLE_KEY = ("object_type", "object_id", "feature_name", "ts")


def _params(spec: FeatureSpec) -> str:
    return ", ".join(f"{k}={v}" for k, v in spec.params) or ""


def _templates(doc: dict[str, Any]) -> list[str]:
    rows = []
    for entry in doc.get("features", []):
        windows = entry.get("windows", entry.get("window", ""))
        metrics = entry.get("metrics", "")
        metrics = metrics if isinstance(metrics, str) else ", ".join(f"`{m}`" for m in metrics)
        skip = ", ".join(f"`{m}`" for m in entry.get("exclude", [])) or ""
        params = ", ".join(f"{k}={v}" for k, v in (entry.get("params") or {}).items())
        windows = windows if isinstance(windows, str) else ", ".join(windows)
        name, function = entry["name"], entry["function"]
        rows.append(f"| `{name}` | {metrics} | {skip} | `{function}` | {windows} | {params} |")
    return rows


def render(feature_set: FeatureSet | None = None, doc: dict[str, Any] | None = None) -> str:
    """The full markdown document."""
    feature_set = feature_set or get_features()
    doc = doc or yaml.safe_load(FEATURES_PATH.read_text(encoding="utf-8"))
    lines = [
        "# Features",
        "",
        "Features computed from `clean_metrics` for training and serving. One implementation "
        "serves both: `compute_features_range` (training) and `compute_features_at` (serving) "
        "run the same code, so they give identical values.",
        "",
        "This file is generated from `features/features.yaml` by `python -m features.catalogue`; "
        "do not edit it by hand. A test fails if it drifts from the code.",
        "",
        "## Rules",
        "",
        "- **No future data.** A feature at time `t` uses only samples at or before `t`. A series "
        "has no row at `t` until it has a sample.",
        "- **Windows.** A window of `w` minutes ending at `t` covers the samples at minutes "
        "`t-w+1` to `t`. `t` is floored to the whole minute.",
        f"- **Missing data.** A window needs at least {feature_set.min_coverage:.0%} of its "
        "minutes present, or its feature is null (it never raises). `std` and `slope` also "
        "need two samples. The first hours of a series therefore have null long-window features.",
        "- **Null, not NaN.** A feature that cannot be computed is stored as SQL NULL.",
        "- **One cluster.** The store must hold a single cluster; mixed clusters are an error.",
        "- **Reads `clean_metrics` only.** Not the rows validation rejected, and never the labels.",
        "",
        "## Windows",
        "",
        "| Window | Minutes |",
        "| --- | --- |",
    ]
    lines += [f"| `{name}` | {minutes} |" for name, minutes in feature_set.windows.items()]
    lines += ["", "## Functions", "", "| Function | What it computes |", "| --- | --- |"]
    lines += [f"| `{name}` | {info.description} |" for name, info in FUNCTIONS.items()]
    lines += [
        "",
        "## The features table",
        "",
        "Written to the ingest store by `python -m features.run --store <db> --step 5`. Computing "
        "the same range again replaces the same rows, so the table is unchanged. After editing "
        "`features.yaml`, run with `--rebuild` to drop features that no longer exist.",
        "",
        "| Column | Type | Nullable | Notes |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| `{c}` | {t} | {n} | {note} |" for c, t, n, note in TABLE_COLUMNS]
    lines += ["", "**Primary key:** " + ", ".join(f"`{c}`" for c in TABLE_KEY), ""]
    lines += [
        "`read_features(store, object_type)` returns a stored object type as one wide Polars "
        "frame (a row per `ts` and `object_id`, a column per feature); `pivot_features` does the "
        "same for a frame from the compute functions.",
        "",
        "## What answers what",
        "",
        "| Need | Function | Metrics | Features |",
        "| --- | --- | --- | --- |",
    ]
    for need, rule in feature_set.coverage.items():
        found = matching(feature_set, rule)
        functions = rule["function"] if isinstance(rule["function"], list) else [rule["function"]]
        metrics = sorted({s.metric for s in found})
        shown = (
            ", ".join(f"`{m}`" for m in metrics) if len(metrics) <= 4 else f"{len(metrics)} metrics"
        )
        names = ", ".join(f"`{f}`" for f in functions)
        lines.append(f"| {need} | {names} | {shown} | {len({s.name for s in found})} |")
    lines += [
        "",
        "## Templates",
        "",
        "| Name | Metrics | Excluding | Function | Windows | Parameters |",
        "| --- | --- | --- | --- | --- | --- |",
        *_templates(doc),
        "",
        "## Features by object type",
        "",
    ]
    for object_type in feature_set.object_types():
        specs = [s for s in feature_set.specs if s.object_type == object_type]
        lines += [
            f"### {object_type} ({len(specs)} features)",
            "",
            "| Feature | Source metric | Function | Window | Parameters |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{s.name}` | `{s.metric}` | `{s.function}` | {s.window or ''} | {_params(s)} |"
            for s in specs
        ]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Write the document; return a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m features.catalogue")
    parser.add_argument("--out", type=Path, default=DOC_PATH)
    args = parser.parse_args(argv)
    args.out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
