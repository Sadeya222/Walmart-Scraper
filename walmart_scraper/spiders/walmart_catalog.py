"""Location-specific Walmart catalog spider.

Crawl shape (per store, per category):

1. **Setup** - the first listing request doubles as the store-context check: the
   browser context is seeded with the store's cookies, the rendered store
   indicator is compared with the configured store, and if it does not match the
   store picker is driven with Playwright (``store_context.select_store_via_ui``).
   A store that still cannot be verified is skipped rather than crawled, because
   every row for it would carry the wrong location.
2. **Listing** - the category page is parsed from its embedded ``__NEXT_DATA__``
   payload (DOM selectors are the fallback), graded for silent blocking, then
   deep pagination (``?page=N``) plus optional infinite-scroll is followed.
3. **PDP** - each product's detail page is fetched once per store to pick up the
   UPC/GTIN, weight, brand and price-per-measure that tiles do not carry.
4. **Emit** - tile + PDP data are merged, offers are classified, and a
   ``ProductRecord`` is handed to the pipeline chain.

Everything that can be skipped is: completed listings and fetched products are
recorded in :class:`RunState` so an interrupted run resumes instead of repeating.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import scrapy
from scrapy_playwright.page import PageMethod

from ..blocking import BlockingDetector
from ..config_loader import CategoryTarget, ScraperConfig, StoreConfig
from ..items import ProductRecord, assemble_record, crawl_date, deals_for
from ..parsing import (
    ParsedProduct,
    extract_listing_products,
    extract_pdp_product,
    merge_products,
)
from ..pipelines import STAT_BLOCKED_PAGES, STAT_STORE_VERIFIED, STAT_SUSPECT_PAGES
from ..state import RunState, listing_key, product_key
from ..store_context import (
    StoreVerifier,
    playwright_context_kwargs,
    select_store_via_ui,
    store_cookies,
)
from ..text import clean_text

logger = logging.getLogger("walmart.spider")

WALMART_HOME = "https://www.walmart.com/"


class WalmartCatalogSpider(scrapy.Spider):
    """Crawls configured category URLs for configured store locations."""

    name = "walmart_catalog"
    allowed_domains = ["walmart.com"]

    def __init__(
        self,
        *args: Any,
        config: ScraperConfig | None = None,
        state: RunState | None = None,
        run_date: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.config: ScraperConfig | None = config
        self.state: RunState | None = state
        self.run_date = run_date
        self._verified_stores: set[str] = set()
        self._skipped_stores: set[str] = set()
        self._setup_started: set[str] = set()
        self._pdp_budget: dict[str, int] = {}
        self.storage_state_path: str = ""
        self._detector: BlockingDetector | None = None
        self._verifier: StoreVerifier | None = None
        # ``use_playwright`` is a real operational switch: production runs need the
        # browser, but a plain-HTTP smoke crawl (or the integration test suite)
        # can exercise every layer below the browser without one.
        self.use_playwright: bool = True

    # ------------------------------------------------------------------ #
    @classmethod
    def from_crawler(cls, crawler: Any, *args: Any, **kwargs: Any) -> "WalmartCatalogSpider":
        spider = super().from_crawler(crawler, *args, **kwargs)
        settings = crawler.settings
        spider.config = settings.get("WMT_CONFIG")
        spider.state = settings.get("WMT_STATE")
        spider.run_date = settings.get("WMT_RUN_DATE") or crawl_date(
            getattr(spider.config, "crawl_date_format", "date")
        )
        spider.storage_state_path = settings.get("WMT_STORAGE_STATE", "") or ""
        spider.use_playwright = settings.getbool("WMT_USE_PLAYWRIGHT", True)
        spider.allowed_domains = settings.getlist("WMT_ALLOWED_DOMAINS", ["walmart.com"])
        spider._detector = BlockingDetector(
            min_items=spider.config.min_items_per_page,
            min_body_bytes=spider.config.min_body_bytes,
        )
        spider._verifier = StoreVerifier(
            tuple((getattr(spider.config, "selectors", {}) or {}).get("store_indicator", ()))
        )
        return spider

    # ------------------------------------------------------------------ #
    async def start(self) -> Any:
        """Entry point (Scrapy >= 2.13 renamed ``start_requests`` to ``start``)."""
        assert self.config is not None and self.state is not None, "spider not wired to a crawler"
        logger.info(
            "crawling %s categor%s x %s store(s); resume=%s",
            len(self.config.categories),
            "y" if len(self.config.categories) == 1 else "ies",
            len(self.config.stores),
            self.state.resume,
        )
        for store in self.config.stores:
            first = self.config.categories[0]
            self._setup_started.add(store.store_id)
            yield self._listing_request(store, first, page=1, verify_store=True)

    # ------------------------------------------------------------------ #
    # Request builders
    # ------------------------------------------------------------------ #
    def _context_kwargs(self, store: StoreConfig) -> dict[str, Any]:
        """Isolated browser context per store, seeded with its location cookies."""
        assert self.config is not None
        kwargs = playwright_context_kwargs(
            store,
            user_agent=self.config.user_agent,
            storage_state_path=self.storage_state_path,
            proxy=self.config.proxy_url,
        )
        kwargs["cookies"] = store_cookies(store)
        return kwargs

    def _scroll_methods(self) -> list[PageMethod]:
        """Scroll to trigger lazy loading, then settle.

        Infinite-scroll grids only render tiles after the viewport moves, so the
        page is scrolled in viewport-sized steps with a short settle between
        steps; the final wait lets the last batch of tiles hydrate.
        """
        if not (self.config and self.config.scroll_enabled) or not self.use_playwright:
            return []
        rounds = max(1, self.config.scroll_max_rounds)
        methods: list[PageMethod] = [
            PageMethod("wait_for_load_state", "domcontentloaded"),
        ]
        for _ in range(rounds):
            methods.append(
                PageMethod(
                    "evaluate",
                    "() => new Promise((resolve) => {"
                    " window.scrollBy(0, window.innerHeight * 2);"
                    " setTimeout(resolve, 900);"
                    "})",
                )
            )
        methods.append(PageMethod("evaluate", "() => window.scrollTo(0, document.body.scrollHeight)"))
        methods.append(PageMethod("wait_for_timeout", 1500))
        return methods

    def _listing_request(
        self,
        store: StoreConfig,
        target: CategoryTarget,
        page: int,
        *,
        verify_store: bool = False,
    ) -> scrapy.Request:
        url = _with_page_param(target.url, page)
        key = listing_key(store.store_id, target.url, page)
        meta: dict[str, Any] = {
            "playwright": self.use_playwright,
            "playwright_context": f"store-{store.store_id}",
            "playwright_context_kwargs": self._context_kwargs(store),
            "playwright_page_methods": self._scroll_methods(),
            "playwright_include_page": verify_store,
            "walmart_listing": True,
            "walmart_pacing": True,
            "store": store,
            "target": target,
            "page": page,
            "listing_url": target.url,
            "verify_store": verify_store,
            "state_key": key,
            "max_retry_times": int((self.crawler.settings.getint("RETRY_TIMES", 4) if self.crawler else 4)),
        }
        return scrapy.Request(
            url,
            callback=self.parse_listing,
            errback=self.on_error,
            meta=meta,
            dont_filter=True,
            priority=10,
        )

    def _pdp_request(
        self, product: ParsedProduct, store: StoreConfig, target: CategoryTarget, page: int
    ) -> scrapy.Request:
        return scrapy.Request(
            product.url,
            callback=self.parse_pdp,
            errback=self.on_error,
            meta={
                "playwright": self.use_playwright,
                "playwright_context": f"store-{store.store_id}",
                "playwright_context_kwargs": self._context_kwargs(store),
                "playwright_page_methods": [PageMethod("wait_for_load_state", "domcontentloaded")],
                "walmart_pacing": True,
                "store": store,
                "target": target,
                "page": page,
            },
            cb_kwargs={"listing_product": product},
            dont_filter=True,
            priority=5,
        )

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #
    async def parse_listing(self, response: scrapy.http.Response, **kwargs: Any) -> Any:
        store: StoreConfig = response.meta["store"]
        target: CategoryTarget = response.meta["target"]
        page: int = response.meta["page"]
        assert self.config is not None and self.state is not None
        assert self._detector is not None

        if response.meta.get("verify_store"):
            if not await self._ensure_store_context(response, store):
                return

        # Resume: the store still has to be verified (that is why the page was
        # fetched), but a listing already completed in an earlier run must not be
        # re-processed - that would re-fetch every PDP for nothing.
        if self.state.is_listing_done(response.meta["state_key"]):
            logger.info("resume: listing %s page %s already done, skipping", target.label, page)
            self.crawler.stats.inc_value("walmart/listings_skipped_on_resume")
            return

        payload = BlockingDetector.extract_next_data(response.text)
        # Resolve relative canonical URLs against the page's own origin so the
        # crawl follows walmart.ca / staging hosts as well as walmart.com.
        products = extract_listing_products(payload, base_url=response.url)
        canaries = target.canary_ids
        report = self._detector.grade(
            url=response.url,
            status=response.status,
            body=response.text,
            item_count=len(products),
            item_ids=[product.us_item_id for product in products],
            canary_ids=canaries,
            payload_parsed=payload is not None,
        )

        if report.blocked:
            self.crawler.stats.inc_value(STAT_BLOCKED_PAGES)
            self.state.note_blocked(response.url)
            logger.error("BLOCKED listing %s -> %s", response.url, report.summary())
            return
        if report.suspect:
            self.crawler.stats.inc_value(STAT_SUSPECT_PAGES)
            logger.warning("SUSPECT listing %s -> %s", response.url, report.summary())

        self.state.mark_listing_done(response.meta["state_key"])

        emitted = 0
        for product in products:
            if not product.identity:
                continue
            dedupe_id = f"{store.zipcode}:{product.identity}"
            if self.state.already_seen(dedupe_id):
                self.crawler.stats.inc_value("drop_reason/duplicate_across_pages")
                continue

            pdp_key = product_key(store.store_id, product.us_item_id or product.url)
            budget_key = f"{store.store_id}:{target.url}"
            budget_left = self.config.max_pdp_per_category - self._pdp_budget.get(budget_key, 0)

            if self.config.visit_pdp and product.url and budget_left > 0:
                if self.state.is_product_done(pdp_key):
                    yield self._record(product, store, target, page)
                else:
                    self._pdp_budget[budget_key] = self._pdp_budget.get(budget_key, 0) + 1
                    yield self._pdp_request(product, store, target, page)
            else:
                yield self._record(product, store, target, page)
            self.state.note_item(dedupe_id)
            emitted += 1

        self.crawler.stats.inc_value("walmart/listing_products", emitted)
        if not products and "empty_page_end_of_pagination" in report.reasons:
            logger.info("listing %s page %s is empty - end of pagination", target.label, page)
        else:
            logger.info(
                "listing %s page %s -> %s products (suspect=%s reasons=%s)",
                target.label, page, emitted, report.suspect, report.summary(),
            )

        next_page = page + 1
        if next_page <= min(target.max_pages, self.config.max_pages_per_category) and products:
            next_key = listing_key(store.store_id, target.url, next_page)
            if self.state.is_listing_done(next_key):
                return
            yield self._listing_request(store, target, next_page)

    # ------------------------------------------------------------------ #
    async def _ensure_store_context(
        self, response: scrapy.http.Response, store: StoreConfig
    ) -> bool:
        """Verify - and if necessary repair - the store context for this store."""
        assert self.config is not None and self._verifier is not None
        if store.store_id in self._verified_stores:
            return True
        if store.store_id in self._skipped_stores:
            return False

        page = response.meta.get("playwright_page")
        mode = self.config.store_verification_mode

        # The location Walmart resolved into the payload is the strongest
        # signal: it is the store this page was actually priced for, and it is
        # present even when the header indicator is not rendered.  The rendered
        # indicator text is the fallback for pages with no location in payload.
        verification = self._verifier.verify_from_payload(
            BlockingDetector.extract_next_data(response.text), store
        )
        if not verification.verified:
            verification = self._verifier.verify_from_text(_indicator_text(response), store)

        if not verification.verified and page is not None and mode in {"ui", "cookie_then_ui"}:
            logger.warning(
                "store %s indicator mismatch (%s); driving the store picker",
                store.store_id, verification.detail,
            )
            selected = await select_store_via_ui(page, store, self.config.selectors)
            if selected:
                verification = self._verifier.verify_from_text(_indicator_text(response), store)
            else:
                verification.detail = f"ui_selection_failed:{verification.detail}"

        if page is not None:
            try:
                await page.close()
            except Exception:  # pragma: no cover - live browser only
                pass

        if verification.verified:
            self._verified_stores.add(store.store_id)
            self.crawler.stats.inc_value(STAT_STORE_VERIFIED)
            logger.info(
                "store %s verified via %s (%s)",
                store.store_id, verification.matched_on, verification.indicator_text,
            )
            return True

        if self.config.require_store_verified:
            self._skipped_stores.add(store.store_id)
            self.crawler.stats.inc_value("walmart/stores_skipped_unverified")
            logger.critical(
                "SKIPPING store %s (ZIP %s): store context could not be verified (%s). "
                "No rows are emitted for this store so the dataset cannot contain "
                "wrong-location prices.",
                store.store_id, store.zipcode, verification.detail,
            )
            return False

        logger.warning(
            "store %s unverified but WMT_REQUIRE_STORE_VERIFIED=false; continuing",
            store.store_id,
        )
        self._verified_stores.add(store.store_id)
        return True

    # ------------------------------------------------------------------ #
    # PDP
    # ------------------------------------------------------------------ #
    def parse_pdp(
        self,
        response: scrapy.http.Response,
        listing_product: ParsedProduct,
        **kwargs: Any,
    ) -> Iterable[Any]:
        store: StoreConfig = response.meta["store"]
        target: CategoryTarget = response.meta["target"]
        assert self.state is not None and self._detector is not None

        payload = BlockingDetector.extract_next_data(response.text)
        pdp_product = extract_pdp_product(payload, base_url=response.url)

        if pdp_product is None:
            # A PDP that will not parse is a block symptom, not a data point.
            self.crawler.stats.inc_value("walmart/pdp_unparsable")
            logger.warning("PDP unparsable %s (status=%s)", response.url, response.status)
            if response.status == 200 and len(response.text) < 20_000:
                self.crawler.stats.inc_value(STAT_SUSPECT_PAGES)
            merged = listing_product
        else:
            merged = merge_products(listing_product, pdp_product)

        self.state.mark_product_done(product_key(store.store_id, merged.us_item_id or merged.url))
        yield self._record(merged, store, target, int(response.meta.get("page", 1)))

    # ------------------------------------------------------------------ #
    def _record(
        self, product: ParsedProduct, store: StoreConfig, target: CategoryTarget, page: int
    ) -> ProductRecord:
        assert self.config is not None
        deals = deals_for(
            product,
            prefer_each_price=self.config.prefer_each_price,
            apply_coupon_discount=self.config.apply_coupon_discount,
            include_manufacturer=self.config.include_manufacturer_offers,
        )
        item = assemble_record(
            product=product,
            store_zipcode=store.zipcode,
            store_name=store.label,
            store_location=store.store_location,
            store_logo=store.store_logo,
            category=target.category,
            sub_category=target.sub_category,
            deals=deals,
            date_stamp=self.run_date,
            extra_private_labels=self.config.extra_private_labels,
            price_format=self.config.price_format,
        )
        return ProductRecord(
            item=item,
            product=product,
            store_id=store.store_id,
            category_url=target.url,
            listing_url=target.url,
            page=page,
            sponsored=product.sponsored,
        )

    # ------------------------------------------------------------------ #
    def on_error(self, failure: Any) -> None:
        request = getattr(failure, "request", None)
        url = getattr(request, "url", "<unknown>")
        # failure.value can be None for some Twisted failures.
        error = getattr(failure, "value", None)
        logger.warning("request failed: %s (%s)", url, type(error).__name__ if error else "unknown")
        self.crawler.stats.inc_value("walmart/request_errors")
        if request is not None and request.meta.get("walmart_listing"):
            self.crawler.stats.inc_value(STAT_SUSPECT_PAGES)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _with_page_param(url: str, page: int) -> str:
    """Append/replace Walmart's ``page`` query parameter."""
    if page <= 1:
        return url
    parts = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
    query.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(query)))


def _indicator_text(response: scrapy.http.Response) -> str:
    """Best-effort store indicator text from a rendered page.

    Reads the store picker control's text or ``aria-label``; returns ``""`` when
    Walmart's markup does not expose one, which the verifier reports as
    "no_store_indicator_found" rather than treating as a match.
    """
    selectors = [
        "[data-testid='store-picker-trigger']::text",
        "button[aria-label*='Store']::attr(aria-label)",
        "[data-automation-id='store-picker']::text",
        "#store-picker-trigger::text",
    ]
    for selector in selectors:
        try:
            value = response.css(selector).get()
        except Exception:  # pragma: no cover - parsel selector edge cases
            continue
        text = clean_text(value)
        if text:
            return text
    return ""
