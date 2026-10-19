"""The output contract: 22 fields, exact order, empty strings, crawl_date last."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import scrapy

from walmart_scraper.constants import FIELD_ORDER
from walmart_scraper.deals import parse_deals
from walmart_scraper.items import (
    WalmartProductItem,
    assert_field_order,
    assemble_record,
    crawl_date,
)
from walmart_scraper.parsing import ParsedProduct

EXPECTED_ORDER = [
    "zipcode", "store_name", "store_location", "store_logo", "store_brand",
    "category", "sub_category", "product_title", "weight", "regular_price",
    "sale_price", "volume_deals_description", "volume_deals_price",
    "digital_deals_description", "digital_deals_price", "image_url", "url",
    "upc", "gupc", "ppm", "unit_price", "crawl_date",
]


def test_field_order_constant_is_exactly_as_specified():
    assert list(FIELD_ORDER) == EXPECTED_ORDER


def test_item_declares_exactly_the_required_fields():
    """scrapy.Item does not preserve declaration order (the metaclass sorts it),
    so the contract is: exactly these fields, serialised in FIELD_ORDER."""
    assert set(WalmartProductItem.fields) == set(EXPECTED_ORDER)


def test_serialisation_is_in_the_required_order_even_though_fields_are_sorted():
    empty = WalmartProductItem()
    assert list(empty.to_ordered_dict()) == EXPECTED_ORDER
    # Documenting the upstream behaviour this test guards against:
    assert tuple(WalmartProductItem.fields) != tuple(EXPECTED_ORDER)


def test_assert_field_order_passes_for_the_shipped_item():
    assert_field_order()


def test_assert_field_order_detects_an_extra_field():
    original = dict(WalmartProductItem.fields)
    try:
        WalmartProductItem.fields = {**original, "gupc14": scrapy.Field()}
        with pytest.raises(AssertionError, match=r"extra=\['gupc14'\]"):
            assert_field_order()
    finally:
        WalmartProductItem.fields = original


def test_assert_field_order_detects_a_removed_field():
    original = dict(WalmartProductItem.fields)
    try:
        WalmartProductItem.fields = {k: v for k, v in original.items() if k != "gupc"}
        with pytest.raises(AssertionError, match=r"missing=\['gupc'\]"):
            assert_field_order()
    finally:
        WalmartProductItem.fields = original


def test_crawl_date_last_and_nothing_after_it():
    assert FIELD_ORDER[-1] == "crawl_date"


@pytest.fixture()
def product():
    return ParsedProduct(
        us_item_id="104517984",
        title="Great Value Whole Vitamin D Milk, 1 gal",
        url="/ip/Great-Value-Whole-Vitamin-D-Milk-1-gal/104517984",
        image_url="//i5.walmartimages.com/asr/milk.jpeg",
        brand="Great Value",
        upc="00078742034165",
        weight_candidates=["8.6 lb"],
        regular_price="3.98",
        sale_price="3.48",
        ppm="$0.03/fl oz",
        deal_evidence=["2 for $5 ($2.50 each)"],
    )


def build(product, **overrides):
    defaults = dict(
        product=product,
        store_zipcode="78745",
        store_name="Walmart Supercenter",
        store_location="4701 W Stassney Ln, Austin, TX 78745",
        store_logo="",
        category="Grocery",
        sub_category="Dairy & Milk",
        deals=parse_deals(product.deal_evidence),
        date_stamp="2026-10-19",
    )
    defaults.update(overrides)
    return assemble_record(**defaults)


def test_serialised_record_has_the_exact_field_order(product):
    record = build(product).to_ordered_dict()
    assert list(record.keys()) == EXPECTED_ORDER


def test_serialisation_survives_a_json_round_trip_in_order(product):
    record = build(product).to_ordered_dict()
    assert list(json.loads(json.dumps(record)).keys()) == EXPECTED_ORDER


def test_gupc_is_always_empty_even_when_upc_exists(product):
    record = build(product).to_ordered_dict()
    assert record["upc"] == "00078742034165"
    assert record["gupc"] == ""


def test_unit_price_is_always_empty(product):
    assert build(product).to_ordered_dict()["unit_price"] == ""


def test_ppm_is_passed_through_verbatim(product):
    assert build(product).to_ordered_dict()["ppm"] == "$0.03/fl oz"


def test_missing_values_become_empty_strings_not_null(product):
    sparse = ParsedProduct(title="Something", url="/ip/Something/1")
    record = build(sparse).to_ordered_dict()
    assert None not in record.values()
    for key in ("weight", "regular_price", "sale_price", "upc", "ppm",
                "volume_deals_description", "digital_deals_price", "image_url", "store_logo"):
        assert record[key] == ""


def test_none_values_are_coerced_to_empty_strings(product):
    item = WalmartProductItem()
    for name in FIELD_ORDER:
        item[name] = None
    assert all(value == "" for value in item.to_ordered_dict().values())


def test_forced_empty_fields_cannot_be_overridden(product):
    item = build(product)
    item["gupc"] = "1234567890128"
    item["unit_price"] = "0.99"
    record = item.to_ordered_dict()
    assert record["gupc"] == ""
    assert record["unit_price"] == ""


def test_store_brand_is_yes_for_private_labels(product):
    assert build(product).to_ordered_dict()["store_brand"] == "yes"


def test_store_brand_defaults_to_no(product):
    product.brand = "Tide"
    assert build(product).to_ordered_dict()["store_brand"] == "no"


def test_prices_are_numeric_two_decimal_strings(product):
    record = build(product).to_ordered_dict()
    assert record["regular_price"] == "3.98"
    assert record["sale_price"] == "3.48"


def test_display_price_format_is_available(product):
    record = build(product, price_format="display").to_ordered_dict()
    assert record["regular_price"] == "$3.98"


def test_urls_are_made_absolute_but_otherwise_untouched(product):
    record = build(product).to_ordered_dict()
    assert record["url"] == (
        "https://www.walmart.com/ip/Great-Value-Whole-Vitamin-D-Milk-1-gal/104517984"
    )
    assert record["image_url"] == "https://i5.walmartimages.com/asr/milk.jpeg"


def test_deal_fields_land_in_the_right_columns(product):
    record = build(product).to_ordered_dict()
    assert record["volume_deals_description"] == "2 for $5 ($2.50 each)"
    assert record["volume_deals_price"] == "2.50"
    assert record["digital_deals_description"] == ""
    assert record["digital_deals_price"] == ""


def test_crawl_date_formats():
    moment = datetime(2026, 10, 19, 13, 45, 30, tzinfo=timezone.utc)
    assert crawl_date("date", now=moment) == "2026-10-19"
    assert crawl_date("iso", now=moment) == "2026-10-19T13:45:30+00:00"


def test_crawl_date_defaults_to_utc():
    naive = datetime(2026, 1, 1, 23, 30)
    assert crawl_date("date", now=naive) == "2026-01-01"


def test_extra_private_labels_extend_the_brand_list(product):
    product.brand = "Acme Store Brand"
    assert build(product).to_ordered_dict()["store_brand"] == "no"
    record = build(product, extra_private_labels=("acme store brand",)).to_ordered_dict()
    assert record["store_brand"] == "yes"


def test_weight_override_is_honoured(product):
    assert build(product, weight_override="1 gal").to_ordered_dict()["weight"] == "1 gal"
