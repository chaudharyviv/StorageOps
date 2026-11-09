"""CLI: python -m simulator.run --scenario <name> --variant <v> --seed <n> --out <db>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from contracts import Variant
from simulator.engine import simulate
from simulator.scenario import SCENARIO_DIRS, ScenarioError, find_scenario
from simulator.store import write_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m simulator.run", description=__doc__)
    parser.add_argument("--scenario", required=True, help="scenario name, e.g. aggregate_filling")
    parser.add_argument("--variant", required=True, choices=[v.value for v in Variant])
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path, help="metrics DuckDB file to create")
    parser.add_argument(
        "--labels-out",
        type=Path,
        help="labels DuckDB file (ground truth; only eval/ may open it); "
        "default: --out with .labels.duckdb",
    )
    parser.add_argument(
        "--scenarios-dir",
        action="append",
        type=Path,
        help="directory to search for <scenario>.yaml (repeatable); defaults to the built-in ones",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace --out and the labels file if they exist"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one scenario variant and write the result; return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        definition = find_scenario(args.scenario, args.scenarios_dir or SCENARIO_DIRS)
        variant = definition.variant(Variant(args.variant))
        result = simulate(variant, args.seed)
        labels_path = write_run(args.out, result, args.labels_out, args.overwrite)
    except (ScenarioError, FileExistsError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    rows = result.minutes * len(result.series)
    print(
        f"{variant.scenario}/{variant.variant} seed={args.seed} split={variant.split}: "
        f"{rows} metric rows, {len(result.labels)} label(s) -> "
        f"{args.out} (labels: {labels_path})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
