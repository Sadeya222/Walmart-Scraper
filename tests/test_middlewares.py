"""Pacing, backoff and the retry middleware."""

from __future__ import annotations

import logging

import pytest
from scrapy import Request
from scrapy.http import HtmlResponse
from scrapy.settings import Settings

from walmart_scraper.middlewares import (
    WalmartBlockDetectionMiddleware,
    WalmartRetryMiddleware,
    abort_non_essential,
    backoff_delay,
    pacing_delay,
)


class FakeStats:
    def __init__(self):
        self.values: dict[str, int] = {}

    def inc_value(self, key, count=1):
        self.values[key] = self.values.get(key, 0) + count

    def get_value(self, key, default=0):
        return self.values.get(key, default)


class FakeCrawler:
    def __init__(self):
        self.stats = FakeStats()
        self.settings = Settings({"RETRY_PRIORITY_ADJUST": -1})


class FakeSpider:
    def __init__(self):
        self.crawler = FakeCrawler()
        self.logger = logging.getLogger("walmart.test")


class ImmediateReactor:
    """Stands in for the Twisted reactor so callLater fires synchronously."""

    def callLater(self, delay, fn, *args, **kwargs):  # noqa: N802 - Twisted API
        fn(*args, **kwargs)


@pytest.fixture()
def middleware():
    settings = Settings(
        {
            "RETRY_TIMES": 3,
            "RETRY_HTTP_CODES": [403, 429, 503],
            "WMT_RETRY_BASE_DELAY": 0.01,
            "WMT_RETRY_MAX_DELAY": 0.05,
            "WMT_RETRY_JITTER": 0.0,
        }
    )
    return WalmartRetryMiddleware(settings)


def _response(url, status=200, body=b"<html><body>ok</body></html>"):
    return HtmlResponse(url=url, status=status, body=body, encoding="utf-8")


def test_backoff_grows_exponentially_and_is_capped():
    assert backoff_delay(1, base=4, cap=90, jitter=0) == 4
    assert backoff_delay(2, base=4, cap=90, jitter=0) == 8
    assert backoff_delay(3, base=4, cap=90, jitter=0) == 16
    assert backoff_delay(10, base=4, cap=90, jitter=0) == 90


def test_backoff_jitter_stays_within_bounds():
    values = [backoff_delay(3, base=4, cap=90, jitter=0.35) for _ in range(200)]
    assert all(16 * 0.65 <= value <= 16 * 1.35 for value in values)
    assert len(set(values)) > 1


def test_backoff_never_returns_a_negative_delay():
    assert backoff_delay(1, base=0.1, cap=1, jitter=0.9) > 0


def test_pacing_delay_stays_in_range():
    values = [pacing_delay(2, 6) for _ in range(200)]
    assert all(2 <= value <= 6 for value in values)
    assert len(set(values)) > 1


def test_pacing_delay_tolerates_reversed_bounds():
    assert 2 <= pacing_delay(6, 2) <= 6


def test_retry_on_a_challenge_page_returns_a_new_request(middleware, monkeypatch):
    """Regression: passing max_retry_times to super()._retry raised TypeError,
    which silently disabled every retry."""
    monkeypatch.setattr("walmart_scraper.middlewares.reactor", ImmediateReactor())
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk")

    result = middleware._retry(request, "challenge_page", spider)

    outcome: list = []
    result.addCallback(outcome.append)
    assert outcome, "the retry deferred should already have fired"
    retried = outcome[0]
    assert isinstance(retried, Request)
    assert retried.meta["retry_times"] == 1
    assert retried.dont_filter is True
    assert spider.crawler.stats.get_value("walmart/retried_requests") == 1


def test_retry_gives_up_after_max_attempts(middleware, monkeypatch):
    monkeypatch.setattr("walmart_scraper.middlewares.reactor", ImmediateReactor())
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk", meta={"retry_times": 3})
    assert middleware._retry(request, "challenge_page", spider) is None


def test_challenge_response_is_retried_then_counted_as_blocked(middleware, monkeypatch):
    monkeypatch.setattr("walmart_scraper.middlewares.reactor", ImmediateReactor())
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk", meta={"retry_times": 3})
    response = _response(
        "https://www.walmart.com/search?q=milk",
        body=b"<html><body>Please verify you are human. Access denied.</body></html>",
    )
    result = middleware.process_response(request, response, spider)
    assert result is response
    assert spider.crawler.stats.get_value("walmart/challenge_responses") == 1
    assert spider.crawler.stats.get_value("walmart/blocked_pages") == 1


def test_throttle_status_is_counted_as_blocked(middleware, monkeypatch):
    monkeypatch.setattr("walmart_scraper.middlewares.reactor", ImmediateReactor())
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk", meta={"retry_times": 3})
    result = middleware.process_response(request, _response("https://w/x", status=429), spider)
    assert result.status == 429
    assert spider.crawler.stats.get_value("walmart/throttled_responses") == 1
    assert spider.crawler.stats.get_value("walmart/blocked_pages") == 1


def test_healthy_response_passes_through_untouched(middleware, monkeypatch):
    monkeypatch.setattr("walmart_scraper.middlewares.reactor", ImmediateReactor())
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk")
    response = _response("https://www.walmart.com/search?q=milk")
    assert middleware.process_response(request, response, spider) is response
    assert spider.crawler.stats.values == {}


def test_block_detection_counts_listing_pages():
    mw = WalmartBlockDetectionMiddleware()
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk", meta={"walmart_listing": True})
    mw.process_response(request, _response("https://w/x"), spider)
    assert spider.crawler.stats.get_value("walmart/pages_crawled") == 1
    assert spider.crawler.stats.get_value("walmart/suspect_pages") == 0


def test_block_detection_flags_non_200_listings():
    mw = WalmartBlockDetectionMiddleware()
    spider = FakeSpider()
    request = Request("https://www.walmart.com/search?q=milk", meta={"walmart_listing": True})
    mw.process_response(request, _response("https://w/x", status=500), spider)
    assert spider.crawler.stats.get_value("walmart/suspect_pages") == 1


@pytest.mark.parametrize(
    ("resource", "url", "blocked"),
    [
        ("image", "https://i5.walmartimages.com/x.jpeg", True),
        ("media", "https://x/y.mp4", True),
        ("font", "https://x/y.woff2", True),
        ("document", "https://www.walmart.com/search?q=milk", False),
        ("script", "https://www.google-analytics.com/analytics.js", True),
        ("script", "https://www.walmart.com/app.js", False),
    ],
)
def test_resource_filter(resource, url, blocked):
    class Req:
        resource_type = resource

    Req.url = url
    assert abort_non_essential(Req()) is blocked
