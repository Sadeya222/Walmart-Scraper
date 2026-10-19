"""Extraction from Walmart's embedded __NEXT_DATA__ payloads."""

from __future__ import annotations

import pytest

from tests.conftest import fixture_json
from walmart_scraper.parsing import (
    ParsedProduct,
    extract_listing_products,
    extract_pdp_product,
    is_marketplace,
    is_shipping_only,
    merge_products,
    normalize_upc,
    search_result,
)


@pytest.fixture(scope="module")
def listing():
    return extract_listing_products(fixture_json("category_next_data.json"))


def test_sponsored_duplicates_are_removed_at_the_source(listing):
    """Five raw tiles (one sponsored repeat) collapse to four products."""
    ids = [product.us_item_id for product in listing]
    assert ids == ["104517984", "554466778", "777888999", "222333444"]


def test_prices_split_into_regular_and_sale(listing):
    milk = listing[0]
    assert milk.regular_price == "3.98"
    assert milk.sale_price == "3.48"


def test_no_sale_price_when_there_is_no_markdown(listing):
    spinach = listing[3]
    assert spinach.regular_price == "2.98"
    assert spinach.sale_price == ""


def test_ppm_comes_straight_from_the_payload(listing):
    assert listing[0].ppm == "$0.03/fl oz"
    assert listing[2].ppm == "$0.02/ct"
    assert listing[1].ppm == ""


def test_offer_text_is_harvested_from_badges_and_promotions(listing):
    assert listing[0].deal_evidence == ["2 for $5", "Rollback"]
    assert listing[1].deal_evidence == ["Clip Coupon: Save $2"]
    assert listing[2].deal_evidence == ["$3.99 each with Digital Coupon"]
    assert listing[3].deal_evidence == ["Get $5 Walmart Cash on $25 purchase"]


def test_urls_and_images_are_resolved(listing):
    assert listing[0].url.startswith("https://www.walmart.com/ip/")
    assert listing[0].image_url.startswith("https://i5.walmartimages.com/")
    # The original query string is preserved, not stripped.
    assert "odnHeight=180" in listing[0].image_url


def test_marketplace_detection(listing):
    assert is_marketplace(listing[1]) is True          # SuperSaver Direct LLC
    assert is_marketplace(listing[0]) is False         # Walmart.com


def test_shipping_only_detection(listing):
    assert is_shipping_only(listing[1]) is True        # SHIP only
    assert is_shipping_only(listing[0]) is False       # STORE + PICKUP + SHIP


def test_absent_fulfillment_is_not_treated_as_shipping_only():
    product = ParsedProduct(fulfillment=[])
    assert is_shipping_only(product) is False


def test_fulfillment_requirement_can_be_enforced():
    product = ParsedProduct(fulfillment=["SHIP"])
    assert is_shipping_only(product, require_tokens=("store", "pickup")) is True
    assert is_shipping_only(ParsedProduct(fulfillment=[]), require_tokens=("store",)) is False


def test_pdp_supplies_the_upc():
    pdp = extract_pdp_product(fixture_json("pdp_next_data.json"))
    assert pdp is not None
    assert pdp.upc == "00078742034165"
    assert pdp.brand == "Great Value"


def test_pdp_weight_candidates_are_ordered_by_specificity():
    pdp = extract_pdp_product(fixture_json("pdp_next_data.json"))
    assert pdp.weight_candidates[0] == "8.6 lb"
    assert any("Net Weight" in candidate for candidate in pdp.weight_candidates)


def test_merge_fills_listing_gaps_from_the_pdp(listing):
    pdp = extract_pdp_product(fixture_json("pdp_next_data.json"))
    merged = merge_products(listing[0], pdp)
    assert merged.source == "merged"
    assert merged.upc == "00078742034165"
    assert merged.ppm == "$0.03/fl oz"
    assert "2 for $5" in " ".join(merged.deal_evidence)


def test_merge_keeps_the_longer_title():
    """A short PDP name must not overwrite a complete tile title."""
    tile = ParsedProduct(us_item_id="7", title="Marketside Fresh Spinach, 10 oz", url="/ip/x/7")
    pdp = ParsedProduct(us_item_id="7", title="Product 7", url="/ip/x/7", source="pdp")
    assert merge_products(tile, pdp).title == "Marketside Fresh Spinach, 10 oz"

    truncated = ParsedProduct(us_item_id="8", title="Great Value Whole Vitamin D M", url="/ip/y/8")
    full = ParsedProduct(us_item_id="8", title="Great Value Whole Vitamin D Milk, 1 gal",
                         url="/ip/y/8", source="pdp")
    assert merge_products(truncated, full).title == "Great Value Whole Vitamin D Milk, 1 gal"


def test_merge_without_a_pdp_keeps_the_listing_product(listing):
    merged = merge_products(listing[0], None)
    assert merged.source == "listing"
    assert merged.us_item_id == "104517984"


def test_identity_prefers_the_item_id():
    assert ParsedProduct(us_item_id="123", url="/ip/x/123").identity == "123"
    assert ParsedProduct(url="/ip/x/123").identity == "/ip/x/123"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("00078742034165", "00078742034165"), ("0 078742 03416 5", "0078742034165"),
     ("", ""), (None, "")],
)
def test_normalize_upc(raw, expected):
    assert normalize_upc(raw) == expected


def test_empty_and_malformed_payloads_do_not_raise():
    assert extract_listing_products(None) == []
    assert extract_listing_products({}) == []
    assert extract_listing_products({"props": {"pageProps": {}}}) == []
    assert extract_pdp_product(None) is None
    assert extract_pdp_product({"props": {"pageProps": {"initialData": {}}}}) is None


def test_flat_items_layout_is_supported():
    """Walmart ships both itemStacks[].itemsV2[] and a flat items[] list."""
    payload = {"props": {"pageProps": {"initialData": {"searchResult": {"items": [
        {"usItemId": "1", "title": "Flat item", "canonicalUrl": "/ip/Flat/1",
         "priceInfo": {"currentPrice": {"price": 1.5}}, "sellerName": "Walmart.com"},
    ]}}}}}
    products = extract_listing_products(payload)
    assert len(products) == 1
    assert products[0].title == "Flat item"
    assert products[0].regular_price == "1.50"


def test_search_result_lookup_finds_nested_layouts():
    assert search_result({"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{}]}}}}})
    assert search_result({"props": {"pageProps": {"initialData": {"items": [{}]}}}})
    assert search_result({"nothing": True}) == {}


def test_ppm_falls_back_to_free_text_on_the_pdp():
    payload = {"props": {"pageProps": {"initialData": {"product": {
        "usItemId": "9", "name": "Thing", "canonicalUrl": "/ip/Thing/9",
        "price": {"currentPrice": {"price": 3.99}},
        "shortDescription": "Only $0.25/oz at this size",
    }}}}}
    product = extract_pdp_product(payload)
    assert product is not None
    assert product.ppm == "$0.25/oz"
