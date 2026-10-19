"""Downloader middleware: pacing, block detection, exponential-backoff retries.

Walmart is a single domain, so Scrapy's per-slot delay is not enough on its own:
a retry after a challenge page needs a *longer* wait than a normal request, and
the wait has to be jittered so parallel workers do not march in lockstep.

``request.meta['download_delay']`` is not honoured by Scrapy, so both pacing and
backoff are implemented the way the engine actually supports them: the middleware
returns a Twisted ``Deferred`` that fires the retry (or the original request)
after a computed delay.  The delay maths lives in :func:`backoff_delay` /
:func:`pacing_delay`, which are pure and unit tested.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.utils.response import response_status_message
from twisted.internet import defer, reactor

from .blocking import looks_like_challenge_page
from .constants import BLOCKING_STATUS_CODES
from .pipelines import STAT_BLOCKED_PAGES, STAT_PAGES_CRAWLED, STAT_SUSPECT_PAGES

__all__ = [
    "backoff_delay",
    "pacing_delay",
    "abort_non_essential",
    "WalmartPacingMiddleware",
    "WalmartRetryMiddleware",
    "WalmartBlockDetectionMiddleware",
]

logger = logging.getLogger("walmart.middlewares")


# --------------------------------------------------------------------------- #
# Pure delay maths
# --------------------------------------------------------------------------- #
def backoff_delay(
    attempt: int,
    *,
    base: float = 4.0,
    cap: float = 90.0,
    jitter: float = 0.35,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with proportional jitter: ``base * 2**(attempt-1)``.

    ``attempt`` is 1 for the first retry.  The result is capped at ``cap`` seconds
    and randomised by +/- ``jitter`` so concurrent workers desynchronise.
    """
    attempt = max(1, int(attempt))
    raw = min(cap, base * (2 ** (attempt - 1)))
    rng = rng or random
    spread = raw * jitter
    return max(0.5, rng.uniform(raw - spread, raw + spread))


def pacing_delay(
    minimum: float,
    maximum: float,
    *,
    rng: random.Random | None = None,
) -> float:
    """Uniform random pause between ``minimum`` and ``maximum`` seconds."""
    rng = rng or random
    low, high = (minimum, maximum) if minimum <= maximum else (maximum, minimum)
    return max(0.0, rng.uniform(low, high))


# --------------------------------------------------------------------------- #
# Playwright resource filter
# --------------------------------------------------------------------------- #
_BLOCKED_RESOURCE_TYPES = frozenset({"media", "font", "websocket", "manifest", "other"})
_BLOCKED_URL_MARKERS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "hotjar", "segment.io", "omtrdc", "akstat", "quantserve", "scorecardresearch",
    "bing.com/bat", "clarity.ms", "mparticle", "branch.io",
)


def abort_non_essential(request: Any) -> bool:
    """Playwright request predicate: skip assets and analytics we never parse.

    Cuts page weight and time substantially; product data lives in the HTML and
    the embedded ``__NEXT_DATA__`` payload, neither of which is blocked here.
    """
    resource_type = getattr(request, "resource_type", "") or ""
    if resource_type == "image":
        return True  # thumbnails are read from the payload, not downloaded
    if resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    url = (getattr(request, "url", "") or "").lower()
    return any(marker in url for marker in _BLOCKED_URL_MARKERS)


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #
class WalmartPacingMiddleware:
    """Adds a randomised pause before each real Walmart navigation.

    Only requests carrying ``meta["walmart_pacing"] = True`` are delayed, so
    lightweight follow-ups (robots, redirects) are not throttled.
    """

    def __init__(self, delay_min: float, delay_max: float, enabled: bool = True) -> None:
        self.delay_min = float(delay_min)
        self.delay_max = float(delay_max)
        self.enabled = bool(enabled)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "WalmartPacingMiddleware":
        return cls(
            crawler.settings.getfloat("WMT_CRAWL_DELAY_MIN", 2.0),
            crawler.settings.getfloat("WMT_CRAWL_DELAY_MAX", 6.0),
            crawler.settings.getbool("WMT_PACING_ENABLED", True),
        )

    def process_request(self, request: Any, spider: Any) -> Any:
        if not self.enabled or not request.meta.get("walmart_pacing"):
            return None
        delay = pacing_delay(self.delay_min, self.delay_max)
        request.meta["walmart_pacing_delay"] = round(delay, 3)
        spider.crawler.stats.inc_value("walmart/paced_requests")
        deferred: "defer.Deferred[Any]" = defer.Deferred()
        reactor.callLater(delay, deferred.callback, None)  # type: ignore[attr-defined]
        return deferred


# --------------------------------------------------------------------------- #
# Retries with exponential backoff
# --------------------------------------------------------------------------- #
class WalmartRetryMiddleware(RetryMiddleware):
    """Retries throttled/blocked requests with exponential backoff + jitter."""

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.backoff_base = settings.getfloat("WMT_RETRY_BASE_DELAY", 4.0)
        self.backoff_cap = settings.getfloat("WMT_RETRY_MAX_DELAY", 90.0)
        self.backoff_jitter = settings.getfloat("WMT_RETRY_JITTER", 0.35)
        self.challenge_status_retry = True

    # ------------------------------------------------------------------ #
    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        if response.status in BLOCKING_STATUS_CODES:
            spider.crawler.stats.inc_value("walmart/throttled_responses")
            retry = self._retry(request, f"http_{response.status}", spider)
            if retry is not None:
                return retry
            spider.crawler.stats.inc_value(STAT_BLOCKED_PAGES)
            return response

        if self.challenge_status_retry and _body_looks_challenged(response):
            spider.crawler.stats.inc_value("walmart/challenge_responses")
            retry = self._retry(request, "challenge_page", spider)
            if retry is not None:
                return retry
            spider.crawler.stats.inc_value(STAT_BLOCKED_PAGES)
            return response

        return super().process_response(request, response, spider)

    # ------------------------------------------------------------------ #
    def process_exception(self, request: Any, exception: Exception, spider: Any) -> Any:
        retry = self._retry(request, repr(exception), spider)
        return retry

    # ------------------------------------------------------------------ #
    def _retry(self, request: Any, reason: Any, spider: Any, *, max_retry_times: int | None = None) -> Any:
        # Scrapy's RetryMiddleware._retry takes no max_retry_times argument - it
        # reads the override from request.meta.  Passing it as a keyword raises
        # TypeError, which would silently break every retry.
        if max_retry_times is not None:
            request.meta.setdefault("max_retry_times", int(max_retry_times))
        retry_request = super()._retry(request, reason, spider)
        if retry_request is None:
            return None
        attempt = int(retry_request.meta.get("retry_times", 1))
        delay = backoff_delay(
            attempt,
            base=self.backoff_base,
            cap=self.backoff_cap,
            jitter=self.backoff_jitter,
        )
        retry_request.dont_filter = True
        retry_request.meta["walmart_retry_delay"] = round(delay, 3)
        spider.crawler.stats.inc_value("walmart/retried_requests")
        logger.warning(
            "retry %s/%s for %s (%s) in %.1fs",
            attempt, self.max_retry_times, request.url, reason, delay,
        )
        deferred: "defer.Deferred[Any]" = defer.Deferred()
        reactor.callLater(delay, deferred.callback, retry_request)  # type: ignore[attr-defined]
        return deferred


def _body_looks_challenged(response: Any) -> bool:
    """Cheap body sniff that avoids decoding multi-megabyte category pages."""
    try:
        body = response.body[:64_000]
    except Exception:  # pragma: no cover - defensive
        return False
    return looks_like_challenge_page(body.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# Block bookkeeping
# --------------------------------------------------------------------------- #
class WalmartBlockDetectionMiddleware:
    """Records page-level outcomes so the validation gate can judge the run.

    Content-level grading (item counts, canaries) happens in the spider because it
    needs the parsed payload; this middleware owns the transport-level view.
    """

    def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        if not request.meta.get("walmart_listing"):
            return response
        spider.crawler.stats.inc_value(STAT_PAGES_CRAWLED)
        if response.status != 200:
            spider.crawler.stats.inc_value(STAT_SUSPECT_PAGES)
            logger.warning(
                "non-200 listing response %s for %s",
                response_status_message(response.status), request.url,
            )
        return response

    def process_exception(self, request: Any, exception: Exception, spider: Any) -> Any:
        if request.meta.get("walmart_listing"):
            spider.crawler.stats.inc_value(STAT_SUSPECT_PAGES)
        if isinstance(exception, IgnoreRequest):
            raise
        logger.warning("request failed: %s (%s)", request.url, exception)
        return None
