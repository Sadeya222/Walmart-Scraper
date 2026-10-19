"""Weight handling and the approximate-weight exclusion rule.

Requirement: *"Exclude any products whose weight string contains 'approx',
'approximately', or equivalent approximate-weight wording."*

Two pieces:

``is_approximate_weight``
    Detects approximate wording (``approx``, ``~``, ``+/-``, ``varies``, ...)
    plus a bare, unit-less number, which is equally untrustworthy.

``extract_weight``
    Picks the first *clean* weight from the candidate strings a PDP offers
    (specifications table, dimensions block, title fallback) and returns it as
    displayed.  A candidate that is approximate is rejected, so the caller drops
    the whole product rather than shipping a fuzzy weight.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from .constants import APPROXIMATE_WEIGHT_TOKENS, MEASURE_UNITS
from .text import clean_text, fold, normalize_measure

__all__ = ["is_approximate_weight", "extract_weight", "WEIGHT_LABEL_RE"]

_APPROX_SYMBOLS = tuple(
    token.lower() for token in APPROXIMATE_WEIGHT_TOKENS if not token.strip("~±+/- ").isalnum()
)
_APPROX_WORDS = tuple(
    fold(token) for token in APPROXIMATE_WEIGHT_TOKENS if token.strip("~±+/- ").isalnum()
)
# "Weight: 1.2 lb", "Net Weight 16 oz", "Item Weight: 500 g", "Shipping Weight"
WEIGHT_LABEL_RE = re.compile(
    r"(?P<label>(?:net\s+|item\s+|product\s+|package\s+|shipping\s+|unit\s+)?weight)"
    r"\s*[:\-]?\s*(?P<value>[^\n|;,]{1,60})",
    re.IGNORECASE,
)
_DIMENSIONS_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>oz|lb|lbs|g|kg|ml|l|cl|dl|fl\s?oz)\b",
    re.IGNORECASE,
)
# A number with no unit at all ("1.2", "16") is not a usable weight.
_BARE_NUMBER_RE = re.compile(r"^[\d.,\s]+$")
_UNIT_TOKENS = "|".join(sorted((re.escape(unit) for unit in MEASURE_UNITS), key=len, reverse=True))
# Two traps avoided here:
#  * the alternation must be grouped - without (?:...) the lookarounds bind only
#    to the first alternative and "g" matched inside the word "great";
#  * a unit *word* alone is not a weight ("Fresh spinach bag", "each item"), so
#    the pattern requires a number in front of the unit.
_GENERIC_LABEL_RE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z .&/'-]{1,40}):\s*(?P<value>.{1,60})$")

_NUMERIC_MEASURE_RE = re.compile(
    rf"(?<![a-z0-9])\d+(?:[.,]\d+)?\s*(?:{_UNIT_TOKENS})(?![a-z])", re.IGNORECASE
)


def is_approximate_weight(value: str | None) -> bool:
    """True when the weight wording is approximate or unusable."""
    text = clean_text(value)
    if not text:
        # An absent weight is not "approximate"; the caller decides what to do.
        return False
    if _BARE_NUMBER_RE.match(text):
        return True
    # Symbols (~, +/-) do not survive folding, so they are matched on the raw text.
    lowered = text.lower()
    if any(symbol in lowered for symbol in _APPROX_SYMBOLS):
        return True
    folded = fold(text)
    if not folded:
        return True
    for token in _APPROX_WORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded):
            return True
    return False


def _contains_unit(text: str) -> bool:
    """True when the text holds a *numeric* measure such as ``8.6 lb``."""
    return bool(_NUMERIC_MEASURE_RE.search(text))


def _candidate_weight(text: str) -> str:
    """Return one candidate weight as Walmart displays it, or ``""`` if unusable.

    The value is preserved verbatim (``"1 gal (3.78 L)"`` stays whole) because
    reformatting a weight is exactly the kind of reconstruction that produces
    silently wrong data.  A candidate is only accepted when it carries a
    recognised unit, so prose can never be exported as a weight.
    """
    cleaned = clean_text(text)
    if not cleaned or is_approximate_weight(cleaned):
        return ""
    label_match = WEIGHT_LABEL_RE.search(cleaned)
    value = label_match.group("value") if label_match else cleaned
    value = clean_text(value)
    # Strip any other "Label: value" prefix (e.g. "Net content statement: ..."),
    # but only when what remains is an actual measure.
    if not label_match:
        generic = _GENERIC_LABEL_RE.match(cleaned)
        if generic and _contains_unit(generic.group("value")):
            value = clean_text(generic.group("value"))
    if not value or is_approximate_weight(value):
        return ""
    if _contains_unit(value):
        return value
    normalised = normalize_measure(value)
    if normalised:
        return normalised
    dimensions = _DIMENSIONS_RE.search(cleaned)
    if dimensions and not is_approximate_weight(dimensions.group(0)):
        return f"{dimensions.group('value')} {dimensions.group('unit').lower()}"
    return ""


# Real Walmart spec sheets carry several overlapping measures on one product
# (verified against captured product pages, e.g. Great Value Walnuts 16 oz which
# lists both "Size: 16 oz" and "Weight: 1.12 lb").  Taking the first parseable
# candidate can therefore report the *size* as the weight, so candidates are
# ranked by how explicitly they state a weight.
_WEIGHT_LABEL_PRIORITY: tuple[tuple[str, int], ...] = (
    ("item weight", 0),
    ("product weight", 0),
    ("unit weight", 0),
    ("shipping weight", 0),
    ("weight", 1),
    ("net weight", 2),
    ("net content", 3),
    ("net wt", 3),
    ("size", 4),
)

# A mass measure is a better ``weight`` than a volume one: "Item Weight: 8.6 lb"
# beats "Net Weight: 1 gal (3.78 L)" on a real milk PDP.
_MASS_UNITS = frozenset(
    {"lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces",
     "g", "gram", "grams", "kg", "kilogram", "kilograms"}
)


def _is_mass(text: str) -> bool:
    match = _NUMERIC_MEASURE_RE.search(clean_text(text))
    return bool(match) and match.group(0).strip().split()[-1].lower().rstrip(".") in _MASS_UNITS


def _candidate_rank(text: str) -> int:
    """Lower is better.  Unlabelled measures rank last."""
    lowered = clean_text(text).lower()
    best = 9
    for needle, rank in _WEIGHT_LABEL_PRIORITY:
        if needle in lowered:
            best = min(best, rank)
    return best


def extract_weight(candidates: Iterable[str | None]) -> str:
    """Best clean weight among ``candidates``, or ``""`` when none is usable.

    "Best" means the most explicitly weight-labelled candidate, then the first
    one in source order.  Returns the value as Walmart displays it.  Approximate
    candidates are skipped; if *every* candidate is approximate the caller drops
    the record - see ``WalmartPolicyPipeline``.
    """
    scored: list[tuple[int, int, int, str]] = []
    for index, candidate in enumerate(candidates or ()):
        weight = _candidate_weight(candidate if candidate is not None else "")
        if weight:
            scored.append((_candidate_rank(candidate or ""), 0 if _is_mass(weight) else 1, index, weight))
    if not scored:
        return ""
    scored.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return scored[0][3]


def all_candidates_approximate(candidates: Sequence[str | None]) -> bool:
    """True when the product *has* weight text but all of it is approximate.

    This is the exclusion trigger: a product with no weight at all is simply
    exported with ``weight: ""``, while a product whose weight is explicitly
    approximate is removed from the dataset.
    """
    values = [clean_text(value) for value in candidates or ()]
    values = [value for value in values if value]
    if not values:
        return False
    return all(is_approximate_weight(value) for value in values)
