"""Raw-payload evidence capture (audit trail for a reviewer).

A reviewer asking to see "the product and store response behind one displayed
price" wants proof that the dataset came from Walmart, not from a template.
This module records, for a bounded number of products per store:

* ``store_context_store<id>.json`` - what Walmart resolved for the store
  context: the location Walmart embedded in the payload (``postalCode`` /
  ``storeIds`` / ``fulfillmentLabel[].locationText``), the rendered store
  indicator, the verification outcome (method, matched signal, detail), the
  fulfillment mode in effect, and the location cookies seeded into the browser
  context. This is the "store response" behind every displayed price.
* ``price_evidence_store<id>_<item>.json`` - the raw listing tile node and the
  raw PDP product node exactly as Walmart served them, plus the exported record
  they produced and the deals classified from them. This is the "product
  response behind one displayed price".
* ``evidence_index.json`` - the list of files written during the run, written
  when the spider closes.

Guarantees:

* **Never breaks the crawl.** Every public method swallows its own errors and
  logs a warning; an evidence failure can never fail a run.
* **Bounded cost.** At most ``WMT_EVIDENCE_MAX_PRODUCTS`` price files are
  written per store, and raw payloads are only held in memory for the products
  being tracked.
* **Not committed.** The default directory is ``output/evidence/<run_id>/``, and
  ``output/`` is git-ignored. Evidence files contain Walmart pages, so store
  them with the same care as the dataset itself.
* **No secrets.** Proxy URLs, storage connection strings and credentials are
  never written. The seeded location cookies (``wm_st`` & friends) contain only
  the store id / ZIP / coordinates the operator configured.

Enable with ``WMT_EVIDENCE_ENABLED=true`` or the ``--evidence`` flag.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json
from .parsing import find_pdp_product_node, iter_listing_item_nodes
from .store_context import _payload_location_evidence
from .text import clean_text

logger = logging.getLogger("walmart.evidence")

MAX_TRACKED = 25  # hard ceiling for WMT_EVIDENCE_MAX_PRODUCTS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", clean_text(value)).strip("._")
    return (slug or "item")[:60]


def _node_identity(node: dict[str, Any]) -> str:
    """usItemId of a raw tile, mirroring the identity :func:`_parse_product_node` uses."""
    for path in ("usItemId", "id", "productId", "itemId", "product.usItemId"):
        parts = path.split(".")
        current: Any = node
        for part in parts:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current is not None and clean_text(str(current)):
            return clean_text(str(current))
    return ""


class EvidenceCollector:
    """Collects and writes raw-payload evidence during a run."""

    def __init__(
        self,
        *,
        enabled: bool,
        directory: str | Path = "output/evidence",
        run_id: str = "",
        max_products_per_store: int = 2,
    ) -> None:
        self.enabled = enabled
        self.run_id = run_id or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
        self.directory = Path(directory) / self.run_id
        self.max_products_per_store = max(0, min(int(max_products_per_store), MAX_TRACKED))
        self.written: list[str] = []
        # Raw payloads held in memory, keyed "<store_id>:<identity>", kept only
        # for the first max_products_per_store identities per store.
        self._tracked: dict[str, set[str]] = {}
        self._tiles: dict[str, dict[str, Any]] = {}
        self._pdp_nodes: dict[str, Any] = {}
        self._files_per_store: dict[str, int] = {}

    # ------------------------------------------------------------------ #
    @classmethod
    def from_settings(cls, settings: Any) -> "EvidenceCollector":
        enabled = str(settings.get("WMT_EVIDENCE_ENABLED", "false")).strip().lower() not in {
            "0", "false", "no", "off",
        }
        try:
            max_products = int(settings.get("WMT_EVIDENCE_MAX_PRODUCTS", 2))
        except (TypeError, ValueError):
            max_products = 2
        return cls(
            enabled=enabled,
            directory=settings.get("WMT_EVIDENCE_DIR", "output/evidence") or "output/evidence",
            run_id=str(settings.get("WMT_RUN_ID", "") or ""),
            max_products_per_store=max_products,
        )

    # ------------------------------------------------------------------ #
    def _track(self, store_id: str, key: str) -> bool:
        """Budget check: at most ``max_products_per_store`` identities per store."""
        tracked = self._tracked.setdefault(store_id, set())
        if key in tracked:
            return True
        if len(tracked) >= self.max_products_per_store:
            return False
        tracked.add(key)
        return True

    def _write(self, name: str, doc: dict[str, Any]) -> Path | None:
        path = self.directory / name
        atomic_write_json(path, doc)
        self.written.append(str(path))
        logger.info("evidence written: %s", path)
        return path

    # ------------------------------------------------------------------ #
    # Store context
    # ------------------------------------------------------------------ #
    def record_store_context(
        self,
        *,
        store: Any,
        verification: Any,
        payload: Any,
        response_url: str,
        verification_mode: str = "",
        cookies: list[dict[str, Any]] | None = None,
        fulfillment_mode: str = "",
    ) -> Path | None:
        """Write the store/fulfillment evidence for one store. Never raises."""
        if not self.enabled:
            return None
        try:
            store_id = clean_text(str(getattr(store, "store_id", ""))) or "unknown"
            if isinstance(payload, dict):
                raw = _payload_location_evidence(payload)
                payload_location = {
                    "postal_codes": sorted(raw["postal_codes"]),
                    "store_ids": sorted(raw["store_ids"]),
                    "location_text": list(raw["location_text"]),
                }
            else:
                payload_location = {"postal_codes": [], "store_ids": [], "location_text": []}
            as_dict = getattr(verification, "as_dict", None)
            if callable(as_dict):
                verification_dict = as_dict()
            else:
                verification_dict = {"verified": bool(getattr(verification, "verified", False))}
            doc = {
                "run_id": self.run_id,
                "captured_at": _now_iso(),
                "evidence": "store_context",
                "url": response_url,
                "store": {
                    "store_id": store_id,
                    "zipcode": clean_text(str(getattr(store, "zipcode", ""))),
                    "store_name": clean_text(str(getattr(store, "store_name", ""))),
                    "store_location": clean_text(str(getattr(store, "store_location", ""))),
                    "store_logo": clean_text(str(getattr(store, "store_logo", ""))),
                    "latitude": clean_text(str(getattr(store, "latitude", ""))),
                    "longitude": clean_text(str(getattr(store, "longitude", ""))),
                },
                "fulfillment_mode": fulfillment_mode,
                "verification": {"mode": verification_mode, **verification_dict},
                "payload_location": payload_location,
                "cookies_seeded": [
                    {"name": clean_text(str(cookie.get("name", ""))),
                     "value": clean_text(str(cookie.get("value", "")))}
                    for cookie in (cookies or [])
                    if isinstance(cookie, dict)
                ],
                "notes": [
                    "cookies contain the configured location preferences only; no credentials",
                    "payload_location is the store/fulfillment context Walmart resolved into "
                    "the page payload; verification is how the scraper confirmed it",
                    "every record from this store in the dataset was priced for this context",
                ],
            }
            return self._write(f"store_context_store{_slug(store_id)}.json", doc)
        except Exception as exc:  # evidence must never break the crawl
            logger.warning("store-context evidence failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Product payloads
    # ------------------------------------------------------------------ #
    def stash_listing_items(self, store: Any, payload: Any, products: list[Any]) -> None:
        """Hold the raw tile nodes for the first products of this store. Never raises."""
        if not self.enabled or not isinstance(payload, dict):
            return
        try:
            store_id = clean_text(str(getattr(store, "store_id", ""))) or "unknown"
            nodes: dict[str, dict[str, Any]] = {}
            for node in iter_listing_item_nodes(payload):
                identity = _node_identity(node)
                if identity:
                    nodes.setdefault(identity, node)
            for product in products:
                identity = getattr(product, "us_item_id", "") or ""
                if not identity:
                    continue
                node = nodes.get(identity)
                if node is None:
                    continue
                key = f"{store_id}:{identity}"
                if key in self._tiles:
                    continue
                if not self._track(store_id, identity):
                    continue
                self._tiles[key] = node
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("listing evidence stash failed: %s", exc)

    def stash_pdp_node(self, store: Any, product: Any, payload: Any) -> None:
        """Hold the raw PDP product node for a tracked product. Never raises."""
        if not self.enabled or not isinstance(payload, dict):
            return
        try:
            store_id = clean_text(str(getattr(store, "store_id", ""))) or "unknown"
            identity = getattr(product, "identity", "") or ""
            if not identity:
                return
            if not self._track(store_id, identity):
                return
            node = find_pdp_product_node(payload)
            if node is not None:
                self._pdp_nodes[f"{store_id}:{identity}"] = node
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("pdp evidence stash failed: %s", exc)

    # ------------------------------------------------------------------ #
    def record_price(
        self,
        *,
        record: Any,
        store: Any,
        target: Any,
        page: int,
    ) -> Path | None:
        """Write the raw product response(s) behind one displayed price. Never raises."""
        if not self.enabled:
            return None
        try:
            store_id = clean_text(str(getattr(store, "store_id", ""))) or "unknown"
            if self._files_per_store.get(store_id, 0) >= self.max_products_per_store:
                return None
            product = record.product
            identity = getattr(product, "identity", "") or "unknown"
            tile = self._tiles.pop(f"{store_id}:{identity}", None)
            pdp_node = self._pdp_nodes.pop(f"{store_id}:{identity}", None)
            notes: list[str] = []
            if tile is None:
                notes.append(
                    "raw_listing_tile_unavailable (resumed run, identity not matched, or per-store budget)"
                )
            if pdp_node is None:
                notes.append(
                    "raw_pdp_payload_unavailable (product page not fetched - resume, --no-pdp or PDP budget)"
                )
            item_dict = record.to_ordered_dict() if hasattr(record, "to_ordered_dict") else {}
            doc = {
                "run_id": self.run_id,
                "captured_at": _now_iso(),
                "evidence": "price",
                "store": {
                    "store_id": store_id,
                    "zipcode": clean_text(str(getattr(store, "zipcode", ""))),
                    "store_name": clean_text(str(getattr(store, "store_name", ""))),
                    "store_location": clean_text(str(getattr(store, "store_location", ""))),
                },
                "listing": {
                    "url": clean_text(str(getattr(target, "url", ""))),
                    "category": clean_text(str(getattr(target, "category", ""))),
                    "sub_category": clean_text(str(getattr(target, "sub_category", ""))),
                    "page": int(page),
                    "raw_tile": tile,
                },
                "pdp": {
                    "url": clean_text(str(getattr(product, "url", ""))),
                    "raw_product": pdp_node,
                },
                "record": item_dict,
                "notes": notes,
            }
            path = self._write(
                f"price_evidence_store{_slug(store_id)}_{_slug(identity)}.json", doc
            )
            if path is not None:
                self._files_per_store[store_id] = self._files_per_store.get(store_id, 0) + 1
            return path
        except Exception as exc:  # evidence must never break the crawl
            logger.warning("price evidence failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    def write_index(self) -> Path | None:
        """Write ``evidence_index.json`` listing everything captured this run."""
        if not self.enabled:
            return None
        try:
            doc = {
                "run_id": self.run_id,
                "captured_at": _now_iso(),
                "files": list(self.written),
                "counts": {
                    "store_context": sum(1 for f in self.written if "store_context_" in f),
                    "price": sum(1 for f in self.written if "price_evidence_" in f),
                },
            }
            path = self.directory / "evidence_index.json"
            atomic_write_json(path, doc)
            self.written.append(str(path))
            logger.info("evidence index written to %s (%d files)", path, len(doc["files"]))
            return path
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("evidence index failed: %s", exc)
            return None
