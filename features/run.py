"""CLI: python -m features.run --store <db> --step 5.

Computes features from clean_metrics and upserts them into the store's `features` table.
Exit code: 0 on success, 2 on a usage, file or data error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import duckdb

from features.compute import FeaturesError, to_utc
from features.spec import SpecError
from features.table import write_features


def _moment(text: str) -> datetime:
    try:
        return to_utc(datetime.fromisoformat(text))
    except ValueError:
        raise argparse.ArgumentTypeError(f"cannot read {text!r} as an ISO-8601 time") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m features.run", description=__doc__)
    parser.add_argument("--store", required=True, type=Path, help="ingest store DuckDB file")
    parser.add_argument("--step", type=int, default=5, help="minutes between feature times")
    parser.add_argument("--start", type=_moment, help="first time (default: first sample)")
    parser.add_argument("--end", type=_moment, help="last time (default: last sample)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="drop the features table first (use after editing features.yaml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Compute and store features; return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        summary = write_features(args.store, args.start, args.end, args.step, args.rebuild)
    except (FeaturesError, SpecError, OSError, duckdb.Error) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(
        f"{summary.rows} feature rows for {summary.series} series at {summary.times} times "
        f"({summary.start.isoformat()} to {summary.end.isoformat()}), step {args.step} min"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
