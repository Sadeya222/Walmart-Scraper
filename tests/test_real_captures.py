"""Tests driven by REAL captured Walmart data (see ``tests/fixtures/real/SOURCES.md``).

Everything else in this suite parses fixtures written by this project, so it can
only prove that the parser agrees with its author's assumptions.  This module
runs the same code against pages and payloads Walmart actually served, and
asserts the exact values Walmart sent.  Every expected value below was read out
of a capture, not invented.

These tests are what caught three genuine defects:

* the product payload sits at ``props.pageProps.initialData.data.product`` - an
  extra ``data`` level the parser did not have, so real PDPs returned ``None``;
* listing tiles keep their amounts in ``priceInfo.priceDetails.priceLines[]``
  with ``minPrice: 0``, which the parser exported as a ``0.00`` price;
* a spec sheet can list several different measures for one product, so taking
  the first parseable weight reported ``Size: 16 oz`` where Walmart states
  ``Weight: 1.12 lb``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from walmart_scraper.blocking import BlockingDetector
from walmart_scraper.items import store_brand_for
from walmart_scraper.parsing import (
    extract_listing_products,
    extract_pdp_product,
    is_marketplace,
    is_shipping_only,
)
from walmart_scraper.store_context import StoreConfig, StoreVerifier
from walmart_scraper.text import normalize_price, validate_ppm
from walmart_scraper.weight import extract_weight

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "real"


def _real_pdp_payload() -> dict[str, Any]:
    html = (FIXTURES / "poc_pdp.html").read_text(encoding="utf-8", errors="replace")
    payload = BlockingDetector.extract_next_data(html)
    assert payload is not None, "real capture must contain a __NEXT_DATA__ block"
    return payload


def _real_listing_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "location_listing.json").read_text(encoding="utf-8"))


def _real_whatnut_products() -> list[dict[str, Any]]:
    capture = json.loads((FIXTURES / "whatnut_capture.json").read_text(encoding="utf-8"))
    return capture["products"]


# --------------------------------------------------------------------------- #
# Real product detail page
# --------------------------------------------------------------------------- #
def test_real_pdp_next_data_envelope_is_reachable():
    payload = _real_pdp_payload()
    assert set(payload) >= {"props", "page", "query", "buildId"}


def test_real_pdp_parses_and_reports_the_values_walmart_sent():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")

    assert product is not None, (
        "the real product payload lives at props.pageProps.initialData.data.product"
    )
    assert product.us_item_id == "795033156"
    assert product.title == "Eliminator Ant, Roach & Spider Killer4, 20 oz, Kills Insects & Spiders"
    assert product.brand == "Eliminator"
    assert product.seller_name == "Walmart.com"
    assert product.seller_id == "F55CDC31AB754BB68FE0B39041159D63"
    assert product.availability == "IN_STOCK"
    assert "FC" in product.fulfillment
    # URL and image URL are preserved, not rewritten.
    assert product.url == (
        "https://www.walmart.com/ip/"
        "Eliminator-Ant-Roach-Spider-Killer4-20-oz-Kills-Insects-Spiders/795033156"
    )
    assert product.image_url.startswith("https://i5.walmartimages.com/asr/")
    assert "High demand item" in product.deal_evidence


def test_real_pdp_price_without_a_was_price_is_a_regular_price_only():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")
    assert product is not None
    # currentPrice.priceString was "$3.48" and wasPrice was null.
    assert product.regular_price == "3.48"
    assert product.sale_price == ""


def test_real_pdp_upc_comes_from_product_upc_because_there_is_no_gtin_key():
    payload = _real_pdp_payload()
    product = extract_pdp_product(payload, base_url="https://www.walmart.com")
    assert product is not None
    assert product.upc == "044579413717"

    # The word "gtin" does occur in the HTML, but there is no such key in the
    # product payload - so a gtin->upc fallback would be reading nothing.
    node = payload["props"]["pageProps"]["initialData"]["data"]["product"]

    def _has_key(node: Any, key: str) -> bool:
        if isinstance(node, dict):
            return key in node or any(_has_key(value, key) for value in node.values())
        if isinstance(node, list):
            return any(_has_key(entry, key) for entry in node)
        return False

    assert not _has_key(node, "gtin")


def test_real_pdp_weight_comes_from_the_idml_spec_sheet_beside_the_product():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")
    assert product is not None
    # initialData.data.idml.specifications holds "Assembled Product Weight".
    assert "Assembled Product Weight: 1.61 lb" in product.weight_candidates
    assert extract_weight(product.weight_candidates) == "1.61 lb"
    # The neighbouring "Assembled Product Dimensions" entry is not a weight.
    assert not any("Dimensions" in candidate for candidate in product.weight_candidates)


def test_real_pdp_structured_location_never_leaks_into_a_text_field():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")
    assert product is not None
    # product.location is a dict ({postalCode, storeIds, city, ...}); the
    # store_location field must stay a string rather than holding its repr.
    assert product.product_location == ""


def test_real_pdp_ppm_with_an_undecodable_unit_character_is_rejected_not_exported():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")
    assert product is not None
    # The capture serves "17.4 <U+FFFD>/oz": the cent sign is corrupted to the
    # replacement character in the upstream file itself.  Walmart's own value
    # was "17.4 ¢/oz".  A ppm we cannot read must be "" - never a mojibake
    # string, and never a value we reconstruct ourselves.
    assert product.ppm == ""
    assert "\ufffd" not in product.ppm
    assert validate_ppm("17.4 \ufffd/oz") == ""
    assert validate_ppm("17.4 ¢/oz") == "17.4 ¢/oz"


def test_real_pdp_walmart_sold_item_is_neither_marketplace_nor_shipping_only():
    product = extract_pdp_product(_real_pdp_payload(), base_url="https://www.walmart.com")
    assert product is not None
    assert product.seller_name == "Walmart.com"
    assert is_marketplace(product) is False
    assert is_shipping_only(product) is False


# --------------------------------------------------------------------------- #
# Store verification from the payload Walmart resolved
# --------------------------------------------------------------------------- #
REAL_STORE = StoreConfig(
    zipcode="79415", store_id="3826", store_name="Walmart Supercenter", store_location="Lubbock, TX"
)


def test_real_pdp_payload_verifies_the_store_it_was_rendered_for():
    result = StoreVerifier().verify_from_payload(_real_pdp_payload(), REAL_STORE)
    assert result.verified is True
    assert result.matched_on == "payload_postal_code"


def test_real_pdp_payload_rejects_a_different_store():
    other = StoreConfig(
        zipcode="78745",
        store_id="2457",
        store_name="Walmart Supercenter",
        store_location="Austin - Stassney",
    )
    result = StoreVerifier().verify_from_payload(_real_pdp_payload(), other)
    assert result.verified is False
    assert result.detail == "payload_location_does_not_match_store_2457_78745"


@pytest.mark.parametrize(
    "payload,matched_on",
    [
        ({"product": {"location": {"storeIds": ["3826"]}}}, "payload_store_id"),
        ({"product": {"location": {"pickupLocation": {"storeId": "3826"}}}}, "payload_store_id"),
        ({"product": {"location": {"postalCode": "79415"}}}, "payload_postal_code"),
    ],
)
def test_payload_verification_uses_each_real_signal(payload, matched_on):
    result = StoreVerifier().verify_from_payload(payload, REAL_STORE)
    assert result.verified is True
    assert result.matched_on == matched_on


def test_payload_verification_matches_the_real_fulfillment_label_text():
    # "Lubbock Supercenter" is the real fulfillmentLabel[].locationText value.
    store = StoreConfig(
        zipcode="00000",
        store_id="9999",
        store_name="Walmart Supercenter",
        store_location="Lubbock, TX",
    )
    payload = {"product": {"fulfillmentLabel": [{"locationText": "Lubbock Supercenter"}]}}
    result = StoreVerifier().verify_from_payload(payload, store)
    assert result.verified is True
    assert result.matched_on == "payload_address_token"


def test_payload_verification_reports_absent_location_distinctly_from_a_mismatch():
    verifier = StoreVerifier()
    assert verifier.verify_from_payload({}, REAL_STORE).detail == "payload_has_no_location"
    assert verifier.verify_from_payload(None, REAL_STORE).detail == "no_payload"


# --------------------------------------------------------------------------- #
# Real category listing
# --------------------------------------------------------------------------- #
def test_real_listing_yields_every_product_tile_and_skips_placeholders():
    products = extract_listing_products(_real_listing_payload(), base_url="https://www.walmart.com")
    assert [product.us_item_id for product in products] == [
        "19288557285",
        "2624964791",
        "732060516",
        "9861071882",
    ]
    # Two entries in itemStacks[0].items[] carry only {"__typename": ...}; they
    # must be dropped, not exported as empty rows.
    assert all(product.title and product.brand for product in products)


def test_real_listing_reads_prices_from_price_details_price_lines():
    products = {
        product.us_item_id: product
        for product in extract_listing_products(
            _real_listing_payload(), base_url="https://www.walmart.com"
        )
    }
    # The tiles serve itemPrice/linePrice/wasPrice as "" and minPrice as 0, so
    # the amount only exists in priceDetails.priceLines[].
    assert products["19288557285"].regular_price == "69.99"
    assert products["19288557285"].sale_price == ""
    assert products["2624964791"].regular_price == "137.45"
    assert products["732060516"].regular_price == "18.90"
    # minPrice: 0 is "not resolved yet", never a free item.
    assert all(product.regular_price != "0.00" for product in products.values())


def test_real_listing_markdown_splits_into_regular_and_sale_price():
    products = {
        product.us_item_id: product
        for product in extract_listing_products(
            _real_listing_payload(), base_url="https://www.walmart.com"
        )
    }
    # Real lineTypes: DISCOUNTED_PRICE 69.87 and COMPARISON/WAS_PRICE 122.00.
    assert products["9861071882"].regular_price == "122.00"
    assert products["9861071882"].sale_price == "69.87"


def test_real_listing_brand_comes_from_product_brand():
    products = extract_listing_products(_real_listing_payload(), base_url="https://www.walmart.com")
    assert {product.brand for product in products} == {"Nike"}


# --------------------------------------------------------------------------- #
# 15 products transcribed live from walmart.com (ZIP 20001, store 3035)
# --------------------------------------------------------------------------- #
def test_real_capture_location_context_is_a_walmart_zip_and_store():
    capture = json.loads((FIXTURES / "whatnut_capture.json").read_text(encoding="utf-8"))
    context = capture["location_context"]
    assert context["zipCode"] == "20001"
    assert context["storeId"] == "3035"
    # The capture's own note corroborates reading the store from the payload:
    # "Read from __NEXT_DATA__ ad configs on each page".
    assert "__NEXT_DATA__" in context.get("note", "")
    assert len(capture["products"]) == 15


@pytest.mark.parametrize("product", _real_whatnut_products(), ids=lambda p: p["usItemId"])
def test_real_ppm_strings_are_accepted_verbatim(product):
    # Never calculated, never reformatted: Walmart's own string is exported as is.
    assert validate_ppm(product["unitPrice"]) == product["unitPrice"]


def test_real_ppm_includes_the_cent_sign_form():
    values = {product["unitPrice"] for product in _real_whatnut_products()}
    assert "41.9 ¢/oz" in values
    assert "$1.34/oz" in values
    assert "$1.33/oz" in values


@pytest.mark.parametrize("product", _real_whatnut_products(), ids=lambda p: p["usItemId"])
def test_real_weight_prefers_walmart_weight_specification(product):
    specs = product.get("specs") or []
    stated = next(
        (entry.split(":", 1)[1].strip() for entry in specs if entry.lower().startswith("weight:")),
        None,
    )
    if stated is None:
        pytest.skip("this product states no Weight: specification")
    assert extract_weight(specs) == stated


def test_real_specs_that_disagree_resolve_to_the_weight_not_the_size():
    # Great Value Walnuts 16 oz lists both of these on one page.
    assert extract_weight(["Size: 16 oz (454g)", "Weight: 1.12 lb"]) == "1.12 lb"
    assert extract_weight(["Product net content parent: 16 Ounces", "Weight: 1 lb"]) == "1 lb"


def test_real_specs_without_a_weight_label_still_yield_a_measure():
    specs = ["Ingredients: ALMONDS.", "Net content statement: NET WT 4 oz (113g)"]
    assert extract_weight(specs) == "NET WT 4 oz (113g)"
    assert extract_weight(["Size: 12 Ounce"]) == "12 Ounce"


@pytest.mark.parametrize("product", _real_whatnut_products(), ids=lambda p: p["usItemId"])
def test_real_store_brand_flags_private_label_only(product):
    expected = "yes" if product["brand"] == "Great Value" else "no"
    assert store_brand_for(product["brand"]) == expected


def test_real_capture_is_predominantly_private_label():
    brands = [product["brand"] for product in _real_whatnut_products()]
    assert brands.count("Great Value") == 14
    assert "Wonderful Pistachios" in brands
    assert store_brand_for("Wonderful Pistachios") == "no"


@pytest.mark.parametrize("product", _real_whatnut_products(), ids=lambda p: p["usItemId"])
def test_real_prices_normalise_without_losing_value(product):
    assert normalize_price(product["currentPrice"]["price"]) == (
        f"{product['currentPrice']['price']:.2f}"
    )
    # Every captured product had wasPrice: null, so nothing is on sale.
    assert product["wasPrice"] is None


@pytest.mark.parametrize("product", _real_whatnut_products(), ids=lambda p: p["usItemId"])
def test_real_items_were_sold_by_walmart_and_in_stock(product):
    assert product["seller"] == "Walmart.com"
    assert product["availability"] == "IN_STOCK"
