"""Scrapy extensions: run lifecycle, checkpoints, manifest, Azure upload.

* :class:`RunLifecycleExtension` - stamps a run id, saves the resume checkpoint
  periodically, and stops the run early if it becomes clear we are being blocked
  (protecting the previous dataset from a doomed overwrite).
* :class:`RunManifestExtension` - writes ``run_manifest.json`` next to the
  dataset: config in effect, drop reasons, block counts, dataset sha256, and the
  exit code the runner should return.
* :class:`AzureBlobUploadExtension` - optional; uploads the published dataset to
  Azure Blob Storage when a connection string is configured.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scrapy import signals
from scrapy.exceptions import NotConfigured

from .atomic_io import atomic_write_json, ensure_dir
from .config_loader import ScraperConfig
from .constants import (
    EXIT_BLOCKED,
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_VALIDATION_FAILED,
    RUN_MANIFEST_NAME,
)
from .pipelines import (
    STAT_BLOCKED_PAGES,
    STAT_PAGES_CRAWLED,
    STAT_SUSPECT_PAGES,
    STAT_STORE_VERIFIED,
    STAT_VALIDATION_OUTCOME,
    STAT_WITHHELD_REASON,
)
from .state import RunState

__all__ = ["RunLifecycleExtension", "RunManifestExtension", "AzureBlobUploadExtension"]

logger = logging.getLogger("walmart.extensions")


def _config(crawler: Any) -> ScraperConfig:
    config = crawler.settings.get("WMT_CONFIG")
    if not isinstance(config, ScraperConfig):  # pragma: no cover - misconfiguration
        raise NotConfigured("WMT_CONFIG missing from settings")
    return config


# --------------------------------------------------------------------------- #
class RunLifecycleExtension:
    """Run identity + periodic checkpointing + early abort when hard blocked."""

    def __init__(self, config: ScraperConfig, state: RunState, checkpoint_every: int) -> None:
        self.config = config
        self.state = state
        self.checkpoint_every = max(1, checkpoint_every)
        self.run_id = ""
        self._since_checkpoint = 0
        self._started = datetime.now(timezone.utc)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "RunLifecycleExtension":
        config = _config(crawler)
        state = crawler.settings.get("WMT_STATE")
        if not isinstance(state, RunState):  # pragma: no cover
            raise NotConfigured("WMT_STATE missing from settings")
        ext = cls(config, state, _env_int("WMT_CHECKPOINT_EVERY", 25))
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        return ext

    # ------------------------------------------------------------------ #
    def spider_opened(self, spider: Any) -> None:
        # Scrapy freezes Settings once the crawler starts, so run identity is
        # published through stats rather than written back into settings.
        crawler = spider.crawler
        run_id = crawler.settings.get("WMT_RUN_ID") or uuid.uuid4().hex[:12]
        started_at = crawler.settings.get("WMT_RUN_STARTED") or self._started.isoformat(
            timespec="seconds"
        )
        self.run_id = run_id
        crawler.stats.set_value("walmart/run_id", run_id)
        crawler.stats.set_value("walmart/run_started", started_at)
        logger.info(
            "run %s started (resume=%s) stores=%s categories=%s",
            run_id, self.state.resume,
            len(self.config.stores), len(self.config.categories),
        )

    def item_scraped(self, item: Any, response: Any, spider: Any) -> None:
        self._since_checkpoint += 1
        if self._since_checkpoint >= self.checkpoint_every:
            self._since_checkpoint = 0
            self.state.save()
            spider.crawler.stats.inc_value("walmart/checkpoints_saved")

        blocked = int(spider.crawler.stats.get_value(STAT_BLOCKED_PAGES, 0))
        if blocked > self.config.max_blocked_pages:
            spider.crawler.stats.set_value("walmart/run_blocked", True)
            spider.crawler.stats.set_value("walmart/exit_code", EXIT_BLOCKED)
            logger.error(
                "aborting run: %s blocked pages exceed WMT_MAX_BLOCKS_PER_RUN=%s",
                blocked, self.config.max_blocked_pages,
            )
            spider.crawler.engine.close_spider(spider, "blocked")

    def spider_closed(self, spider: Any, reason: str = "") -> None:
        try:
            self.state.save()
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning("could not save checkpoint: %s", exc)
        logger.info(
            "run finished reason=%s listings=%s products=%s blocked=%s suspect=%s",
            reason,
            len(self.state.completed_listings),
            len(self.state.fetched_products),
            spider.crawler.stats.get_value(STAT_BLOCKED_PAGES, 0),
            spider.crawler.stats.get_value(STAT_SUSPECT_PAGES, 0),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):  # pragma: no cover
        return default


# --------------------------------------------------------------------------- #
class RunManifestExtension:
    """Writes ``run_manifest.json`` describing the run and its dataset."""

    def __init__(self, config: ScraperConfig, crawler: Any) -> None:
        self.config = config
        self.crawler = crawler
        self.close_reason = ""

    @classmethod
    def from_crawler(cls, crawler: Any) -> "RunManifestExtension":
        ext = cls(_config(crawler), crawler)
        # spider_closed only records why the spider stopped; engine_stopped fires
        # *after* it, once the atomic writer pipeline has published or withheld,
        # which is when the manifest can state the truth.
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(ext.engine_stopped, signal=signals.engine_stopped)
        return ext

    def spider_closed(self, spider: Any, reason: str = "") -> None:
        self.close_reason = reason or ""

    def engine_stopped(self) -> None:
        crawler = self.crawler
        stats = crawler.stats.get_stats()
        published = stats.get(STAT_VALIDATION_OUTCOME) == "published"
        records_this_run = int(stats.get("item_scraped_count", 0))
        # Judge the exit code on the dataset that was actually published, not on
        # how many rows *this* run added: a resumed run with nothing left to do
        # still leaves a complete, valid dataset behind and is a success.
        records_in_dataset = int(stats.get("walmart/dataset_record_count", 0))

        if not published:
            exit_code = EXIT_BLOCKED if stats.get("walmart/run_blocked") else EXIT_VALIDATION_FAILED
        elif records_in_dataset == 0:
            exit_code = EXIT_NO_DATA
        else:
            exit_code = EXIT_OK
        crawler.stats.set_value("walmart/exit_code", exit_code)

        manifest = {
            "run_id": stats.get("walmart/run_id") or crawler.settings.get("WMT_RUN_ID", ""),
            "started_at": stats.get("walmart/run_started")
            or crawler.settings.get("WMT_RUN_STARTED", ""),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "close_reason": self.close_reason,
            "exit_code": exit_code,
            "dataset": {
                "path": str(stats.get("walmart/dataset_path", self.config.output_path)),
                "published": published,
                "record_count": records_in_dataset,
                "records_added_this_run": records_this_run,
                "sha256": stats.get("walmart/dataset_sha256", ""),
                "withheld_reason": stats.get(STAT_WITHHELD_REASON, ""),
            },
            "crawl": {
                "stores_verified": int(stats.get(STAT_STORE_VERIFIED, 0)),
                "stores_configured": len(self.config.stores),
                "categories_configured": len(self.config.categories),
                "listing_pages": int(stats.get(STAT_PAGES_CRAWLED, 0)),
                "blocked_pages": int(stats.get(STAT_BLOCKED_PAGES, 0)),
                "suspect_pages": int(stats.get(STAT_SUSPECT_PAGES, 0)),
                "retried_requests": int(stats.get("walmart/retried_requests", 0)),
                "challenged_responses": int(stats.get("walmart/challenge_responses", 0)),
                "checkpoints_saved": int(stats.get("walmart/checkpoints_saved", 0)),
            },
            "drop_reasons": {
                key.split("/", 1)[1]: int(value)
                for key, value in stats.items()
                if key.startswith("drop_reason/")
            },
            "elapsed_seconds": round(float(stats.get("elapsed_time_seconds", 0.0)), 2),
            "config": self.config.as_redacted_dict(),
        }

        ensure_dir(self.config.output_dir)
        manifest_path = Path(self.config.output_dir) / RUN_MANIFEST_NAME
        atomic_write_json(manifest_path, manifest)
        logger.info("run manifest written to %s (exit_code=%s)", manifest_path, exit_code)


# --------------------------------------------------------------------------- #
class AzureBlobUploadExtension:
    """Uploads the published dataset to Azure Blob Storage when configured."""

    def __init__(
        self,
        config: ScraperConfig,
        crawler: Any,
        container: str,
        connection_string: str,
        blob_prefix: str,
    ) -> None:
        self.config = config
        self.crawler = crawler
        self.container = container
        self.connection_string = connection_string
        self.blob_prefix = blob_prefix.strip("/")

    @classmethod
    def from_crawler(cls, crawler: Any) -> "AzureBlobUploadExtension":
        config = _config(crawler)
        connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        container = os.environ.get("AZURE_STORAGE_CONTAINER", "")
        if not connection or not container:
            raise NotConfigured("Azure Blob upload not configured")
        ext = cls(config, crawler, container, connection, os.environ.get("AZURE_BLOB_PREFIX", "walmart"))
        crawler.signals.connect(ext.engine_stopped, signal=signals.engine_stopped)
        return ext

    def engine_stopped(self) -> None:
        dataset = Path(self.crawler.stats.get_value("walmart/dataset_path", ""))
        if not dataset.exists():
            logger.warning("nothing published; skipping Azure upload")
            return
        try:
            from azure.storage.blob import BlobServiceClient  # optional dependency
        except ImportError:
            logger.error(
                "azure-storage-blob is not installed; dataset stays at %s. "
                "Install with: pip install azure-storage-blob", dataset,
            )
            return

        run_id = self.crawler.stats.get_value("walmart/run_id") or self.crawler.settings.get(
            "WMT_RUN_ID", "unknown"
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"{self.blob_prefix}/{stamp}/{run_id}/{dataset.name}".replace("//", "/")
        try:
            service = BlobServiceClient.from_connection_string(self.connection_string)
            container_client = service.get_container_client(self.container)
            container_client.upload_blob(
                name=blob_name,
                data=dataset.read_bytes(),
                overwrite=True,
                metadata={"run_id": run_id, "source": "walmart-scraper"},
            )
            self.crawler.stats.set_value("walmart/azure_blob", blob_name)
            logger.info("uploaded dataset to blob %s/%s", self.container, blob_name)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("Azure upload failed: %s", exc)
