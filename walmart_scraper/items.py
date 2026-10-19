"""The single exported record type and its assembly rules.

The contract here is literal: 22 fields, in this order, ``""`` for anything
missing, ``crawl_date`` last.  ``FIELD_ORDER`` in :mod:`walmart_scraper.constants`
is the source of truth, the ``Item`` declares fields in that order, and the
exporter serialises by that tuple - so the order cannot drift silently.  A test
asserts all three agree.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import scrapy

from .constants import EMPTY, FIELD_ORDER, FORCED_EMPTY_FIELDS
from .deals import DealResult, parse_deals
from .parsing import ParsedProduct
from .text import (
    absolute_url,
    clean_text,
    normalize_price,
    safe_str,
    store_brand_for,
)
from .weight import extract_weight

__all__ = [
    "WalmartProductItem",
    "ProductRecord",
    "assemble_record",
    "crawl_date",
    "assert_field_order",
]


class WalmartProductItem(scrapy.Item):
    """One exported row.  Field declaration order *is* the output field order."""

    zipcode = scrapy.Field()
    store_name = scrapy.Field()
    store_location = scrapy.Field()
    store_logo = scrapy.Field()
    store_brand = scrapy.Field()
    category = scrapy.Field()
    sub_category = scrapy.Field()
    product_title = scrapy.Field()
    weight = scrapy.Field()
    regular_price = scrapy.Field()
    sale_price = scrapy.Field()
    volume_deals_description = scrapy.Field()
    volume_deals_price = scrapy.Field()
    digital_deals_description = scrapy.Field()
    digital_deals_price = scrapy.Field()
    image_url = scrapy.Field()
    url = scrapy.Field()
    upc = scrapy.Field()
    gupc = scrapy.Field()          # always "" - never generated, never copied from upc
    ppm = scrapy.Field()           # verbatim from the page, e.g. "$0.25/oz"
    unit_price = scrapy.Field()    # always "" - computed downstream
    crawl_date = scrapy.Field()    # always last

    # ------------------------------------------------------------------ #
    def to_ordered_dict(self) -> "OrderedDict[str, Any]":
        """Serialise in the canonical field order, coercing every value to ``str``.

        ``None`` becomes ``""`` - a JSON ``null`` must never reach the dataset.
        """
        record: OrderedDict[str, Any] = OrderedDict()
        for name in FIELD_ORDER:
            value = self.get(name, EMPTY)
            record[name] = EMPTY if value is None else safe_str(value)
        for name in FORCED_EMPTY_FIELDS:
            record[name] = EMPTY
        return record


def assert_field_order() -> None:
    """Guard the output contract.

    Note: ``scrapy.Item`` does **not** preserve field declaration order - the
    metaclass rebuilds ``fields`` from ``dir()``, so on most versions it comes
    back alphabetically sorted.  What actually decides the JSON column order is
    :meth:`WalmartProductItem.to_ordered_dict`, which iterates ``FIELD_ORDER``.
    So this check verifies (a) that the item declares exactly the right fields
    and (b) that serialising one really produces ``FIELD_ORDER``.
    """
    declared = set(WalmartProductItem.fields)
    expected = set(FIELD_ORDER)
    if declared != expected:
        raise AssertionError(
            "WalmartProductItem fields do not match FIELD_ORDER; "
            f"missing={sorted(expected - declared)} extra={sorted(declared - expected)}"
        )
    serialised = tuple(WalmartProductItem().to_ordered_dict())
    if serialised != FIELD_ORDER:
        raise AssertionError(
            f"Serialised field order {serialised} != FIELD_ORDER {FIELD_ORDER}"
        )


def crawl_date(fmt: str = "date", *, now: datetime | None = None) -> str:
    """``YYYY-MM-DD`` (default) or a full ISO-8601 timestamp, always UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.strftime("%Y-%m-%d") if fmt == "date" else moment.isoformat(timespec="seconds")


def assemble_record(
    *,
    product: ParsedProduct,
    store_zipcode: str,
    store_name: str,
    store_location: str,
    store_logo: str,
    category: str,
    sub_category: str,
    deals: DealResult,
    date_stamp: str,
    extra_private_labels: tuple[str, ...] = (),
    price_format: str = "numeric",
    weight_override: str = "",
) -> WalmartProductItem:
    """Build one export row from a parsed product plus its classified deals.

    Every field is set explicitly - there is no ``setdefault`` path that could
    leave a key missing, which is what would break the column contract.
    """
    weight = clean_text(weight_override) or extract_weight(product.weight_candidates)

    item = WalmartProductItem()
    item["zipcode"] = safe_str(store_zipcode)
    item["store_name"] = safe_str(store_name)
    item["store_location"] = safe_str(store_location)
    item["store_logo"] = safe_str(store_logo)
    item["store_brand"] = store_brand_for(product.brand, extra_private_labels)
    item["category"] = safe_str(category)
    item["sub_category"] = safe_str(sub_category)
    item["product_title"] = clean_text(product.title)
    item["weight"] = weight
    item["regular_price"] = normalize_price(product.regular_price, style=price_format)
    item["sale_price"] = normalize_price(product.sale_price, style=price_format)
    item["volume_deals_description"] = deals.volume_description
    item["volume_deals_price"] = deals.volume_price
    item["digital_deals_description"] = deals.digital_description
    item["digital_deals_price"] = deals.digital_price
    item["image_url"] = absolute_url(product.image_url)
    item["url"] = absolute_url(product.url)
    item["upc"] = safe_str(product.upc)
    item["gupc"] = EMPTY                       # never generated, never copied
    item["ppm"] = product.ppm or EMPTY         # verbatim from the page
    item["unit_price"] = EMPTY                 # computed downstream
    item["crawl_date"] = date_stamp
    return item


def deals_for(product: ParsedProduct, **policy: Any) -> DealResult:
    """Classify a product's offer evidence with the run's deal policy."""
    return parse_deals(
        product.deal_evidence,
        current_price=product.sale_price or product.regular_price or None,
        **policy,
    )


@dataclass
class ProductRecord:
    """An exported item plus the metadata needed to enforce crawl policy.

    Pipelines have to decide things like "this seller is marketplace" or "this
    offer was shipping only", which the 22 exported columns deliberately do not
    carry.  Wrapping the item keeps that metadata available to the pipeline chain
    without polluting the output schema.
    """

    item: WalmartProductItem
    product: ParsedProduct
    store_id: str = ""
    category_url: str = ""
    listing_url: str = ""
    page: int = 1
    sponsored: bool = False

    def to_ordered_dict(self) -> "OrderedDict[str, Any]":
        return self.item.to_ordered_dict()
