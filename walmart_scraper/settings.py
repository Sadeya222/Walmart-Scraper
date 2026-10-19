"""Scrapy settings, driven entirely by environment variables / ``.env``.

Nothing secret is hard-coded: proxy URLs, storage credentials and any token come
from the environment (Azure app settings or ``.env`` locally).  The whole
:class:`ScraperConfig` object is loaded once here and exposed to spiders,
pipelines and extensions as ``WMT_CONFIG``, so there is a single place where
configuration is read and validated.
"""

from __future__ import annotations

import os

from .config_loader import ScraperConfig

# --------------------------------------------------------------------------- #
# Configuration object (validated at import time -> fail fast with exit code 2)
# --------------------------------------------------------------------------- #
WMT_CONFIG: ScraperConfig = ScraperConfig.from_env()

BOT_NAME = "walmart_scraper"
SPIDER_MODULES = ["walmart_scraper.spiders"]
NEWSPIDER_MODULE = "walmart_scraper.spiders"

# --------------------------------------------------------------------------- #
# Crawl shape / politeness
# --------------------------------------------------------------------------- #
ROBOTSTXT_OBEY = False  # see README "Legal and responsible use"
# Walmart is a single domain, so per-domain concurrency is the meaningful knob.
# CONCURRENT_REQUESTS_PER_IP is deliberately NOT set: Scrapy rejects it alongside
# the downloader-aware priority queue that per-domain concurrency selects.
CONCURRENT_REQUESTS = WMT_CONFIG.concurrency
CONCURRENT_REQUESTS_PER_DOMAIN = WMT_CONFIG.concurrency
DOWNLOAD_TIMEOUT = WMT_CONFIG.download_timeout
DOWNLOAD_DELAY = WMT_CONFIG.crawl_delay_min
RANDOMIZE_DOWNLOAD_DELAY = True
COOKIES_ENABLED = True
COOKIES_DEBUG = False
TELNETCONSOLE_ENABLED = False
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = max(2.0, WMT_CONFIG.crawl_delay_min)
AUTOTHROTTLE_MAX_DELAY = 120.0
AUTOTHROTTLE_TARGET_CONCURRENCY = float(WMT_CONFIG.concurrency)
AUTOTHROTTLE_DEBUG = False

WMT_CRAWL_DELAY_MIN = WMT_CONFIG.crawl_delay_min
WMT_CRAWL_DELAY_MAX = WMT_CONFIG.crawl_delay_max
WMT_PACING_ENABLED = os.environ.get("WMT_PACING_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
# Set to false to run a plain-HTTP smoke crawl (no browser).  Production runs
# need the browser; the integration suite uses this to exercise every layer
# below it.  NOTE: Scrapy settings do not read os.environ implicitly, so every
# environment-driven knob must be declared here explicitly.
WMT_USE_PLAYWRIGHT = os.environ.get("WMT_USE_PLAYWRIGHT", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
# Comma separated.  walmart.com only by default; set to "walmart.ca" or a staging
# host to retarget the crawl without a code change.
WMT_ALLOWED_DOMAINS = [
    part.strip()
    for part in os.environ.get("WMT_ALLOWED_DOMAINS", "walmart.com").split(",")
    if part.strip()
]

RETRY_ENABLED = True
RETRY_TIMES = int(os.environ.get("WMT_RETRY_TIMES", "4"))
RETRY_HTTP_CODES = [403, 408, 412, 429, 500, 502, 503, 504, 522, 524]
WMT_RETRY_BASE_DELAY = float(os.environ.get("WMT_RETRY_BASE_DELAY", "4"))
WMT_RETRY_MAX_DELAY = float(os.environ.get("WMT_RETRY_MAX_DELAY", "90"))
WMT_RETRY_JITTER = float(os.environ.get("WMT_RETRY_JITTER", "0.35"))

# --------------------------------------------------------------------------- #
# Browser / Playwright
# --------------------------------------------------------------------------- #
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

PLAYWRIGHT_BROWSER_TYPE = WMT_CONFIG.browser_type
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": WMT_CONFIG.headless,
    "timeout": 60_000,
    "args": [
        "--no-sandbox",
        "--disable-dev-shm-usage",       # required in containers with small /dev/shm
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu",
    ],
}
if WMT_CONFIG.proxy_url:
    PLAYWRIGHT_LAUNCH_OPTIONS["proxy"] = {"server": WMT_CONFIG.proxy_url}

PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = float(os.environ.get("WMT_NAV_TIMEOUT", "60000"))
PLAYWRIGHT_DEFAULT_PAGE_TIMEOUT = float(os.environ.get("WMT_PAGE_TIMEOUT", "30000"))
PLAYWRIGHT_ABORT_REQUEST = "walmart_scraper.middlewares.abort_non_essential"
PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = int(os.environ.get("WMT_MAX_PAGES_PER_CONTEXT", "8"))
PLAYWRIGHT_MAX_CONTEXTS = int(os.environ.get("WMT_MAX_CONTEXTS", "4"))

# --------------------------------------------------------------------------- #
# Pipelines / middlewares / extensions
# --------------------------------------------------------------------------- #
ITEM_PIPELINES = {
    "walmart_scraper.pipelines.WalmartPolicyPipeline": 100,
    "walmart_scraper.pipelines.WalmartDedupePipeline": 200,
    "walmart_scraper.pipelines.WalmartAtomicWriterPipeline": 900,
}

DOWNLOADER_MIDDLEWARES = {
    # Replace Scrapy's retry middleware with the backoff-aware one.
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
    "walmart_scraper.middlewares.WalmartRetryMiddleware": 550,
    "walmart_scraper.middlewares.WalmartBlockDetectionMiddleware": 560,
    "walmart_scraper.middlewares.WalmartPacingMiddleware": 570,
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
}

EXTENSIONS = {
    "walmart_scraper.extensions.RunLifecycleExtension": 100,
    "walmart_scraper.extensions.RunManifestExtension": 200,
    "walmart_scraper.extensions.AzureBlobUploadExtension": 900,
}

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOG_LEVEL = os.environ.get("WMT_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
LOG_DATEFORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOG_SHORT_NAMES = False
LOG_FILE = os.environ.get("WMT_LOG_FILE") or None

# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
ROBOTSTXT_USER_AGENT = None
USER_AGENT = WMT_CONFIG.user_agent or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
WMT_STORAGE_STATE = os.environ.get("WMT_STORAGE_STATE", "")
