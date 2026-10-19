"""Silent block detection and thin-result validation."""

from __future__ import annotations

import pytest

from walmart_scraper.blocking import (
    SEVERITY_BLOCKED,
    SEVERITY_OK,
    SEVERITY_SUSPECT,
    BlockingDetector,
    looks_like_challenge_page,
)

HEALTHY_PADDING = "x" * 40_000


def healthy_page(item_ids: list[str]) -> str:
    payload = '{"props":{"pageProps":{"initialData":{"searchResult":{"itemStacks":[]}}}}}'
    tiles = "".join(f'<div data-item-id="{item}"></div>' for item in item_ids)
    return (
        "<!DOCTYPE html><html><body>"
        f"{tiles}<!-- {HEALTHY_PADDING} -->"
        f'<script id="__NEXT_DATA__">{payload}</script>'
        "</body></html>"
    )


@pytest.fixture()
def detector():
    return BlockingDetector(min_items=5, min_body_bytes=20_000)


def test_healthy_page_is_ok(detector):
    report = detector.grade(url="u", status=200, body=healthy_page(["1"] * 10), item_count=10)
    assert report.severity == SEVERITY_OK
    assert report.ok


@pytest.mark.parametrize("status", [403, 412, 429, 503, 999])
def test_block_statuses_are_hard_blocks(detector, status):
    report = detector.grade(url="u", status=status, body=healthy_page(["1"] * 10), item_count=10)
    assert report.severity == SEVERITY_BLOCKED
    assert f"http_status_{status}" in report.summary()


def test_challenge_page_is_blocked(detector):
    body = "<html><body>Please verify you are human. Access denied.</body></html>"
    report = detector.grade(url="u", status=200, body=body, item_count=0)
    assert report.blocked
    assert "challenge_markers" in report.summary()


def test_empty_response_is_blocked(detector):
    assert detector.grade(url="u", status=200, body="   ", item_count=0).blocked


def test_missing_payload_is_suspect_not_blocked(detector):
    body = "<html><body>" + HEALTHY_PADDING + "</body></html>"
    report = detector.grade(url="u", status=200, body=body, item_count=10)
    assert report.severity == SEVERITY_SUSPECT
    assert not report.blocked
    assert "missing_next_data" in report.summary()


def test_thin_results_are_suspect(detector):
    report = detector.grade(url="u", status=200, body=healthy_page(["1", "2"]), item_count=2)
    assert report.severity == SEVERITY_SUSPECT
    assert "thin_results_2<5" in report.summary()


def test_empty_page_with_valid_payload_is_the_end_of_pagination(detector):
    """Crawlers always overshoot the last page; that is not a block."""
    body = healthy_page([])
    report = detector.grade(url="u", status=200, body=body, item_count=0, payload_parsed=True)
    assert report.severity == SEVERITY_OK
    assert "empty_page_end_of_pagination" in report.reasons


def test_empty_page_without_a_payload_is_blocked(detector):
    body = healthy_page([])
    report = detector.grade(
        url="u", status=200, body=body.replace("__NEXT_DATA__", "nope"), item_count=0,
        payload_parsed=False,
    )
    assert report.blocked
    assert "zero_items" in report.summary()


def test_missing_canary_is_a_hard_block(detector):
    body = healthy_page(["9", "8", "7", "6", "5"])
    report = detector.grade(
        url="u", status=200, body=body, item_count=5,
        item_ids=["9", "8", "7", "6", "5"], canary_ids=["12345"],
    )
    assert report.blocked
    assert "canary_missing" in report.summary()


def test_present_canary_passes(detector):
    body = healthy_page(["12345", "8", "7", "6", "5"])
    report = detector.grade(
        url="u", status=200, body=body, item_count=5,
        item_ids=["12345", "8", "7", "6", "5"], canary_ids=["12345"],
    )
    assert report.ok
    assert report.canary_found is True


def test_challenge_wording_on_a_full_page_is_only_suspect():
    """Challenge words on a large page are prose, not a wall (e.g. a product
    called 'Captcha Solving Kit'), so they must not abort the run."""
    page_detector = BlockingDetector(
        min_items=5, min_body_bytes=20_000, challenge_min_body_bytes=30_000
    )
    body = healthy_page(["1"] * 10) + "<p>captcha solving kit</p>"
    report = page_detector.grade(url="u", status=200, body=body, item_count=10)
    assert report.severity == SEVERITY_SUSPECT
    assert not report.blocked


def test_same_wording_on_a_small_page_is_a_block(detector):
    body = healthy_page(["1"] * 10) + "<p>captcha solving kit</p>"
    assert detector.grade(url="u", status=200, body=body, item_count=10).blocked


def test_extract_next_data_handles_nested_braces_and_escaped_scripts():
    payload = '{"a":{"b":"a } brace and </script> inside a string"},"c":[1,2]}'
    html = f'<html><script id="__NEXT_DATA__" type="application/json">{payload}</script></html>'
    assert BlockingDetector.extract_next_data(html) == {
        "a": {"b": "a } brace and </script> inside a string"}, "c": [1, 2],
    }


def test_extract_next_data_returns_none_when_absent():
    assert BlockingDetector.extract_next_data("<html><body>nothing</body></html>") is None


def test_extract_next_data_returns_none_for_truncated_json():
    assert BlockingDetector.extract_next_data('<script id="__NEXT_DATA__">{"a":') is None


def test_looks_like_challenge_page():
    assert looks_like_challenge_page("<html>Access Denied - unusual traffic</html>") is True
    assert looks_like_challenge_page("") is True
    assert looks_like_challenge_page("<html>" + "y" * 500_000 + "captcha</html>") is False
