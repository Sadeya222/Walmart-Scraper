"""Volume vs. digital deal classification.

The two deal families are defined by *what the shopper has to do*, not by the
wording on the tile:

``volume_deals_*``
    Applies automatically at the register - "2 for $5", "Buy 1, Get 1 Free",
    "Must buy 4 at $1.99 each".  No clipping, no account action.

``digital_deals_*``
    Requires the customer to clip/activate something first - "Clip Coupon:
    Save $2", "$3.99 each with Digital Coupon".

Hard rules implemented here:

* Descriptions keep Walmart's **complete original terms**, joined with ``" | "``
  and deduped.  We never paraphrase and never drop limit text.
* A ``*_deals_price`` is only populated with a price Walmart **displays**.
  "Clip Coupon: Save $2" yields a description and an empty price, because $2 is a
  discount, not a shelf price.  Deriving the post-coupon price is available as an
  explicit opt-in (``apply_coupon_discount``) and is off by default.
* "Buy 1, Get 1 Free" gets no price unless a price is shown - we do not invent
  an implied 50% value.
* Cash-back style offers (Walmart Cash, rebates, "Get $5 back"), membership
  pricing (Walmart+ Savings Pass) and straight price cuts (Rollback, clearance)
  are excluded from both families: the first two are not price reductions, the
  third is already reflected in ``sale_price``.
* Manufacturer offers may appear in ``digital_deals_description`` (they are real
  terms the shopper sees) but never contribute a ``digital_deals_price``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

from .constants import (
    CASHBACK_MARKERS,
    DIGITAL_DEAL_MARKERS,
    MANUFACTURER_OFFER_MARKERS,
    MEMBERSHIP_MARKERS,
    PRICE_CUT_ONLY_MARKERS,
    VOLUME_DEAL_MARKERS,
)
from .text import clean_text, dedupe_preserve_order, fold, format_price, to_decimal

__all__ = [
    "Deal",
    "condense_terms",
    "DealResult",
    "parse_deals",
    "classify_offer_text",
    "DEAL_CLASS_VOLUME",
    "DEAL_CLASS_DIGITAL",
    "DEAL_CLASS_IGNORED",
]

DEAL_CLASS_VOLUME = "volume"
DEAL_CLASS_DIGITAL = "digital"
DEAL_CLASS_IGNORED = "ignored"

MAX_DESCRIPTION_CHARS = 400

_MONEY = r"\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"

# "2 for $5", "3/$6.00", "2 for $5.00 ($2.50 each)"
_N_FOR_X_RE = re.compile(
    rf"(?P<count>\d{{1,2}})\s*(?:for|/)\s*(?P<total>{_MONEY})"
)
# "must buy 4 at $1.99 each" / "4 for $1.99 each" / "$1.99 each when you buy 4"
_EACH_PRICE_RE = re.compile(
    rf"(?:at|for)?\s*(?P<price>{_MONEY})\s*(?:each|ea|per\s+item)\b", re.IGNORECASE
)
# "now $4.99" / "only $4.99" / "just $4.99"
_NOW_ONLY_RE = re.compile(rf"\b(?:now|only|just)\s*(?P<price>{_MONEY})", re.IGNORECASE)
# "Buy 1, Get 1 Free" / "B1G1" / "buy 2 get 1 50% off"
_BOGO_RE = re.compile(
    r"\b(?:buy|b)\s*(?P<buy>\d+|one)\s*[,]?\s*(?:get|g)\s*(?P<get>\d+|one)"
    r"(?:\s*(?P<benefit>free|\d{1,3}\s*%\s*off))?",
    re.IGNORECASE,
)
# "when you buy 2", "must buy 4"
_BUY_N_RE = re.compile(r"\b(?:must\s+buy|when\s+you\s+buy|buy)\s+(?P<count>\d{1,2})\b", re.IGNORECASE)
# "Save $2", "$2 off", "save up to $3.50"
_DISCOUNT_RE = re.compile(
    rf"\b(?:save\s+(?:up\s+to\s+)?|take\s+)(?P<amount>{_MONEY})"
    rf"|(?P<amount2>{_MONEY})\s*(?:off|discount)\b",
    re.IGNORECASE,
)
_PERCENT_OFF_RE = re.compile(r"(?P<percent>\d{1,3})\s*%\s*off", re.IGNORECASE)


@dataclass(frozen=True)
class Deal:
    """One classified offer, carrying Walmart's verbatim wording."""

    text: str
    kind: str
    source: str = ""
    price: str = ""          # displayed resulting price, "" when not shown
    discount: str = ""       # displayed discount amount, "" when not shown
    price_basis: str = ""    # "each" | "offer_total" | "now" | "discount" | ""


@dataclass
class DealResult:
    """The four exported deal fields plus non-exported diagnostics."""

    volume_description: str = ""
    volume_price: str = ""
    digital_description: str = ""
    digital_price: str = ""
    deals: list[Deal] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, str]:
        return {
            "volume_deals_description": self.volume_description,
            "volume_deals_price": self.volume_price,
            "digital_deals_description": self.digital_description,
            "digital_deals_price": self.digital_price,
        }


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _money(match: re.Match | None, *groups: str) -> str:
    """First non-empty capture group in ``groups``, formatted as a price."""
    if match is None:
        return ""
    for group in groups:
        try:
            raw = match.group(group)
        except IndexError:  # pragma: no cover - defensive
            continue
        if raw:
            return format_price(raw)
    return ""


def classify_offer_text(text: str, *, include_manufacturer: bool = True) -> str:
    """Return ``"volume"``, ``"digital"`` or ``"ignored"`` for one offer string.

    ``include_manufacturer=False`` drops manufacturer coupons entirely instead of
    recording their terms in ``digital_deals_description``.
    """
    folded = fold(text)
    # Markers are matched on folded text, but the structural regexes need the
    # punctuation kept ("3/$6.00" would otherwise become "3 6 00").
    literal = clean_text(text).lower()
    if not folded:
        return DEAL_CLASS_IGNORED
    if _contains_any(folded, CASHBACK_MARKERS):
        return DEAL_CLASS_IGNORED
    if _contains_any(folded, MEMBERSHIP_MARKERS):
        return DEAL_CLASS_IGNORED
    if _contains_any(folded, PRICE_CUT_ONLY_MARKERS):
        return DEAL_CLASS_IGNORED
    # "Clip coupon: 2 for $5" needs clipping, so the digital rule wins over the
    # volume wording it happens to contain.
    if _contains_any(folded, DIGITAL_DEAL_MARKERS):
        return DEAL_CLASS_DIGITAL
    # Manufacturer coupons are real terms the shopper sees, but their stated
    # amount is never a shelf price - see ``parse_deals``.
    if include_manufacturer and _contains_any(folded, MANUFACTURER_OFFER_MARKERS):
        return DEAL_CLASS_DIGITAL
    if _contains_any(folded, VOLUME_DEAL_MARKERS):
        return DEAL_CLASS_VOLUME
    if _N_FOR_X_RE.search(literal) or _BOGO_RE.search(literal):
        return DEAL_CLASS_VOLUME
    if _BUY_N_RE.search(literal):
        return DEAL_CLASS_VOLUME
    return DEAL_CLASS_IGNORED


def _extract_price(text: str, *, prefer_each: bool = True) -> tuple[str, str]:
    """Return ``(displayed_price, basis)`` from a single offer string."""
    each = _EACH_PRICE_RE.search(text)
    n_for_x = _N_FOR_X_RE.search(text)
    now = _NOW_ONLY_RE.search(text)

    if each and (prefer_each or not n_for_x):
        return _money(each, "price"), "each"
    if n_for_x:
        return _money(n_for_x, "total"), "offer_total"
    if each:
        return _money(each, "price"), "each"
    if now:
        return _money(now, "price"), "now"
    return "", ""


def _extract_discount(text: str) -> str:
    discount = _money(_DISCOUNT_RE.search(text), "amount", "amount2")
    if discount:
        return discount
    percent = _PERCENT_OFF_RE.search(text)
    if percent:
        return f"{int(percent.group('percent'))}%"
    return ""


def parse_deals(
    evidence: Iterable[str] | None,
    *,
    current_price: Decimal | float | str | None = None,
    prefer_each_price: bool = True,
    apply_coupon_discount: bool = False,
    include_manufacturer: bool = True,
    max_description_chars: int = MAX_DESCRIPTION_CHARS,
) -> DealResult:
    """Classify every offer string scraped from a tile/PDP.

    ``evidence`` is any iterable of raw strings: badge labels, promotion blocks,
    coupon module text, offer tiles.  Order does not matter; descriptions are
    emitted in first-seen order.

    ``apply_coupon_discount`` (default ``False``) is the only switch that lets a
    coupon's stated *discount* become a price, by subtracting it from the
    displayed ``current_price``.  Leave it off to satisfy "ONLY when Walmart
    displays it".

    ``include_manufacturer`` (default ``True``) keeps manufacturer coupon wording
    in ``digital_deals_description``; their amounts are never used as prices.
    """
    result = DealResult()
    volume_terms: list[str] = []
    digital_terms: list[str] = []
    volume_prices: list[str] = []
    digital_prices: list[str] = []

    for raw in evidence or ():
        if raw is None:
            continue
        text = str(raw).replace("\u00a0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) < 3:
            continue

        kind = classify_offer_text(text, include_manufacturer=include_manufacturer)
        if kind == DEAL_CLASS_IGNORED:
            result.ignored.append(text)
            continue

        price, basis = _extract_price(text, prefer_each=prefer_each_price)
        discount = _extract_discount(text)
        if _contains_any(fold(text), MANUFACTURER_OFFER_MARKERS):
            # A manufacturer offer states a discount, never a resulting price.
            price, basis = "", ""

        # Opt-in only: turn a stated coupon discount into a price by subtracting
        # it from the displayed shelf price.  Off by default.
        if (
            kind == DEAL_CLASS_DIGITAL
            and not price
            and apply_coupon_discount
            and discount
            and not discount.endswith("%")
            and current_price is not None
        ):
            base = to_decimal(current_price)
            off = to_decimal(discount)
            if base is not None and off is not None and base - off >= 0:
                price, basis = format_price(base - off), "discount_applied"

        result.deals.append(
            Deal(text=text, kind=kind, price=price, discount=discount, price_basis=basis)
        )

        if kind == DEAL_CLASS_VOLUME:
            volume_terms.append(text)
            if price:
                volume_prices.append(price)
        else:
            digital_terms.append(text)
            if price:
                digital_prices.append(price)

    result.volume_description = dedupe_preserve_order(condense_terms(volume_terms))[:max_description_chars]
    result.digital_description = dedupe_preserve_order(condense_terms(digital_terms))[:max_description_chars]
    # When several prices are shown, the lowest is the effective promoted price.
    result.volume_price = _lowest(volume_prices)
    result.digital_price = _lowest(digital_prices)
    return result


def condense_terms(terms: Sequence[str]) -> list[str]:
    """Drop terms that another retained term already states more completely.

    The listing tile often shows ``"2 for $5"`` while the PDP shows
    ``"2 for $5 ($2.50 each)"``.  Both are Walmart's own wording, but exporting
    both is noise, so the shorter substring is superseded by the longer one.
    """
    kept: list[str] = []
    for term in terms:
        if any(term.lower() in existing.lower() for existing in kept):
            continue
        kept = [existing for existing in kept if existing.lower() not in term.lower()]
        kept.append(term)
    return kept


def _lowest(prices: Sequence[str]) -> str:
    values = [to_decimal(value) for value in prices if to_decimal(value) is not None]
    if not values:
        return ""
    return format_price(min(values))
