"""Resume-after-interruption checkpoints.

A Walmart crawl is long (many stores x many categories x deep pagination x PDP
fetches).  When Azure recycles the container we must not restart from zero, and
we must not re-export rows already written.  :class:`RunState` persists:

* completed ``(store, category, page)`` listing keys, so pagination resumes;
* fetched product keys, so PDPs are not re-requested;
* the count of records already staged, for the validation gate;
* blocked URLs, so a poisoned store/category can be reported rather than retried
  forever.

State is written atomically (temp + ``os.replace``) after every checkpoint, so a
kill mid-write cannot corrupt it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["RunState", "listing_key", "product_key"]


def listing_key(store_id: str, category_url: str, page: int) -> str:
    return f"listing::{store_id}::{category_url}::{page}"


def product_key(store_id: str, product_id: str) -> str:
    return f"product::{store_id}::{product_id}"


@dataclass
class RunState:
    """JSON backed checkpoint file for one run."""

    path: Path
    run_id: str = ""
    resume: bool = False
    completed_listings: set[str] = field(default_factory=set)
    fetched_products: set[str] = field(default_factory=set)
    seen_item_ids: set[str] = field(default_factory=set)
    blocked_urls: set[str] = field(default_factory=set)
    records_staged: int = 0
    checkpoints: int = 0
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path, *, resume: bool = False, run_id: str = "") -> "RunState":
        file_path = Path(path)
        state = cls(path=file_path, resume=resume, run_id=run_id)
        if not resume or not file_path.exists():
            return state
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt checkpoint must not abort the run: start fresh and let
            # the validation gate protect the published dataset.
            return state
        state.completed_listings = set(raw.get("completed_listings") or ())
        state.fetched_products = set(raw.get("fetched_products") or ())
        state.seen_item_ids = set(raw.get("seen_item_ids") or ())
        state.blocked_urls = set(raw.get("blocked_urls") or ())
        state.records_staged = int(raw.get("records_staged") or 0)
        state.checkpoints = int(raw.get("checkpoints") or 0)
        state.run_id = run_id or str(raw.get("run_id") or "")
        return state

    # ------------------------------------------------------------------ #
    def is_listing_done(self, key: str) -> bool:
        return self.resume and key in self.completed_listings

    def is_product_done(self, key: str) -> bool:
        return self.resume and key in self.fetched_products

    def mark_listing_done(self, key: str) -> None:
        self.completed_listings.add(key)

    def mark_product_done(self, key: str) -> None:
        self.fetched_products.add(key)

    def already_seen(self, item_id: str) -> bool:
        return item_id in self.seen_item_ids

    def note_item(self, item_id: str) -> None:
        self.seen_item_ids.add(item_id)

    def note_blocked(self, url: str) -> None:
        self.blocked_urls.add(url)

    def add_records(self, count: int) -> None:
        self.records_staged += max(0, int(count))

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "resume": self.resume,
            "updated_at": self.updated_at,
            "checkpoints": self.checkpoints,
            "records_staged": self.records_staged,
            "completed_listings": sorted(self.completed_listings),
            "fetched_products": sorted(self.fetched_products),
            "seen_item_ids": sorted(self.seen_item_ids),
            "blocked_urls": sorted(self.blocked_urls),
        }

    def save(self) -> Path:
        """Atomically persist the checkpoint (safe against a kill mid-write)."""
        self.updated_at = time.time()
        self.checkpoints += 1
        ensure_parent(self.path)
        handle, name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state.", suffix=".tmp")
        tmp = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.to_dict(), stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self.path

    def clear(self) -> None:
        self.completed_listings.clear()
        self.fetched_products.clear()
        self.seen_item_ids.clear()
        self.records_staged = 0
        if self.path.exists():
            self.path.unlink(missing_ok=True)


def ensure_parent(path: Path) -> Path:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path
