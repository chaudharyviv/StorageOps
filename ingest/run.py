"""CLI: python -m ingest.run --metrics <db> --store <db>.

Replays a simulator metrics file through a file queue, validates every batch, loads good rows
into clean_metrics and findings into quarantine, and records the run in validation_runs.
Exit code: 0 if everything passed, 1 if anything was quarantined, 2 on a usage or file error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ingest.pipeline import run_ingest
from ingest.reader import ReplayReader, SourceError
from ingest.report import RunReport
from ingest.rules import RulesError, load_rules


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ingest.run", description=__doc__)
    parser.add_argument("--metrics", required=True, type=Path, help="simulator metrics DuckDB file")
    parser.add_argument("--store", required=True, type=Path, help="store DuckDB file to write")
    parser.add_argument("--batch-minutes", type=int, default=60, help="minutes per batch")
    parser.add_argument("--queue-dir", type=Path, help="keep queue files here (default: temp dir)")
    parser.add_argument("--report", type=Path, help="also write the run report as JSON")
    return parser


def _print_summary(report: RunReport) -> None:
    print(
        f"{report.status.upper()}: {report.batches} batches, {report.rows_read} rows read, "
        f"{report.rows_clean} clean ({report.rows_loaded} new, {report.rows_already_present} "
        f"already present), {report.rows_quarantined} quarantined"
    )
    for check in report.checks:
        if check.failed:
            print(f"  {check.code}: {check.failed}  ({check.description})")


def main(argv: list[str] | None = None) -> int:
    """Run one ingest; return a process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.store.resolve() == args.metrics.resolve():
            raise SourceError("--store must be a different file than --metrics")
        rules = load_rules()
        with ReplayReader(args.metrics, args.batch_minutes) as reader:
            report = run_ingest(
                reader.batches(), args.store, str(args.metrics), args.batch_minutes,
                rules=rules, queue_dir=args.queue_dir,
            )  # fmt: skip
    except (SourceError, RulesError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.report:
        args.report.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    _print_summary(report)
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
