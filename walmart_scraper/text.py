"""Deterministic text, price, measure and brand normalisation helpers.

Rules enforced here (see README "Data rules"):

* missing values become ``""`` - never ``None`` / ``null``;
* prices are emitted as bare two-decimal numbers (``4.99``), no currency symbol;
* a price-per-measure string is **never** computed - it is only ever validated
  and trimmed, so ``$0.25/oz`` survives verbatim and a blank stays blank;
* brand comparison for private-label detection ignores case, punctuation and
  accents, and matches whole phrases only (``Onn`` must not match ``Bonnie``).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from .constants import EMPTY, MEASURE_UNITS, STORE_BRAND_NO, DEFAULT_PRIVATE_LABEL_BRANDS

__all__ = [
    "clean_text",
    "safe_str",
    "to_decimal",
    "format_price",
    "normalize_price",
    "looks_like_price",
    "validate_ppm",
    "normalize_measure",
    "absolute_url",
    "strip_tracking",
    "fold",
    "brand_is_private_label",
    "store_brand_for",
    "dedupe_preserve_order",
    "truncate",
]

_WHITESPACE_RE = re.compile(r"[\s\u00a0\u200b\u2028\u2029]+")
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
# "$1,234.56" / "1234.56" / "12,34" - US pricing only, Walmart.com is en-US.
_PRICE_RE = re.compile(r"-?\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_PRICE_STRICT_RE = re.compile(r"^\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?$")
_CENTS_RE = re.compile(r"^\$?\s*(\d+)[\u00a0 ]?(\d{2})\b")  # "$4 99" style tiles
# "$0.25/oz", "$3.99 / lb", "25¢/oz", "$0.25 per oz"
_PPM_RE = re.compile(
    r"(?P<price>(?:\$|usd\s?)?\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?|\d{1,3}(?:\.\d{1,4})?\s*[c\u00a2])"
    r"\s*(?:/|per)\s*"
    r"(?P<unit>[a-z][a-z\u00b2\u00b3 .]*)"
)
_MEASURE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[a-z][a-z\u00b2\u00b3 .]{0,11})"
)
_UNIT_LOOKUP = frozenset(UNIT.lower() for UNIT in MEASURE_UNITS)


# --------------------------------------------------------------------------- #
# Strings
# --------------------------------------------------------------------------- #
def clean_text(value: Any) -> str:
    """Collapse whitespace/zero-width characters; ``None`` becomes ``""``."""
    if value is None:
        return EMPTY
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        value = str(value)
    return _WHITESPACE_RE.sub(" ", value.replace("\u00a0", " ")).strip()


def safe_str(value: Any) -> str:
    """Coerce to a clean string, never returning ``None``."""
    if value is None:
        return EMPTY
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return clean_text(value)


def fold(value: str) -> str:
    """Case/accent/punctuation-insensitive comparison key.

    ``"Better Homes & Gardens"`` -> ``"better homes gardens"``,
    ``"onn."`` -> ``"onn"``.
    """
    text = unicodedata.normalize("NFKD", value or EMPTY)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _PUNCT_RE.sub(" ", text.lower()).strip()


def truncate(value: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary with an ellipsis."""
    text = value or EMPTY
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)].rstrip()
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return f"{cut}\u2026"


def dedupe_preserve_order(values: Iterable[str], separator: str = " | ") -> str:
    """Join unique non-empty strings, keeping first-seen order."""
    seen: list[str] = []
    lowered: set[str] = set()
    for raw in values:
        text = clean_text(raw)
        if not text:
            continue
        key = fold(text)
        if key in lowered:
            continue
        lowered.add(key)
        seen.append(text)
    return separator.join(seen)


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def to_decimal(value: Any) -> Decimal | None:
    """Parse a price-ish value into ``Decimal`` or return ``None``.

    Accepts ``Decimal``/``int``/``float``, ``"$1,234.56"``, ``"4.99"``,
    ``"4 99"`` and ``"$4 99"``.  Returns ``None`` for junk such as
    ``"$2.50 each"`` (extra text) so callers cannot mis-parse prose.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if isinstance(value, int):
        return Decimal(value).quantize(Decimal("0.01"))
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    text = clean_text(value)
    if not text:
        return None
    if text.lower() in {"n/a", "na", "none", "null", "-", "--"}:
        return None

    match = _PRICE_STRICT_RE.match(text)
    if not match:
        cents = _CENTS_RE.match(text)
        if not cents:
            return None
        text = f"{cents.group(1)}.{cents.group(2)}"

    normalised = text.replace("$", "").replace(" ", "").replace(",", "")
    try:
        return Decimal(normalised).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def format_price(value: Any) -> str:
    """``4.99`` / ``""``.  Never emits a currency symbol and never ``null``."""
    amount = to_decimal(value)
    if amount is None:
        return EMPTY
    return f"{amount:.2f}"


def normalize_price(value: Any, *, style: str = "numeric") -> str:
    """Normalise a price for export.

    ``style="numeric"`` (default) -> ``"4.99"``; ``style="display"`` -> ``"$4.99"``
    (the price Walmart shows).  Both are explicit so downstream consumers get a
    single documented shape.
    """
    amount = to_decimal(value)
    if amount is None:
        return EMPTY
    if style == "display":
        return f"${amount:,.2f}"
    return f"{amount:.2f}"


def looks_like_price(value: Any) -> bool:
    """True when ``value`` is a bare price and nothing more."""
    return to_decimal(value) is not None


# --------------------------------------------------------------------------- #
# Price per measure / measures
# --------------------------------------------------------------------------- #
def validate_ppm(value: Any) -> str:
    """Return the page's price-per-measure string verbatim, or ``""``.

    This function **never builds** a PPM.  It only accepts a string that already
    contains a price and a known unit - ``"$0.25/oz"``, ``"$3.99 / lb"``,
    ``"25\u00a2/oz"`` - and cleans up surrounding whitespace.  Anything else,
    including a bare ``"$3.99"`` or a bare ``"oz"``, yields ``""`` so the record
    cannot claim a unit price Walmart did not display.
    """
    text = clean_text(value)
    if not text or len(text) > 60:
        return EMPTY
    lowered = text.lower()
    if "approx" in lowered:
        return EMPTY
    match = _PPM_RE.search(lowered)
    if not match:
        return EMPTY
    # Exact membership only.  A "first word" fallback would accept prose such as
    # "oz at this size"; multi-word units ("fl oz", "sq ft") are already listed.
    unit = match.group("unit").strip(" .")
    if unit not in _UNIT_LOOKUP:
        return EMPTY
    # Keep the caller's original casing/prefix; only trim outer whitespace.
    return text


def normalize_measure(value: Any) -> str:
    """Normalise a size/weight phrase such as ``"16 FL OZ"`` -> ``"16 fl oz"``.

    Returns ``""`` when the text carries no recognised unit, so a stray label can
    never be exported as a weight.
    """
    text = clean_text(value)
    if not text:
        return EMPTY
    match = _MEASURE_RE.search(text.lower())
    if not match:
        return EMPTY
    unit = match.group("unit").strip(" .")
    if unit not in _UNIT_LOOKUP:
        return EMPTY
    number = match.group("value").replace(",", ".")
    return f"{number} {unit}"


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
_TRACKING_PARAMS = frozenset(
    {
        "athena_location_id", "athena_location_type", "affinityOverride",
        "adUid", "wl13", "wmlspartner", "u1", "u2", "u3", "veh", "wpa_bd",
        "wpa_pg_id", "wpa_pos", "wpa_ta_id", "wpa_mod",
    }
)


def absolute_url(url: Any, base: str = "https://www.walmart.com/") -> str:
    """Resolve ``/ip/Slug/123`` against Walmart's origin; ``""`` stays ``""``."""
    text = clean_text(url)
    if not text:
        return EMPTY
    if text.startswith("//"):
        return f"https:{text}"
    if "://" in text:
        return text
    return urljoin(base, text if text.startswith("/") else f"/{text}")


def strip_tracking(url: Any) -> str:
    """Drop Walmart click-tracking query params while preserving identity params.

    The product URL itself is preserved completely (requirement: "Preserve
    original product and image URLs completely"); only volatile ad/click
    tracking keys are removed so dedupe is not defeated by them.  Called on the
    *dedupe key* only, never on the exported ``url`` value.
    """
    text = clean_text(url)
    if not text:
        return EMPTY
    parts = urlparse(text)
    if not parts.query:
        return text
    pairs = [
        pair for pair in parts.query.split("&")
        if pair and pair.split("=", 1)[0] not in _TRACKING_PARAMS
    ]
    return urlunparse(parts._replace(query="&".join(pairs)))


# --------------------------------------------------------------------------- #
# Private label detection
# --------------------------------------------------------------------------- #
def brand_is_private_label(
    brand: Any,
    extra_brands: Iterable[str] | None = None,
) -> bool:
    """True when ``brand`` is a Walmart private label.

    Matching uses :func:`fold` and requires the private-label name to appear as
    a whole phrase inside the brand string, so ``"Equate"`` and
    ``"Equate (Walmart)"`` match while ``"Bonnie Plants"`` does not match ``onn``.
    """
    key = fold(safe_str(brand))
    if not key or key in {"-", "n/a", "na", "none", "unbranded", "generic", "no brand"}:
        return False

    candidates = {fold(name) for name in DEFAULT_PRIVATE_LABEL_BRANDS if fold(name)}
    candidates |= {fold(name) for name in (extra_brands or ()) if fold(name)}

    for candidate in candidates:
        # Whole phrase match on folded text.
        if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", key):
            return True
    return False


def store_brand_for(
    brand: Any,
    extra_brands: Iterable[str] | None = None,
    *,
    yes_value: str = "yes",
) -> str:
    """``store_brand`` value: ``"yes"`` for private labels else ``"no"``."""
    return yes_value if brand_is_private_label(brand, extra_brands) else STORE_BRAND_NO
