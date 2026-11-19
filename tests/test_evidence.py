"""Raw-payload evidence capture (``walmart_scraper.evidence``).

The acceptance review asks to see "the product and store response behind one
displayed price". These tests pin the behaviour of the capture layer: files are
written only when enabled, the raw Walmart nodes land in the file verbatim, the
store-context file tells the truth about how the store was verified, the
per-store budget is enforced, and a capture failure can never raise into the
crawl.
"""

from __future__ import annotations

import json

from tests.conftest import fixture_json

from walmart_scraper.config_loader import CategoryTarget, StoreConfig
from walmart_scraper.deals import parse_deals
from walmart_scraper.evidence import EvidenceCollector
from walmart_scraper.items import assemble_record
from walmart_scraper.items import ProductRecord
from walmart_scraper.parsing import (
    extract_listing_products,
    extract_pdp_product,
    find_pdp_product_node,
    iter_listing_item_nodes,
)
from walmart_scraper.store_context import StoreVerifier, store_cookies

STORE = StoreConfig(
    store_id="2457",
    zipcode="78745",
    store_name="Walmart Supercenter",
    store_location="4701 W Stassney Ln, Austin, TX 78745",
)
TARGET = CategoryTarget(url="https://www.walmart.com/search?q=whole+milk",
                        category="Grocery", sub_category="Dairy & Milk")


def _record(product, page=1):
    item = assemble_record(
        product=product,
        store_zipcode=STORE.zipcode,
        store_name=STORE.label,
        store_location=STORE.store_location,
        store_logo=STORE.store_logo,
        category=TARGET.category,
        sub_category=TARGET.sub_category,
        deals=parse_deals(product.deal_evidence),
        date_stamp="2026-11-19",
    )
    return ProductRecord(item=item, product=product, store_id=STORE.store_id,
                         category_url=TARGET.url, listing_url=TARGET.url, page=page)


def _enabled(tmp_path, **kwargs) -> EvidenceCollector:
    return EvidenceCollector(enabled=True, directory=tmp_path, run_id="testrun",
                             max_products_per_store=kwargs.get("max_products", 2))


# --------------------------------------------------------------------------- #
# Disabled by default
# --------------------------------------------------------------------------- #
def test_disabled_collector_writes_nothing(tmp_path):
    collector = _enabled(tmp_path)
    collector.enabled = False
    assert collector.record_store_context(
        store=STORE, verification=None, payload=None, response_url="u"
    ) is None
    assert collector.record_price(record=_record(
        extract_pdp_product(fixture_json("pdp_next_data.json"))
    ), store=STORE, target=TARGET, page=1) is None
    assert collector.write_index() is None
    assert list(tmp_path.rglob("*.json")) == []


def test_from_settings_defaults_to_off(monkeypatch, tmp_path):
    class FakeSettings(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    assert EvidenceCollector.from_settings(FakeSettings()).enabled is False
    on = EvidenceCollector.from_settings(FakeSettings({
        "WMT_EVIDENCE_ENABLED": "true",
        "WMT_EVIDENCE_DIR": str(tmp_path),
        "WMT_EVIDENCE_MAX_PRODUCTS": "1",
        "WMT_RUN_ID": "run123",
    }))
    assert on.enabled is True
    assert on.directory.name == "run123"
    assert on.max_products_per_store == 1


# --------------------------------------------------------------------------- #
# Store context evidence
# --------------------------------------------------------------------------- #
def test_store_context_records_payload_location_and_verification(tmp_path):
    payload = {"props": {"pageProps": {"initialData": {"data": {"product": {
        "location": {"postalCode": "78745", "storeIds": ["2457"]},
        "fulfillmentLabel": [{"locationText": "Pickup from 78745"}],
    }}}}}}
    verification = StoreVerifier().verify_from_payload(payload, STORE)
    collector = _enabled(tmp_path)
    path = collector.record_store_context(
        store=STORE,
        verification=verification,
        payload=payload,
        response_url="https://www.walmart.com/search?q=whole+milk",
        verification_mode="cookie_then_ui",
        cookies=store_cookies(STORE),
        fulfillment_mode="any",
    )
    assert path is not None and path.exists()
    doc = json.loads(path.read_text())
    assert doc["evidence"] == "store_context"
    assert doc["store"]["store_id"] == "2457"
    assert doc["store"]["zipcode"] == "78745"
    assert doc["payload_location"]["postal_codes"] == ["78745"]
    assert doc["payload_location"]["store_ids"] == ["2457"]
    assert "Pickup from 78745" in doc["payload_location"]["location_text"]
    assert doc["verification"]["verified"] is True
    assert doc["verification"]["matched_on"] == "payload_postal_code"
    assert doc["fulfillment_mode"] == "any"
    assert any(c["name"] == "wm_st" for c in doc["cookies_seeded"])
    assert doc["run_id"] == "testrun"
    assert path.parent.name == "testrun"


def test_store_context_records_a_failed_verification_truthfully(tmp_path):
    collector = _enabled(tmp_path)
    verification = StoreVerifier().verify_from_text("Some unrelated text", STORE)
    path = collector.record_store_context(
        store=STORE,
        verification=verification,
        payload=None,
        response_url="https://www.walmart.com/",
        verification_mode="cookie_then_ui",
        cookies=[],
        fulfillment_mode="pickup",
    )
    doc = json.loads(path.read_text())
    assert doc["verification"]["verified"] is False
    assert doc["payload_location"] == {"postal_codes": [], "store_ids": [], "location_text": []}


# --------------------------------------------------------------------------- #
# Price evidence: raw tile + raw PDP behind the displayed price
# --------------------------------------------------------------------------- #
def test_price_evidence_contains_raw_payloads_and_the_record(tmp_path):
    listing = fixture_json("category_next_data.json")
    pdp_payload = fixture_json("pdp_next_data.json")
    listing_product = next(
        p for p in extract_listing_products(listing) if p.us_item_id == "104517984"
    )
    pdp = extract_pdp_product(pdp_payload)
    merged = listing_product.merge_from(pdp)

    collector = _enabled(tmp_path)
    collector.stash_listing_items(STORE, listing, [listing_product])
    collector.stash_pdp_node(STORE, merged, pdp_payload)
    path = collector.record_price(record=_record(merged), store=STORE, target=TARGET, page=1)

    assert path is not None and path.exists()
    doc = json.loads(path.read_text())
    assert doc["evidence"] == "price"
    assert doc["store"]["store_id"] == "2457"
    # The raw tile exactly as served: prices live in priceLines / currentPrice.
    assert doc["listing"]["raw_tile"]["usItemId"] == "104517984"
    assert doc["listing"]["raw_tile"]["priceInfo"]["currentPrice"]["price"] == 3.48
    # The raw PDP product node exactly as served.
    assert doc["pdp"]["raw_product"]["usItemId"] == "104517984"
    assert doc["pdp"]["raw_product"]["gtin"] == "00078742034165"
    # The exported record those payloads produced.
    assert doc["record"]["regular_price"] == "3.98"
    assert doc["record"]["sale_price"] == "3.48"
    assert doc["record"]["ppm"] == "$0.03/fl oz"
    assert doc["record"]["upc"] == "00078742034165"
    assert doc["record"]["gupc"] == ""
    assert doc["record"]["crawl_date"] == "2026-11-19"
    assert doc["notes"] == []


def test_price_evidence_is_written_with_honest_notes_when_raw_missing(tmp_path):
    product = extract_pdp_product(fixture_json("pdp_next_data.json"))
    collector = _enabled(tmp_path)
    path = collector.record_price(record=_record(product), store=STORE, target=TARGET, page=1)
    doc = json.loads(path.read_text())
    assert doc["listing"]["raw_tile"] is None
    assert doc["pdp"]["raw_product"] is None
    assert any("raw_listing_tile_unavailable" in note for note in doc["notes"])
    assert any("raw_pdp_payload_unavailable" in note for note in doc["notes"])


def test_per_store_budget_caps_price_files(tmp_path):
    listing = fixture_json("category_next_data.json")
    pdp_payload = fixture_json("pdp_next_data.json")
    products = extract_listing_products(listing)
    collector = _enabled(tmp_path, max_products=1)
    for product in products[:3]:
        merged = product.merge_from(extract_pdp_product(pdp_payload))
        collector.stash_listing_items(STORE, listing, [product])
        collector.stash_pdp_node(STORE, merged, pdp_payload)
        collector.record_price(record=_record(merged), store=STORE, target=TARGET, page=1)
    files = list(tmp_path.rglob("price_evidence_*.json"))
    assert len(files) == 1


def test_unserialisable_raw_node_never_raises(tmp_path):
    product = extract_pdp_product(fixture_json("pdp_next_data.json"))
    collector = _enabled(tmp_path)
    collector._tiles[f"{STORE.store_id}:{product.identity}"] = {"bad": object()}
    path = collector.record_price(record=_record(product), store=STORE, target=TARGET, page=1)
    assert path is None


def test_write_index_lists_captured_files(tmp_path):
    collector = _enabled(tmp_path)
    verification = StoreVerifier().verify_from_text("Store #2457", STORE)
    collector.record_store_context(
        store=STORE, verification=verification, payload=None,
        response_url="u", verification_mode="cookie", cookies=[], fulfillment_mode="any",
    )
    index = collector.write_index()
    doc = json.loads(index.read_text())
    assert doc["run_id"] == "testrun"
    assert doc["counts"]["store_context"] == 1
    assert doc["counts"]["price"] == 0
    assert doc["files"], "index must list the captured files"
    for file_path in doc["files"]:
        assert file_path.endswith(".json")


# --------------------------------------------------------------------------- #
# Parsing helpers the evidence layer relies on
# --------------------------------------------------------------------------- #
def test_iter_listing_item_nodes_yields_the_raw_tiles():
    nodes = list(iter_listing_item_nodes(fixture_json("category_next_data.json")))
    identities = [node.get("usItemId") for node in nodes]
    assert "104517984" in identities
    assert "222333444" in identities
    # The sponsored repeat is nested under "product" and resolved to its core.
    assert identities.count("104517984") == 2


def test_find_pdp_product_node_returns_the_raw_node():
    node = find_pdp_product_node(fixture_json("pdp_next_data.json"))
    assert node["usItemId"] == "104517984"
    assert node["gtin"] == "00078742034165"
    assert find_pdp_product_node({}) is None
    assert find_pdp_product_node(None) is None
