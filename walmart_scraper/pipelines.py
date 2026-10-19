"""Item pipeline chain: policy enforcement, dedupe, atomic publication.

Order matters and is declared in ``settings.ITEM_PIPELINES``:

1. :class:`WalmartPolicyPipeline`  - applies the data rules and drops records
   that must not reach the dataset (approximate weight, marketplace sellers,
   shipping-only offers, missing title/URL).
2. :class:`WalmartDedupePipeline`  - kills duplicates caused by sponsored
   placements and by the same product appearing in several categories.
3. :class:`WalmartAtomicWriterPipeline` - stages every surviving record and, at
   the end of the run, publishes it **only** if validation passes.  A failed run
   leaves the previous good dataset untouched.

Every drop increments ``drop_reason/<reason>`` in Scrapy stats so the run
manifest can explain exactly what was removed and why.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from scrapy import signals
from scrapy.exceptions import DropItem

from .atomic_io import DatasetStats
from .config_loader import ScraperConfig
from .constants import EXIT_VALIDATION_FAILED
from .exporters import OrderedJsonLinesWriter
from .items import ProductRecord, WalmartProductItem
from .parsing import is_marketplace, is_shipping_only
from .atomic_io import read_jsonl
from .state import RunState
from .text import strip_tracking
from .weight import all_candidates_approximate

__all__ = [
    "WalmartPolicyPipeline",
    "WalmartDedupePipeline",
    "WalmartAtomicWriterPipeline",
    "STAT_BLOCKED_PAGES",
    "STAT_SUSPECT_PAGES",
    "STAT_PAGES_CRAWLED",
    "STAT_STORE_VERIFIED",
]

logger = logging.getLogger("walmart.pipelines")

STAT_BLOCKED_PAGES = "walmart/blocked_pages"
STAT_SUSPECT_PAGES = "walmart/suspect_pages"
STAT_PAGES_CRAWLED = "walmart/pages_crawled"
STAT_STORE_VERIFIED = "walmart/stores_verified"
STAT_VALIDATION_OUTCOME = "walmart/validation_outcome"
STAT_WITHHELD_REASON = "walmart/withheld_reason"


def _config(crawler: Any) -> ScraperConfig:
    config = crawler.settings.get("WMT_CONFIG")
    if not isinstance(config, ScraperConfig):  # pragma: no cover - misconfiguration
        raise RuntimeError("WMT_CONFIG is missing from Scrapy settings")
    return config


def _drop_reason(crawler: Any, reason: str) -> None:
    crawler.stats.inc_value(f"drop_reason/{reason}")


# --------------------------------------------------------------------------- #
class WalmartPolicyPipeline:
    """Enforces the data rules that decide whether a record is exportable."""

    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self.require_tokens: tuple[str, ...] = ()
        if config.fulfillment_mode in {"pickup", "in_store"}:
            self.require_tokens = ("store", "pickup", "curbside")

    @classmethod
    def from_crawler(cls, crawler: Any) -> "WalmartPolicyPipeline":
        return cls(_config(crawler))

    # ------------------------------------------------------------------ #
    def process_item(self, item: Any, spider: Any) -> Any:
        record = item.item if isinstance(item, ProductRecord) else item
        if not isinstance(record, WalmartProductItem):  # pragma: no cover - defensive
            return item

        product = item.product if isinstance(item, ProductRecord) else None

        if not record.get("product_title"):
            _drop_reason(spider.crawler, "missing_title")
            raise DropItem("missing product_title")
        if not record.get("url"):
            _drop_reason(spider.crawler, "missing_url")
            raise DropItem("missing url")

        if product is not None:
            # Requirement: exclude products whose weight wording is approximate.
            if all_candidates_approximate(product.weight_candidates):
                _drop_reason(spider.crawler, "approximate_weight")
                raise DropItem(f"approximate weight: {product.weight_candidates[:2]}")

            if self.config.exclude_marketplace and is_marketplace(product):
                _drop_reason(spider.crawler, "marketplace_seller")
                raise DropItem(f"marketplace seller: {product.seller_name!r}")

            if self.config.shipping_only_mode == "exclude" and is_shipping_only(
                product, require_tokens=self.require_tokens
            ):
                _drop_reason(spider.crawler, "shipping_only_offer")
                raise DropItem("offer is shipping only at this store")

        return item


# --------------------------------------------------------------------------- #
class WalmartDedupePipeline:
    """Removes duplicate records within a run.

    Key precedence: ``(zipcode, upc)`` when Walmart publishes a UPC, else
    ``(zipcode, product url without click-tracking params)``, else
    ``(zipcode, usItemId)``.  Sponsored placements reuse the same product id with
    different tracking query strings, which is exactly why tracking params are
    stripped from the *key* (the exported ``url`` keeps them).
    """

    def __init__(self, seed_records: Iterable[dict[str, Any]] = ()) -> None:
        self.seen: set[tuple[str, str]] = set()
        for record in seed_records:
            key = self._key(record)
            if key[1]:
                self.seen.add(key)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "WalmartDedupePipeline":
        config = _config(crawler)
        # A resumed run starts from the already published dataset, so records
        # that were exported before must not be duplicated by this run.
        seed: list[dict[str, Any]] = []
        if config.resume and config.output_path.exists():
            seed = list(read_jsonl(config.output_path))
            logger.info("dedupe seeded with %s existing records", len(seed))
        return cls(seed)

    @staticmethod
    def _key(record: dict[str, Any]) -> tuple[str, str]:
        zipcode = str(record.get("zipcode", ""))
        upc = str(record.get("upc", "")).strip()
        if upc:
            return (zipcode, f"upc:{upc}")
        url = str(record.get("url", "")).strip()
        return (zipcode, f"url:{strip_tracking(url)}") if url else (zipcode, "")

    def process_item(self, item: Any, spider: Any) -> Any:
        record = item.item if isinstance(item, ProductRecord) else item
        key = self._key(record)
        if key[1] and key in self.seen:
            _drop_reason(spider.crawler, "duplicate_record")
            raise DropItem(f"duplicate {key[1]}")
        self.seen.add(key)
        if isinstance(item, ProductRecord):
            spider.crawler.stats.inc_value("walmart/unique_records")
        return item


# --------------------------------------------------------------------------- #
class WalmartAtomicWriterPipeline:
    """Stages records and publishes the dataset only when validation passes."""

    def __init__(self, config: ScraperConfig, state: RunState) -> None:
        self.config = config
        self.state = state
        self.writer = OrderedJsonLinesWriter(
            staged_path=Path(config.output_dir) / f".{config.dataset_name}.staged",
            target_path=config.output_path,
            quarantine_dir=config.quarantine_dir,
            seed_from=config.output_path if config.resume else None,
        )
        self.stats = DatasetStats()

    # ------------------------------------------------------------------ #
    @classmethod
    def from_crawler(cls, crawler: Any) -> "WalmartAtomicWriterPipeline":
        pipeline = cls(_config(crawler), crawler.settings.get("WMT_STATE"))
        crawler.signals.connect(pipeline.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(pipeline.spider_closed, signal=signals.spider_closed)
        return pipeline

    def spider_opened(self, spider: Any) -> None:
        crawler = spider.crawler
        self.stats = DatasetStats(
            run_id=str(
                crawler.stats.get_value("walmart/run_id", "")
                or crawler.settings.get("WMT_RUN_ID", "")
            ),
            started_at=str(
                crawler.stats.get_value("walmart/run_started", "")
                or crawler.settings.get("WMT_RUN_STARTED", "")
            ),
        )
        self.writer.open()
        logger.info("staging dataset to %s", self.writer.staged_path)

    # ------------------------------------------------------------------ #
    def process_item(self, item: Any, spider: Any) -> Any:
        record = item.to_ordered_dict() if isinstance(item, ProductRecord) else item
        self.writer.write(record)
        self.state.add_records(1)
        return item

    # ------------------------------------------------------------------ #
    def spider_closed(self, spider: Any, reason: str = "") -> None:
        crawler = spider.crawler
        blocked = int(crawler.stats.get_value(STAT_BLOCKED_PAGES, 0))
        suspect = int(crawler.stats.get_value(STAT_SUSPECT_PAGES, 0))
        pages = int(crawler.stats.get_value(STAT_PAGES_CRAWLED, 0))
        suspect_ratio = (suspect / pages) if pages else 0.0

        self.stats.store_count = int(crawler.stats.get_value(STAT_STORE_VERIFIED, 0))
        self.stats.category_count = len(self.config.categories)
        self.stats.drop_reasons = {
            key.split("/", 1)[1]: int(value)
            for key, value in crawler.stats.get_stats().items()
            if key.startswith("drop_reason/")
        }
        self.stats.blocked_urls = sorted(self.state.blocked_urls)[:200]
        self.stats.suspect_urls = []

        hard_blocked = blocked > self.config.max_blocked_pages
        self.stats = self.writer.publish(
            min_records=self.config.min_records_per_run,
            blocked=hard_blocked or bool(crawler.stats.get_value("walmart/run_blocked", False)),
            suspect_ratio=suspect_ratio,
            max_suspect_ratio=self.config.max_suspect_ratio,
            stats=self.stats,
        )

        crawler.stats.set_value(STAT_VALIDATION_OUTCOME, "published" if self.stats.published else "withheld")
        if self.stats.published:
            logger.info(
                "published %s records to %s (sha256=%s)",
                self.stats.record_count, self.stats.path, self.stats.sha256[:12],
            )
            crawler.stats.set_value("walmart/dataset_path", self.stats.path)
            crawler.stats.set_value("walmart/dataset_sha256", self.stats.sha256)
            crawler.stats.set_value("walmart/dataset_record_count", self.stats.record_count)
        else:
            logger.error(
                "DATASET WITHHELD (%s): previous dataset retained; staged run moved to %s",
                self.stats.withheld_reason, self.stats.path,
            )
            crawler.stats.set_value(STAT_WITHHELD_REASON, self.stats.withheld_reason)
            crawler.stats.set_value("walmart/exit_code", EXIT_VALIDATION_FAILED)
