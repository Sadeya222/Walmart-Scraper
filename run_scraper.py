#!/usr/bin/env python3
"""Startup runner: validates config, sets up logging, runs the crawl, reports.

This is the single entry point used locally, in Docker and on Azure.  It

* resolves configuration from ``.env`` / environment (CLI flags are translated
  into the same environment variables, so there is exactly one config path),
* configures logging to console and optionally to a file,
* loads or resets the resume checkpoint,
* runs the ``walmart_catalog`` spider in a ``CrawlerProcess``,
* prints a summary of what was published or withheld, and
* exits with a meaningful status code so Azure can alert or retry:

    0  dataset published            3  run aborted, blocking detected
    1  unexpected error             4  dataset withheld (previous one retained)
    2  configuration error          5  run published but produced no data
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from walmart_scraper.config_loader import ConfigError, ScraperConfig
from walmart_scraper.constants import (
    EXIT_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_GENERIC_ERROR,
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_VALIDATION_FAILED,
)
from walmart_scraper.items import crawl_date
from walmart_scraper.state import RunState

EXIT_LABELS = {
    EXIT_OK: "OK - dataset published",
    EXIT_GENERIC_ERROR: "ERROR - unexpected failure",
    EXIT_CONFIG_ERROR: "ERROR - configuration invalid",
    EXIT_BLOCKED: "BLOCKED - run aborted, scraping was blocked",
    EXIT_VALIDATION_FAILED: "WITHHELD - validation failed, previous dataset retained",
    EXIT_NO_DATA: "EMPTY - run completed but produced no records",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_scraper.py",
        description="Walmart location-specific catalog scraper (Scrapy + Playwright).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--categories", default=None, metavar="CSV",
                        help="Path to the category URL CSV (WMT_CATEGORY_CSV).")
    parser.add_argument("--stores", default=None, metavar="CSV",
                        help="Path to the store locations CSV (WMT_STORES_CSV).")
    parser.add_argument("--store", action="append", default=[], metavar="ID",
                        help="Only crawl this store id (repeatable).")
    parser.add_argument("--category", action="append", default=[], metavar="SUBSTRING",
                        help="Only crawl categories whose URL/label contains this (repeatable).")
    parser.add_argument("--output-dir", default=None, help="Where the dataset is written.")
    parser.add_argument("--pages", type=int, default=None,
                        help="Override the maximum pages per category.")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Override concurrent browser pages.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from the last checkpoint instead of starting fresh.")
    parser.add_argument("--reset-state", action="store_true",
                        help="Delete the checkpoint file before running.")
    parser.add_argument("--no-pdp", action="store_true",
                        help="Skip product detail pages (faster, but no UPC/weight).")
    parser.add_argument("--no-scroll", action="store_true", help="Disable infinite scroll.")
    parser.add_argument("--min-records", type=int, default=None,
                        help="Minimum records required to publish a dataset.")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Console log level.")
    parser.add_argument("--log-file", default=None, help="Also write logs to this file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate configuration and print the crawl plan, then exit.")
    return parser


def apply_cli_to_env(args: argparse.Namespace) -> None:
    """Translate flags into environment variables before settings are loaded."""
    mapping = {
        "WMT_CATEGORY_CSV": args.categories,
        "WMT_STORES_CSV": args.stores,
        "WMT_OUTPUT_DIR": args.output_dir,
        "WMT_LOG_LEVEL": args.log_level,
        "WMT_LOG_FILE": args.log_file,
        "WMT_MAX_PAGES_PER_CATEGORY": args.pages,
        "WMT_CONCURRENCY": args.concurrency,
        "WMT_MIN_RECORDS_PER_RUN": args.min_records,
    }
    for key, value in mapping.items():
        if value is not None:
            os.environ[key] = str(value)
    if args.resume:
        os.environ["WMT_RESUME"] = "true"
    if args.no_pdp:
        os.environ["WMT_VISIT_PDP"] = "false"
    if args.no_scroll:
        os.environ["WMT_SCROLL_ENABLED"] = "false"


def select_stores(config: ScraperConfig, store_ids: list[str]) -> None:
    if not store_ids:
        return
    wanted = {store_id.strip() for store_id in store_ids}
    config.stores = [store for store in config.stores if store.store_id in wanted]
    if not config.stores:
        raise ConfigError(f"no configured store matches --store {sorted(wanted)}")


def select_categories(config: ScraperConfig, needles: list[str]) -> None:
    if not needles:
        return
    lowered = [needle.lower() for needle in needles]
    config.categories = [
        target
        for target in config.categories
        if any(needle in target.url.lower() or needle in target.label.lower() for needle in lowered)
    ]
    if not config.categories:
        raise ConfigError(f"no configured category matches --category {needles}")


def print_plan(config: ScraperConfig, state: RunState) -> None:
    print("=" * 78)
    print(f"Walmart catalog scraper - crawl plan ({len(config.stores)} store(s) x "
          f"{len(config.categories)} categor(ies) = "
          f"{len(config.stores) * len(config.categories)} listing sequence(s))")
    print("=" * 78)
    for store in config.stores:
        print(f"  store {store.store_id}  ZIP {store.zipcode}  {store.label}")
        print(f"         {store.store_location or '(no address configured)'}")
    print("-" * 78)
    for target in config.categories:
        canary = f"  canary={','.join(target.canary_ids)}" if target.canary_ids else ""
        print(f"  {target.label}")
        print(f"         {target.url}  (max {target.max_pages} pages{canary})")
    print("-" * 78)
    print(f"  output          : {config.output_path}")
    print(f"  quarantine      : {config.quarantine_dir}/")
    print(f"  checkpoint      : {config.state_path} (resume={state.resume}, "
          f"{len(state.completed_listings)} listings already done)")
    print(f"  publish requires: >= {config.min_records_per_run} records, "
          f"<= {config.max_suspect_ratio:.0%} suspect pages, "
          f"<= {config.max_blocked_pages} blocked pages")
    print(f"  policy          : marketplace={'excluded' if config.exclude_marketplace else 'kept'}, "
          f"shipping-only={config.shipping_only_mode}, price_format={config.price_format}, "
          f"coupon_price_from_discount={config.apply_coupon_discount}")
    print("=" * 78)


def configure_logging(config: ScraperConfig, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=os.environ.get("WMT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply_cli_to_env(args)

    # Imported *after* the env is finalised: the settings module reads config here.
    from scrapy.utils.project import get_project_settings

    logger = logging.getLogger("walmart.runner")
    configure_logging(None, args.log_file)

    try:
        settings = get_project_settings()
        config: ScraperConfig = settings.get("WMT_CONFIG")
        select_stores(config, args.store)
        select_categories(config, args.category)
        config.validate_policy_values()

        state_path = config.state_path
        if args.reset_state and state_path.exists():
            state_path.unlink()
            logger.info("removed checkpoint %s", state_path)
        state = RunState.load(state_path, resume=args.resume)

        if args.dry_run:
            print_plan(config, state)
            return EXIT_OK

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        configure_logging(config, args.log_file)
        settings.set("WMT_CONFIG", config, priority="project")
        settings.set("WMT_STATE", state, priority="project")
        settings.set("WMT_RUN_ID", run_id, priority="project")
        settings.set("WMT_RUN_DATE", crawl_date(config.crawl_date_format), priority="project")

        from scrapy.crawler import CrawlerProcess

        logger.info("run %s starting (resume=%s)", run_id, state.resume)
        process = CrawlerProcess(settings=settings)
        # Create the crawler explicitly and keep the reference: reading it back
        # out of process.crawlers after start() is not reliable across versions.
        crawler = process.create_crawler("walmart_catalog")
        process.crawl(crawler)
        process.start()  # blocks until the crawl finishes

        stats = crawler.stats
        exit_code = int(stats.get_value("walmart/exit_code", EXIT_OK))
        records = int(stats.get_value("item_scraped_count", 0))
        drops = {
            key.split("/", 1)[1]: int(value)
            for key, value in stats.get_stats().items()
            if key.startswith("drop_reason/")
        }

        print()
        print("-" * 78)
        print(f"  outcome          : {EXIT_LABELS.get(exit_code, exit_code)} (exit {exit_code})")
        print(f"  records emitted  : {records}")
        print(f"  listing pages    : {stats.get_value('walmart/pages_crawled', 0)}")
        print(f"  blocked pages    : {stats.get_value('walmart/blocked_pages', 0)}")
        print(f"  suspect pages    : {stats.get_value('walmart/suspect_pages', 0)}")
        print(f"  retries          : {stats.get_value('walmart/retried_requests', 0)}")
        print(f"  stores verified  : {stats.get_value('walmart/stores_verified', 0)}"
              f"/{len(config.stores)}")
        if drops:
            print(f"  dropped records  : {sum(drops.values())} -> {drops}")
        if exit_code == EXIT_OK:
            print(f"  dataset          : {stats.get_value('walmart/dataset_path', config.output_path)}")
            print(f"  sha256           : {stats.get_value('walmart/dataset_sha256', '')}")
        elif exit_code in {EXIT_VALIDATION_FAILED, EXIT_BLOCKED}:
            print(f"  withheld because : {stats.get_value('walmart/withheld_reason', 'n/a')}")
            print("  the previous dataset was left untouched")
        print(f"  manifest         : {Path(config.output_dir) / 'run_manifest.json'}")
        print("-" * 78)
        return exit_code

    except ConfigError as exc:
        logging.getLogger("walmart.runner").error("configuration error: %s", exc)
        return EXIT_CONFIG_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted - checkpoint saved, re-run with --resume to continue", file=sys.stderr)
        return EXIT_GENERIC_ERROR
    except Exception as exc:  # pragma: no cover - unexpected
        logging.getLogger("walmart.runner").exception("unexpected failure: %s", exc)
        return EXIT_GENERIC_ERROR


if __name__ == "__main__":
    sys.exit(main())
