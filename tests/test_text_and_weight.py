"""Price/measure normalisation and the approximate-weight exclusion."""

from __future__ import annotations

import pytest

from walmart_scraper.text import (
    absolute_url,
    brand_is_private_label,
    clean_text,
    fold,
    format_price,
    normalize_measure,
    normalize_price,
    store_brand_for,
    strip_tracking,
    to_decimal,
    validate_ppm,
)
from walmart_scraper.weight import (
    all_candidates_approximate,
    extract_weight,
    is_approximate_weight,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.99", "4.99"),
        ("$4.99", "4.99"),
        ("$1,234.56", "1234.56"),
        (12, "12.00"),
        (3.5, "3.50"),
        ("4 99", "4.99"),
        ("$4 99", "4.99"),
    ],
)
def test_format_price(raw, expected):
    assert format_price(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "n/a", "-", "$2.50 each", "free", "see store"])
def test_format_price_returns_empty_for_unusable(raw):
    assert format_price(raw) == ""


def test_price_format_styles():
    assert normalize_price("4.99", style="numeric") == "4.99"
    assert normalize_price("1234.5", style="display") == "$1,234.50"
    assert normalize_price(None, style="display") == ""


def test_to_decimal_rejects_prose():
    assert to_decimal("$2.50 each") is None
    assert to_decimal(True) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$0.25/oz", "$0.25/oz"),
        ("$3.99 / lb", "$3.99 / lb"),
        ("25\u00a2/oz", "25\u00a2/oz"),
        ("$0.10 per fl oz", "$0.10 per fl oz"),
        ("$1.25/count", "$1.25/count"),
    ],
)
def test_ppm_is_passed_through_verbatim(raw, expected):
    assert validate_ppm(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "$3.99", "oz", "3.99", "free", "approximately $0.25/oz"])
def test_ppm_is_never_fabricated(raw):
    assert validate_ppm(raw) == ""


def test_clean_text_collapses_whitespace_and_none():
    assert clean_text("  a \n b  ") == "a b"
    assert clean_text(None) == ""
    assert clean_text(b"caf\xc3\xa9") == "caf\u00e9"


def test_fold_ignores_case_punctuation_and_accents():
    assert fold("Better Homes & Gardens") == "better homes gardens"
    assert fold("Equ\u00e1te") == "equate"
    assert fold("onn.") == "onn"


@pytest.mark.parametrize(
    "brand", ["Great Value", "great value", "EQUATE", "Marketside", "Spring Valley",
              "Parent's Choice", "onn.", "Better Homes & Gardens", "Ozark Trail"]
)
def test_private_labels_detected(brand):
    assert brand_is_private_label(brand) is True
    assert store_brand_for(brand) == "yes"


@pytest.mark.parametrize("brand", ["Tide", "Bonnie Plants", "Colgate", "", None, "Generic", "Onna Belle"])
def test_non_private_labels(brand):
    assert brand_is_private_label(brand) is False
    assert store_brand_for(brand) == "no"


def test_extra_private_labels_can_be_supplied():
    assert brand_is_private_label("Acme Store Brand") is False
    assert brand_is_private_label("Acme Store Brand", ["acme store brand"]) is True


def test_absolute_url_resolution():
    assert absolute_url("/ip/Thing/123") == "https://www.walmart.com/ip/Thing/123"
    assert absolute_url("//i5.walmartimages.com/x.jpeg") == "https://i5.walmartimages.com/x.jpeg"
    assert absolute_url("https://i5.walmartimages.com/x.jpeg") == "https://i5.walmartimages.com/x.jpeg"
    assert absolute_url("") == ""


def test_strip_tracking_keeps_identity_but_drops_click_tracking():
    url = "https://www.walmart.com/ip/Milk/123?athena_location_id=abc&wl13=1"
    assert strip_tracking(url) == "https://www.walmart.com/ip/Milk/123"
    assert strip_tracking("https://www.walmart.com/ip/Milk/123") == "https://www.walmart.com/ip/Milk/123"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("16 FL OZ", "16 fl oz"), ("1.2 lb", "1.2 lb"), ("500 g", "500 g"), ("Weight: 8.6 lb", "8.6 lb")],
)
def test_normalize_measure(raw, expected):
    assert normalize_measure(raw) == expected


def test_normalize_measure_rejects_unitless_text():
    assert normalize_measure("Great Value") == ""
    assert normalize_measure("") == ""


@pytest.mark.parametrize(
    "weight",
    ["approx 1.2 lb", "approximately 1.2 lb", "Approximately 500 g", "~1 lb",
     "1.2 lb (approx)", "about 16 oz", "1.2", "weight varies", "assorted sizes"],
)
def test_approximate_weights_are_detected(weight):
    assert is_approximate_weight(weight) is True


@pytest.mark.parametrize("weight", ["1.2 lb", "16 fl oz", "", None, "500 g"])
def test_exact_weights_are_not_flagged(weight):
    assert is_approximate_weight(weight) is False


def test_extract_weight_ranks_candidates_instead_of_taking_the_first():
    # A real spec sheet carries several overlapping measures.  An explicit
    # weight label beats an unlabelled one, and a mass beats a volume.
    assert extract_weight(["2 lb", "Item Weight: 1.5 lb"]) == "1.5 lb"
    assert extract_weight(["Net Weight: 1 gal (3.78 L)", "Item Weight: 8.6 lb"]) == "8.6 lb"
    # Verified against captured spec sheets that disagree with each other:
    # Great Value Walnuts 16 oz lists both "Size: 16 oz" and "Weight: 1.12 lb".
    assert extract_weight(["Size: 16 oz (454g)", "Weight: 1.12 lb"]) == "1.12 lb"
    assert extract_weight(["Product net content parent: 16 Ounces", "Weight: 1 lb"]) == "1 lb"
    # Falls back to net content / size when nothing is labelled "weight".
    assert extract_weight(["Net content statement: NET WT 4 oz (113g)"]) == "NET WT 4 oz (113g)"
    assert extract_weight(["Size: 12 Ounce"]) == "12 Ounce"
    # Unlabelled and approximate handling.
    assert extract_weight([None, "", "8.6 lb"]) == "8.6 lb"
    assert extract_weight(["approx 1.2 lb"]) == ""
    assert extract_weight([]) == ""


@pytest.mark.parametrize(
    "prose",
    ["Great Value Milk", "Fresh spinach bag", "great value", "each item", "count on us"],
)
def test_prose_is_never_returned_as_a_weight(prose):
    """Regression: an ungrouped unit alternation once matched 'g' in 'great'."""
    assert extract_weight([prose]) == ""


def test_all_candidates_approximate_is_the_exclusion_trigger():
    assert all_candidates_approximate(["Weight: approximately 1.1 lb"]) is True
    assert all_candidates_approximate(["approx 1 lb", "8.6 lb"]) is False
    # No weight at all is *not* the same as an approximate weight.
    assert all_candidates_approximate([None, ""]) is False
