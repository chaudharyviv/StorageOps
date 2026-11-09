"""A file-based queue of batches: one Parquet file per batch in a directory.

Producers write to a temporary name and rename, so a consumer never sees a half-written
batch. File names carry a sequence number, so batches come out in the order they went in.
A batch is removed only when the consumer acknowledges it, so a crash means it is seen
again (at least once); loading into the store is idempotent, which makes that safe.
A Redis queue can replace this later behind the same four methods.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import polars as pl

SUFFIX = ".parquet"


class FileQueue:
    """Directory-backed FIFO queue of batches."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        existing = [int(p.stem) for p in directory.glob(f"*{SUFFIX}")]
        self._next = max(existing, default=0) + 1

    def put(self, frame: pl.DataFrame) -> int:
        """Append a batch; return its sequence number (the batch id)."""
        seq = self._next
        self._next += 1
        final = self.directory / f"{seq:09d}{SUFFIX}"
        temporary = final.with_suffix(".tmp")
        frame.write_parquet(temporary)
        os.replace(temporary, final)
        return seq

    def pending(self) -> Iterator[tuple[int, Path]]:
        """Yield (sequence number, file) for queued batches, oldest first."""
        for path in sorted(self.directory.glob(f"*{SUFFIX}")):
            yield int(path.stem), path

    def ack(self, path: Path) -> None:
        """Remove a batch once it has been handled."""
        path.unlink(missing_ok=True)

    def __len__(self) -> int:
        return sum(1 for _ in self.directory.glob(f"*{SUFFIX}"))
