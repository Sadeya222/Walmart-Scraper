"""Crash-safe file writing and dataset publication.

Two failure modes matter for this scraper:

1. A killed container leaves a half-written dataset that downstream jobs ingest.
2. A blocked/thin run overwrites yesterday's good dataset with an empty file.

Both are handled here.  Every write goes to a temporary file in the *same*
directory as the target, is flushed and ``fsync``-ed, then moved into place with
:func:`os.replace` (atomic on POSIX and on NTFS).  Publication is separate from
writing: :func:`publish_dataset` refuses to replace a good dataset with a
suspicious one and quarantines the run instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "append_jsonl",
    "read_jsonl",
    "sha256_file",
    "DatasetStats",
    "publish_dataset",
    "ensure_dir",
]


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _temp_path_in(target: Path) -> int:
    ensure_dir(target.parent)
    handle, name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    os.close(handle)
    return Path(name)  # type: ignore[return-value]


def atomic_write_text(target: str | Path, data: str) -> Path:
    """Write ``data`` to ``target`` atomically (temp file + ``os.replace``)."""
    path = Path(target)
    tmp = _temp_path_in(path)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return path


def atomic_write_json(target: str | Path, payload: Any, *, indent: int | None = 2) -> Path:
    """Atomically write JSON with a trailing newline."""
    return atomic_write_text(target, json.dumps(payload, indent=indent, ensure_ascii=False) + "\n")


def append_jsonl(target: str | Path, records: Iterable[dict[str, Any]]) -> int:
    """Append JSON Lines, flushing each record so a kill loses at most one line.

    Used for the incremental checkpoint stream; the *final* dataset goes through
    :func:`publish_dataset`.
    """
    path = Path(target)
    ensure_dir(path.parent)
    written = 0
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records from a JSON Lines file, skipping truncated trailing lines."""
    file_path = Path(path)
    if not file_path.exists():
        return
    with open(file_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def sha256_file(path: str | Path) -> str:
    """SHA-256 of a file in 1 MB chunks (``""`` when missing)."""
    file_path = Path(path)
    if not file_path.exists():
        return ""
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so the rename itself is durable."""
    try:
        handle = os.open(str(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without directory fds
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - not supported on every filesystem
        pass
    finally:
        os.close(handle)


@dataclass
class DatasetStats:
    """Run summary recorded next to every published dataset."""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    record_count: int = 0
    store_count: int = 0
    category_count: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)
    blocked_urls: list[str] = field(default_factory=list)
    suspect_urls: list[str] = field(default_factory=list)
    path: str = ""
    sha256: str = ""
    published: bool = False
    withheld_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def publish_dataset(
    staged_path: str | Path,
    target_path: str | Path,
    *,
    record_count: int,
    min_records: int,
    blocked: bool = False,
    suspect_ratio: float = 0.0,
    max_suspect_ratio: float = 0.34,
    quarantine_dir: str | Path | None = None,
    keep_backup: bool = True,
    stats: DatasetStats | None = None,
    now: datetime | None = None,
) -> DatasetStats:
    """Move a staged dataset into place **only** if it passes validation.

    Rules (requirement 4.4 "Atomic File Protection"):

    * never publish an empty or abnormally small run - the previous dataset is
      left untouched;
    * never publish a run that was hard-blocked;
    * never publish a run where too many pages were suspect;
    * rejected runs are moved to ``quarantine_dir`` with a reason so they can be
      inspected instead of silently disappearing;
    * a published run replaces the previous file's ``.prev`` backup.

    Returns the updated :class:`DatasetStats` with ``published`` set accordingly.
    """
    staged = Path(staged_path)
    target = Path(target_path)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    stats = stats or DatasetStats()
    stats.record_count = int(record_count)
    stats.path = str(target)
    stats.finished_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    reasons: list[str] = []
    if blocked:
        reasons.append("run_was_blocked")
    if record_count <= 0:
        reasons.append("zero_records")
    elif record_count < min_records:
        reasons.append(f"below_minimum_records_{record_count}<{min_records}")
    if suspect_ratio > max_suspect_ratio:
        reasons.append(f"suspect_page_ratio_{suspect_ratio:.2f}>{max_suspect_ratio:.2f}")
    if not staged.exists():
        reasons.append("staged_file_missing")

    if reasons:
        stats.published = False
        stats.withheld_reason = ";".join(reasons)
        stats.sha256 = sha256_file(staged) if staged.exists() else ""
        if quarantine_dir and staged.exists():
            quarantine = ensure_dir(Path(quarantine_dir))
            moved = quarantine / f"{target.stem}.{stamp}.withheld.jsonl"
            shutil.move(str(staged), str(moved))
            stats.path = str(moved)
            atomic_write_json(
                quarantine / f"{target.stem}.{stamp}.reason.json",
                {"withheld_reason": stats.withheld_reason, "stats": stats.as_dict()},
            )
        return stats

    if keep_backup and target.exists():
        backup = target.with_suffix(target.suffix + ".prev")
        try:
            shutil.copy2(str(target), str(backup))
        except OSError:  # pragma: no cover - best effort
            pass

    ensure_dir(target.parent)
    os.replace(str(staged), str(target))
    _fsync_dir(target.parent)

    stats.published = True
    stats.withheld_reason = ""
    stats.sha256 = sha256_file(target)
    return stats


def iter_chunks(records: Sequence[dict[str, Any]], size: int = 1_000) -> Iterator[list[dict[str, Any]]]:
    """Yield ``records`` in lists of at most ``size`` (used for streaming writes)."""
    for index in range(0, len(records), max(1, size)):
        yield list(records[index : index + max(1, size)])
