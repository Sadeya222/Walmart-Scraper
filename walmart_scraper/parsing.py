"""Extraction from Walmart's embedded state (``__NEXT_DATA__``) and DOM fallbacks.

Walmart renders category and product pages as a Next.js app whose full data
payload is embedded in a ``<script id="__NEXT_DATA__">`` block.  Parsing that JSON
is far more reliable than scraping markup, so it is the primary path; a
selector-driven DOM pass (``config/selectors.json``) is the fallback for layouts
that ship less state.

Naming note that trips people up: Walmart calls its price-per-measure object
``priceInfo.unitPrice`` (``priceString`` = ``"3\u00a2/fl oz"``).  That value is what
this project exports as **``ppm``**; the exported field called ``unit_price`` is a
different, always-empty column that is computed downstream.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .constants import IN_STORE_FULFILLMENT_TOKENS, WALMART_SELLER_IDS, WALMART_SELLER_NAMES
from .text import (
    absolute_url,
    clean_text,
    format_price,
    safe_str,
    validate_ppm,
)

__all__ = [
    "ParsedProduct",
    "extract_listing_products",
    "extract_pdp_product",
    "merge_products",
    "is_marketplace",
    "is_shipping_only",
    "normalize_upc",
    "search_result",
    "DEFAULT_BASE_URL",
]

_DIGITS_RE = re.compile(r"\D+")
# No spaces in the unit class: allowing them let a free-text scan capture
# "$0.25/oz at this s".  The authoritative source is priceInfo.unitPrice anyway.
_MEASURE_IN_TEXT_RE = re.compile(
    r"(?:\$|\u00a2)?\s*\d+(?:\.\d{1,4})?\s*(?:\u00a2)?\s*(?:/|per)\s*[a-z\u00b2\u00b3]{1,10}",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
@dataclass
class ParsedProduct:
    """Everything the exporter needs about one product, from listing and/or PDP."""

    us_item_id: str = ""
    title: str = ""
    url: str = ""
    image_url: str = ""
    brand: str = ""
    upc: str = ""
    weight_candidates: list[str] = field(default_factory=list)
    regular_price: str = ""
    sale_price: str = ""
    ppm: str = ""
    deal_evidence: list[str] = field(default_factory=list)
    seller_name: str = ""
    seller_id: str = ""
    availability: str = ""
    fulfillment: list[str] = field(default_factory=list)
    product_location: str = ""
    sponsored: bool = False
    source: str = "listing"      # "listing" | "pdp" | "merged"
    raw_flags: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        """Stable dedupe key component (US item id when present, else URL)."""
        return self.us_item_id or self.url

    def merge_from(self, other: "ParsedProduct") -> "ParsedProduct":
        """Fill blanks in ``self`` from ``other`` (PDP data wins on conflicts)."""
        for name in (
            "us_item_id", "title", "url", "image_url", "brand", "upc",
            "regular_price", "sale_price", "ppm", "seller_name", "seller_id",
            "availability", "product_location",
        ):
            existing = getattr(self, name)
            incoming = getattr(other, name)
            if not existing and incoming:
                setattr(self, name, incoming)
        # Tiles sometimes truncate the title, so prefer the longer of the two.
        # Taking the PDP title unconditionally would let a short PDP name
        # overwrite a complete tile title.
        if len(other.title) > len(self.title):
            self.title = other.title
        for name in ("weight_candidates", "deal_evidence", "fulfillment", "raw_flags"):
            merged: list[str] = []
            for value in list(getattr(self, name)) + list(getattr(other, name)):
                if value and value not in merged:
                    merged.append(value)
            setattr(self, name, merged)
        if not self.ppm and other.ppm:
            self.ppm = other.ppm
        self.source = "merged"
        return self


# --------------------------------------------------------------------------- #
# Small path helpers - Walmart nests the same data differently across layouts.
# --------------------------------------------------------------------------- #
def _walk(obj: Any, path: str) -> Any:
    node = obj
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def _first(obj: Any, *paths: str) -> Any:
    for path in paths:
        value = _walk(obj, path)
        if value not in (None, "", [], {}):
            return value
    return None


def search_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Locate the search result object inside a category page payload."""
    if not isinstance(payload, dict):
        return {}
    for path in (
        # Verified on a captured walmart.com /search payload: the real product
        # array sits one level deeper than initialData.
        "props.pageProps.initialData.data.searchResult",
        "props.pageProps.initialData.searchResult",
        "props.pageProps.initialData",
        "props.pageProps.searchResult",
        "props.initialData.searchResult",
        "searchResult",
    ):
        found = _walk(payload, path)
        if not isinstance(found, dict):
            continue
        if found.get("itemStacks") or found.get("items") or found.get("itemsV2"):
            return found
    return {}


def _item_stacks(result: dict[str, Any]) -> list[dict[str, Any]]:
    stacks = result.get("itemStacks")
    if isinstance(stacks, list):
        return [stack for stack in stacks if isinstance(stack, dict)]
    if isinstance(result.get("items"), list) or isinstance(result.get("itemsV2"), list):
        return [result]
    return []


def _stack_items(stack: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("itemsV2", "items", "gridItems"):
        items = stack.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _item_core(item: dict[str, Any]) -> dict[str, Any]:
    """Some layouts nest the product under ``item['product']``."""
    core = item.get("product") if isinstance(item.get("product"), dict) else None
    return core or item


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# --------------------------------------------------------------------------- #
# Deal / flag evidence
# --------------------------------------------------------------------------- #
def _badge_texts(item: dict[str, Any]) -> list[str]:
    """Collect every offer string Walmart attaches to a tile.

    Sources seen in the wild: ``badges.flags[].text``, ``badges.tags[].text``,
    ``productFlags``, ``promotions[].description``, ``offerMessages``.  All are
    harvested as raw strings - classification happens later in ``deals.py``.
    """
    texts: list[str] = []

    def add(value: Any) -> None:
        text = clean_text(value)
        if text and text not in texts:
            texts.append(text)

    badges = item.get("badges")
    if isinstance(badges, dict):
        for key in ("flags", "tags", "badges"):
            for badge in _as_list(badges.get(key)):
                if isinstance(badge, dict):
                    add(badge.get("text") or badge.get("label") or badge.get("title"))
                else:
                    add(badge)
    elif isinstance(badges, list):
        for badge in badges:
            add(badge.get("text") if isinstance(badge, dict) else badge)

    for key in ("productFlags", "flags", "tags", "labels"):
        for flag in _as_list(item.get(key)):
            if isinstance(flag, dict):
                add(flag.get("text") or flag.get("label") or flag.get("flag"))
            else:
                add(flag)

    for promo in _as_list(item.get("promotions")):
        if isinstance(promo, dict):
            add(promo.get("description") or promo.get("title") or promo.get("text"))
        else:
            add(promo)

    for message in _as_list(item.get("offerMessages") or item.get("promoMessages")):
        if isinstance(message, dict):
            add(message.get("message") or message.get("text") or message.get("shortMessage"))
        else:
            add(message)

    for key in ("couponText", "digitalCouponText", "promotionText", "offerText"):
        add(item.get(key))

    return texts


# --------------------------------------------------------------------------- #
# Listing extraction
# --------------------------------------------------------------------------- #
def extract_listing_products(payload: dict[str, Any] | None, *, base_url: str = "") -> list[ParsedProduct]:
    """Parse every product tile from a category page payload."""
    result = search_result(payload)
    products: list[ParsedProduct] = []
    seen: set[str] = set()

    for stack in _item_stacks(result):
        for raw_item in _stack_items(stack):
            item = _item_core(raw_item)
            product = _parse_product_node(item, source="listing", base_url=base_url)
            if not product or not product.identity:
                continue
            if product.identity in seen:
                # Sponsored placements repeat the same item inside one page.
                continue
            seen.add(product.identity)
            products.append(product)
    return products


DEFAULT_BASE_URL = "https://www.walmart.com/"


def _parse_product_node(
    item: dict[str, Any], *, source: str, base_url: str = ""
) -> ParsedProduct | None:
    """Build a :class:`ParsedProduct` from one listing/PDP product object."""
    us_item_id = safe_str(
        _first(item, "usItemId", "id", "productId", "itemId", "product.usItemId")
    )
    title = clean_text(_first(item, "title", "name", "productTitle", "displayName"))
    canonical = clean_text(
        _first(item, "canonicalUrl", "productPageUrl", "url", "seoData.canonicalUrl")
    )
    if not us_item_id and not canonical:
        return None

    origin = _origin(base_url) or DEFAULT_BASE_URL
    url = absolute_url(canonical, origin) if canonical else ""
    if not url and us_item_id:
        url = absolute_url(f"/ip/{us_item_id}", origin)

    image_url = clean_text(
        _first(
            item,
            "imageInfo.thumbnailUrl",
            "imageInfo.allImages.0.url",
            "imageInfo.size.0.url",
            "imageUrl",
            "image",
        )
    )
    image_url = absolute_url(image_url) if image_url else ""

    price_info = item.get("priceInfo") if isinstance(item.get("priceInfo"), dict) else {}
    price_node = item.get("price") if isinstance(item.get("price"), dict) else {}
    price_source = price_info or price_node

    # Real listing tiles leave currentPrice/itemPrice/wasPrice/unitPrice empty
    # and keep minPrice at 0, so a structured zero must not be read as a price:
    # the amount lives in priceInfo.priceDetails.priceLines[].
    line_prices = _price_line_amounts(price_info)
    current = _first(price_source, "currentPrice.price", "price", "amount")
    if not _usable_amount(current):
        current = _first(price_source, "minPrice")
    if not _usable_amount(current):
        current = line_prices.get("current")
    was = _first(price_source, "wasPrice.price", "originalPrice.price")
    if not _usable_amount(was):
        was = _first(price_source, "wasPrice")
    if not _usable_amount(was):
        was = line_prices.get("was")
    if not _usable_amount(current):
        current = None
    if not _usable_amount(was):
        was = None
    # Walmart's price-per-measure object is called "unitPrice" - see module docstring.
    ppm_raw = _first(
        price_source,
        "unitPrice.priceString",
        "unitPrice.price",
        "pricePerMeasure",
        "pricePerUnit",
        "unitPriceString",
    )

    regular, sale = _split_prices(current, was)

    fulfillment: list[str] = []
    for value in _as_list(_first(item, "fulfillmentType", "fulfillmentBadges", "availability.fulfillment")):
        if isinstance(value, dict):
            token = safe_str(value.get("type") or value.get("fulfillmentType") or value.get("text"))
        else:
            token = safe_str(value)
        if token and token not in fulfillment:
            fulfillment.append(token)

    product = ParsedProduct(
        us_item_id=us_item_id,
        title=title,
        url=url,
        image_url=image_url,
        brand=clean_text(
            _first(
                item,
                "brand",
                "productBrand.name",
                "productBrand",
                "brandName",
                "productSeoData.brand",
            )
        ),
        upc=normalize_upc(_first(item, "gtin", "upc", "gtin13", "gtin12", "productSeoData.gtin")),
        weight_candidates=_weight_candidates(item),
        regular_price=regular,
        sale_price=sale,
        ppm=validate_ppm(ppm_raw),
        deal_evidence=_badge_texts(item),
        seller_name=clean_text(_first(item, "sellerName", "seller.name", "soldAndShippedBy")),
        seller_id=safe_str(_first(item, "sellerId", "seller.id")),
        availability=safe_str(_first(item, "availabilityStatus", "availability.status", "stock")),
        fulfillment=fulfillment,
        product_location=_location_label(item),
        sponsored=_is_sponsored(item),
        source=source,
        raw_flags=[safe_str(flag) for flag in _as_list(_first(item, "productClass", "classType"))],
    )
    return product


# lineType -> which slot the amount fills.  Verified on a captured /search
# payload: [{"lineType": "CURRENT_PRICE",
#            "values": [{"key": "PRICE", "value": "69.99"}]}]
_PRICE_LINE_SLOTS = {
    "CURRENT_PRICE": "current",
    "DISCOUNTED_PRICE": "current",
    "WAS_PRICE": "was",
    "PREVIOUS_PRICE": "was",
    "COMPARISON_PRICE": "was",
    "COMPARISON": "was",
}


def _usable_amount(value: Any) -> bool:
    """True for a real monetary amount.  Walmart sends ``minPrice: 0`` on tiles
    whose price is not yet resolved, and that is not a price."""
    amount = _parse_amount(value)
    return amount is not None and amount > 0


def _parse_amount(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\d+(?:[,.]\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def _price_line_amounts(price_info: Mapping[str, Any]) -> dict[str, float | None]:
    """``{"current": …, "was": …}`` read from ``priceDetails.priceLines``."""
    found: dict[str, float | None] = {}
    for line in _as_list(_walk(price_info, "priceDetails.priceLines")):
        if not isinstance(line, dict):
            continue
        line_type = clean_text(safe_str(line.get("lineType"))).upper()
        for entry in _as_list(line.get("values")):
            if not isinstance(entry, dict):
                continue
            key = clean_text(safe_str(entry.get("key"))).upper()
            if key == "WAS_PRICE":
                slot = "was"
            elif key == "PRICE":
                slot = _PRICE_LINE_SLOTS.get(line_type, "current")
            else:
                continue
            amount = _parse_amount(entry.get("value"))
            if amount is not None and amount > 0:
                found.setdefault(slot, amount)
    return found


def _origin(url: str) -> str:
    """``https://host[:port]`` for a page URL, or ``""`` when not absolute."""
    text = clean_text(url)
    if "://" not in text:
        return ""
    scheme, _, rest = text.partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0]
    return f"{scheme}://{host}/" if host else ""


def _split_prices(current: Any, was: Any) -> tuple[str, str]:
    """Map Walmart's current/was pair onto ``regular_price`` / ``sale_price``.

    * a genuine markdown (was > current) -> regular = was, sale = current;
    * otherwise the shelf price is the regular price and there is no sale price.
    """
    current_str = format_price(current)
    was_str = format_price(was)
    if was_str and current_str and float(was_str) > float(current_str):
        return was_str, current_str
    return current_str, ""


def _weight_candidates(item: dict[str, Any]) -> list[str]:
    """Weight-ish strings in priority order (most authoritative first)."""
    candidates: list[str] = []

    def add(value: Any) -> None:
        text = clean_text(value)
        if text and text not in candidates:
            candidates.append(text)

    for path in (
        "dimensions.weight",
        "dimensions.shippingWeight",
        "productWeight",
        "shippingWeight",
        "weight",
    ):
        node = _walk(item, path)
        if isinstance(node, dict):
            unit = safe_str(node.get("unit") or node.get("unitOfMeasure") or node.get("measurement"))
            value = safe_str(node.get("value") or node.get("weight") or node.get("amount"))
            add(f"{value} {unit}".strip())
        elif node is not None:
            add(node)

    # Specifications / "About this item" frequently carry "Net Weight: 16 oz".
    for path in ("specifications", "specificationsList", "features", "aboutThisItem", "keyFeatures"):
        for entry in _as_list(_walk(item, path)):
            if isinstance(entry, dict):
                label = safe_str(entry.get("name") or entry.get("label") or entry.get("key"))
                value = safe_str(entry.get("value") or entry.get("content") or entry.get("text"))
                if "weight" in label.lower() or "weight" in value.lower():
                    add(f"{label}: {value}" if label else value)
            elif isinstance(entry, str):
                if "weight" in entry.lower():
                    add(entry)

    size = clean_text(_first(item, "size", "unitSize", "displaySize", "containerSize"))
    if size:
        add(size)
    return candidates


def _location_label(item: dict[str, Any]) -> str:
    """Aisle/shelf text, or ``""``.

    A real product node also carries a structured ``location`` object
    (``postalCode``/``storeIds``/``city``); that is store context, not a shelf
    label, so a dict value is never rendered into the ``store_location`` field.
    """
    for path in ("productLocation", "aisleLocation", "location"):
        value = _walk(item, path)
        if isinstance(value, str) and clean_text(value):
            return clean_text(value)
    return ""


def _is_sponsored(item: dict[str, Any]) -> bool:
    sponsored = _first(item, "sponsoredProduct", "isSponsored", "sponsored", "adInfo")
    if isinstance(sponsored, bool):
        return sponsored
    text = safe_str(sponsored).lower()
    return text in {"sponsored", "true", "yes", "1"} or bool(item.get("adInfo"))


# --------------------------------------------------------------------------- #
# PDP extraction
# --------------------------------------------------------------------------- #
def extract_pdp_product(payload: dict[str, Any] | None, *, base_url: str = "") -> ParsedProduct | None:
    """Parse a product detail page payload (adds UPC, weight, PPM, brand)."""
    if not isinstance(payload, dict):
        return None
    product = None
    for path in (
        # Verified on a captured walmart.com product page: the product lives at
        # props.pageProps.initialData.data.product (extra "data" level).
        "props.pageProps.initialData.data.product",
        "props.pageProps.initialData.product",
        "props.pageProps.product",
        "props.pageProps.initialData",
        "props.initialData.product",
    ):
        node = _walk(payload, path)
        if isinstance(node, dict) and (node.get("usItemId") or node.get("name") or node.get("title")):
            product = node
            break
    if product is None:
        return None

    parsed = _parse_product_node(product, source="pdp", base_url=base_url)
    if parsed is None:
        return None

    # PDP-only UPC sources, in decreasing reliability.
    upc = parsed.upc or normalize_upc(
        _first(
            product,
            "gtin", "upc", "gtin13", "gtin12", "sku",
            "productSeoData.gtin", "productSeoData.upc", "seoData.gtin",
            "identifiers.upc", "identifiers.gtin",
        )
    )
    parsed.upc = upc

    # PPM may only be rendered in the offer block or as free text.
    if not parsed.ppm:
        for path in (
            "price.unitPrice.priceString",
            "price.pricePerMeasure",
            "price.pricePerUnit",
            "offer.unitPrice.priceString",
        ):
            candidate = validate_ppm(_first(product, path))
            if candidate:
                parsed.ppm = candidate
                break
    if not parsed.ppm:
        for text in _all_strings(product):
            match = _MEASURE_IN_TEXT_RE.search(text)
            if match:
                candidate = validate_ppm(match.group(0))
                if candidate:
                    parsed.ppm = candidate
                    break

    # A real PDP keeps its spec sheet at initialData.data.idml.specifications -
    # a sibling of the product node, so it is not reached by _weight_candidates.
    for candidate in _specification_candidates(payload):
        if candidate not in parsed.weight_candidates:
            parsed.weight_candidates.append(candidate)
    if not parsed.weight_candidates:
        parsed.weight_candidates = _weight_candidates(product)
    return parsed


def _specification_candidates(payload: dict[str, Any]) -> list[str]:
    """Weight-ish strings from the IDML spec sheet beside the product node."""
    for path in (
        "props.pageProps.initialData.data",
        "props.pageProps.initialData",
        "props.pageProps",
    ):
        node = _walk(payload, path)
        specs = _walk(node, "idml.specifications") if isinstance(node, dict) else None
        if _as_list(specs):
            return _weight_candidates({"specifications": specs})
    return []


def _all_strings(node: Any, limit: int = 400) -> Iterable[str]:
    """Yield every string in a JSON structure (used as a last-resort PPM scan)."""
    produced = 0
    stack = [node]
    while stack and produced < limit:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and current.strip():
            produced += 1
            yield current


# --------------------------------------------------------------------------- #
# UPC
# --------------------------------------------------------------------------- #
def normalize_upc(value: Any) -> str:
    """Keep only digits from a GTIN/UPC/SKU value.  Never generates or pads one.

    Walmart exposes ``gtin`` (14 digits), ``gtin13`` and ``sku``; the exported
    ``upc`` column carries whatever Walmart publishes, digits only.  A SKU that is
    not a barcode still lands here as-is rather than being silently dropped.
    """
    text = safe_str(value)
    if not text:
        return ""
    digits = _DIGITS_RE.sub("", text)
    return digits


# --------------------------------------------------------------------------- #
# Seller / fulfillment policy
# --------------------------------------------------------------------------- #
def is_marketplace(product: ParsedProduct) -> bool:
    """True when the offer is from a third-party marketplace seller."""
    seller_name = product.seller_name.strip().lower()
    seller_id = product.seller_id.strip().lower()
    if seller_name and seller_name not in WALMART_SELLER_NAMES:
        return True
    if not seller_name and seller_id and seller_id not in WALMART_SELLER_IDS:
        return True
    return False


def is_shipping_only(product: ParsedProduct, *, require_tokens: Sequence[str] = ()) -> bool:
    """True when the offer cannot be collected at the selected store.

    Walmart omits ``fulfillmentType`` on many tiles, so an *absent* list is not
    treated as shipping-only (that would discard most of a category).  Only an
    explicit ship-only signal is, or the absence of any in-store token when the
    caller demands one (``fulfillment_mode=pickup|in_store``).
    """
    tokens = [token.strip().lower() for token in product.fulfillment if token.strip()]
    explicit_ship_only = any(
        token in {"ship", "shipping", "ship_only", "shipping_only", "direct_from_manufacturer"}
        for token in tokens
    )
    has_store_token = any(
        any(needle in token for needle in IN_STORE_FULFILLMENT_TOKENS) for token in tokens
    )
    if explicit_ship_only and not has_store_token:
        return True
    if require_tokens and tokens and not has_store_token:
        return True
    return False


def merge_products(listing: ParsedProduct, pdp: ParsedProduct | None) -> ParsedProduct:
    """Combine tile data with PDP data (PDP wins where it has a value)."""
    if pdp is None:
        listing.source = "listing"
        return listing
    merged = ParsedProduct(**{**listing.__dict__})
    merged.merge_from(pdp)
    return merged
