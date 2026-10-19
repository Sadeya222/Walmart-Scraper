"""Staged, ordered JSON Lines writing with atomic publication.

Records are appended to a *staged* file as they are scraped (so a long run leaves
inspectable progress behind), and only promoted to the real dataset path by
:func:`publish_dataset` once the run passes validation.  Key order in every line
comes from ``FIELD_ORDER`` via :meth:`WalmartProductItem.to_ordered_dict`, so the
JSON on disk matches the required column order byte for byte.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from .atomic_io import (
    DatasetStats,
    ensure_dir,
    publish_dataset,
    read_jsonl,
    sha256_file,
)

__all__ = ["OrderedJsonLinesWriter"]


class OrderedJsonLinesWriter:
    """Append-only staged writer that publishes atomically on success."""

    def __init__(
        self,
        staged_path: str | Path,
        target_path: str | Path,
        *,
        quarantine_dir: str | Path | None = None,
        keep_backup: bool = True,
        seed_from: str | Path | None = None,
    ) -> None:
        self.staged_path = Path(staged_path)
        self.target_path = Path(target_path)
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir else None
        self.keep_backup = keep_backup
        # On a resumed run the staged file must start as a copy of the dataset
        # that is already published.  Otherwise a run that only re-crawls part of
        # the plan would publish a *partial* file over a complete one.
        self.seed_from = Path(seed_from) if seed_from else None
        self.count = 0
        self.seeded = 0
        self._handle = None
        ensure_dir(self.staged_path.parent)

    # ------------------------------------------------------------------ #
    def open(self) -> "OrderedJsonLinesWriter":
        if self._handle is None:
            ensure_dir(self.staged_path.parent)
            if self.seed_from and self.seed_from.exists():
                shutil.copyfile(self.seed_from, self.staged_path)
                self.seeded = sum(1 for _ in read_jsonl(self.staged_path))
                self.count = self.seeded
                self._handle = open(self.staged_path, "a", encoding="utf-8", newline="\n")
            else:
                self._handle = open(self.staged_path, "w", encoding="utf-8", newline="\n")
        return self

    def write(self, record: "OrderedDict[str, Any]") -> None:
        """Append one record as a single JSON line (key order preserved)."""
        self.open()
        assert self._handle is not None
        self._handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        self._handle.write("\n")
        self.count += 1

    def write_many(self, records: Iterable["OrderedDict[str, Any]"]) -> int:
        for record in records:
            self.write(record)
        return self.count

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            self.flush()
            self._handle.close()
            self._handle = None

    # ------------------------------------------------------------------ #
    def discard_staged(self) -> None:
        """Remove the staged file without touching any published dataset."""
        self.close()
        self.staged_path.unlink(missing_ok=True)

    def publish(
        self,
        *,
        min_records: int,
        blocked: bool = False,
        suspect_ratio: float = 0.0,
        max_suspect_ratio: float = 0.34,
        stats: DatasetStats | None = None,
    ) -> DatasetStats:
        """Close the staged file and promote it if - and only if - it validates."""
        self.close()
        return publish_dataset(
            self.staged_path,
            self.target_path,
            record_count=self.count,
            min_records=min_records,
            blocked=blocked,
            suspect_ratio=suspect_ratio,
            max_suspect_ratio=max_suspect_ratio,
            quarantine_dir=self.quarantine_dir,
            keep_backup=self.keep_backup,
            stats=stats,
        )

    # ------------------------------------------------------------------ #
    @property
    def staged_sha256(self) -> str:
        return sha256_file(self.staged_path)

    def __enter__(self) -> "OrderedJsonLinesWriter":
        return self.open()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
