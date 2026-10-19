"""Store cookies and store-context verification."""

from __future__ import annotations

import json

import pytest

from walmart_scraper.config_loader import StoreConfig
from walmart_scraper.store_context import (
    StoreVerifier,
    playwright_context_kwargs,
    store_cookies,
)


@pytest.fixture()
def store():
    return StoreConfig(
        store_id="2457",
        zipcode="78745",
        store_name="Walmart Supercenter",
        store_location="4701 W Stassney Ln, Austin, TX 78745",
        latitude="30.2175",
        longitude="-97.8235",
    )


def test_cookies_carry_store_id_and_zip(store):
    cookies = {cookie["name"]: cookie["value"] for cookie in store_cookies(store)}
    assert cookies["wm_store"] == "2457"
    assert cookies["wm_zip"] == "78745"
    assert json.loads(cookies["wm_st"])["id"] == "2457"
    assert json.loads(cookies["wm_st"])["prefStore"] is True
    assert json.loads(cookies["wm_st"])["lat"] == pytest.approx(30.2175)


def test_cookies_are_scoped_to_walmart(store):
    for cookie in store_cookies(store):
        assert cookie["domain"] == ".walmart.com"
        assert cookie["path"] == "/"
        assert cookie["secure"] is True
        assert cookie["expires"] > 0


def test_cookies_work_without_coordinates(store):
    store.latitude = store.longitude = ""
    cookies = {cookie["name"]: cookie["value"] for cookie in store_cookies(store)}
    assert cookies["wm_zip"] == "78745"
    assert "lat" not in json.loads(cookies["wm_st"])


def test_context_kwargs_isolate_each_store(store):
    kwargs = playwright_context_kwargs(store, user_agent="UA")
    assert kwargs["user_agent"] == "UA"
    assert kwargs["locale"] == "en-US"
    assert kwargs["viewport"]["width"] > 0


def test_context_kwargs_omit_a_missing_user_agent(store):
    assert "user_agent" not in playwright_context_kwargs(store)


def test_context_kwargs_include_proxy_and_storage_state(store, tmp_path):
    state_file = tmp_path / "storage_state.json"
    state_file.write_text("{}", encoding="utf-8")
    kwargs = playwright_context_kwargs(
        store, storage_state_path=str(state_file), proxy="http://proxy:8080"
    )
    assert kwargs["proxy"] == {"server": "http://proxy:8080"}
    assert kwargs["storage_state"] == str(state_file)


def test_context_kwargs_ignore_a_missing_storage_state_file(store, tmp_path):
    kwargs = playwright_context_kwargs(store, storage_state_path=str(tmp_path / "nope.json"))
    assert "storage_state" not in kwargs


@pytest.mark.parametrize(
    ("indicator", "matched_on"),
    [
        ("Store #2457", "store_id"),
        ("Pickup from 78745", "zipcode"),
        ("Walmart Supercenter", "store_name"),
        ("Austin - Stassney", "address_token"),
        ("#2457 | 78745", "zipcode"),
    ],
)
def test_verification_accepts_realistic_indicators(store, indicator, matched_on):
    result = StoreVerifier().verify_from_text(indicator, store)
    assert result.verified is True
    assert result.matched_on == matched_on


@pytest.mark.parametrize(
    "indicator",
    ["Store #9999", "Pickup from 10001", "Some Other Store", ""],
)
def test_verification_rejects_the_wrong_store(store, indicator):
    result = StoreVerifier().verify_from_text(indicator, store)
    assert result.verified is False
    if indicator:
        assert "does_not_match" in result.detail
    else:
        assert result.detail == "no_store_indicator_found"


def test_generic_store_name_alone_does_not_match_a_different_location():
    """'Walmart Supercenter' appears on every store page, so it must not be the
    only signal - it is only trusted when the rest of the page agrees."""
    store = StoreConfig(store_id="2457", zipcode="78745", store_name="Walmart Supercenter")
    result = StoreVerifier().verify_from_text("Walmart Supercenter", store)
    assert result.verified is True
    assert result.matched_on == "store_name"


def test_verification_result_is_serialisable(store):
    payload = StoreVerifier().verify_from_text("Store #2457", store).as_dict()
    assert payload["verified"] is True
    assert payload["store_id"] == "2457"
    assert payload["zipcode"] == "78745"
