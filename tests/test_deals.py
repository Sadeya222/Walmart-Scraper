"""Deal classification rules - the business logic most likely to be wrong."""

from __future__ import annotations

import pytest

from walmart_scraper.deals import (
    DEAL_CLASS_DIGITAL,
    DEAL_CLASS_IGNORED,
    DEAL_CLASS_VOLUME,
    classify_offer_text,
    condense_terms,
    parse_deals,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Volume: applies automatically, no clipping.
        ("2 for $5", DEAL_CLASS_VOLUME),
        ("3/$6.00", DEAL_CLASS_VOLUME),
        ("Buy 1, Get 1 Free", DEAL_CLASS_VOLUME),
        ("Buy one get one free", DEAL_CLASS_VOLUME),
        ("B1G1", DEAL_CLASS_VOLUME),
        ("Must buy 4 at $1.99 each", DEAL_CLASS_VOLUME),
        ("$1.99 each when you buy 4", DEAL_CLASS_VOLUME),
        ("Buy 2, Get 1 50% off", DEAL_CLASS_VOLUME),
        ("Mix & match: 2 for $5", DEAL_CLASS_VOLUME),
        # Digital: the customer must clip/activate.
        ("Clip Coupon: Save $2", DEAL_CLASS_DIGITAL),
        ("$3.99 each with Digital Coupon", DEAL_CLASS_DIGITAL),
        ("Clip to save $1.00", DEAL_CLASS_DIGITAL),
        ("Coupon applied at checkout", DEAL_CLASS_DIGITAL),
        ("Clip coupon: 2 for $5", DEAL_CLASS_DIGITAL),  # clipping wins over wording
        # Not deals at all.
        ("Get $5 Walmart Cash on $25 purchase", DEAL_CLASS_IGNORED),
        ("Walmart Cash back offer", DEAL_CLASS_IGNORED),
        ("Mail-in rebate $10", DEAL_CLASS_IGNORED),
        ("Rollback", DEAL_CLASS_IGNORED),
        ("Clearance", DEAL_CLASS_IGNORED),
        ("Free shipping on orders over $35", DEAL_CLASS_IGNORED),
        ("$2.99 with Walmart+ Savings Pass", DEAL_CLASS_IGNORED),
    ],
)
def test_classification(text, expected):
    assert classify_offer_text(text) == expected


def test_volume_price_is_the_displayed_each_price():
    result = parse_deals(["2 for $5 ($2.50 each)"])
    assert result.volume_description == "2 for $5 ($2.50 each)"
    assert result.volume_price == "2.50"


def test_volume_price_falls_back_to_the_displayed_offer_total():
    result = parse_deals(["2 for $5"])
    assert result.volume_price == "5.00"


def test_bogo_without_a_shown_price_gets_no_price():
    result = parse_deals(["Buy 1, Get 1 Free"])
    assert result.volume_description == "Buy 1, Get 1 Free"
    assert result.volume_price == ""


def test_must_buy_each_price():
    result = parse_deals(["Must buy 4 at $1.99 each"])
    assert result.volume_price == "1.99"


def test_clip_coupon_discount_is_not_a_price():
    """'$2 off' is a discount, so digital_deals_price must stay empty."""
    result = parse_deals(["Clip Coupon: Save $2"], current_price="9.99")
    assert result.digital_description == "Clip Coupon: Save $2"
    assert result.digital_price == ""


def test_displayed_post_coupon_price_is_used():
    result = parse_deals(["$3.99 each with Digital Coupon"], current_price="9.99")
    assert result.digital_price == "3.99"


def test_coupon_discount_can_be_applied_only_when_opted_in():
    off = parse_deals(["Clip Coupon: Save $2"], current_price="9.99")
    on = parse_deals(["Clip Coupon: Save $2"], current_price="9.99", apply_coupon_discount=True)
    assert off.digital_price == ""
    assert on.digital_price == "7.99"


def test_cashback_never_reaches_a_deal_field():
    result = parse_deals(["Get $5 Walmart Cash on $25 purchase"], current_price="26.00")
    assert result.volume_description == result.digital_description == ""
    assert result.volume_price == result.digital_price == ""
    assert result.ignored == ["Get $5 Walmart Cash on $25 purchase"]


def test_manufacturer_offer_has_terms_but_no_price():
    result = parse_deals(["Manufacturer Coupon: $1.00 off"])
    assert result.digital_description == "Manufacturer Coupon: $1.00 off"
    assert result.digital_price == ""


def test_manufacturer_offers_can_be_excluded_entirely():
    result = parse_deals(["Manufacturer Coupon: $1.00 off"], include_manufacturer=False)
    assert result.digital_description == ""


def test_rollback_is_not_reported_as_a_deal():
    result = parse_deals(["Rollback", "2 for $5"])
    assert result.volume_description == "2 for $5"
    assert "Rollback" in result.ignored


def test_complete_terms_are_preserved_including_limits():
    result = parse_deals(["Clip Coupon: Save $2, limit 4 per customer"])
    assert "limit 4 per customer" in result.digital_description


def test_lowest_displayed_price_wins_when_several_offers_exist():
    result = parse_deals(["2 for $8", "2 for $5"])
    assert result.volume_price == "5.00"


def test_volume_and_digital_are_reported_independently():
    result = parse_deals(["2 for $5", "Clip Coupon: Save $2"])
    assert result.volume_description == "2 for $5"
    assert result.digital_description == "Clip Coupon: Save $2"
    assert result.digital_price == ""


def test_duplicate_terms_are_deduplicated_case_insensitively():
    result = parse_deals(["2 for $5", "2 FOR $5"])
    assert result.volume_description == "2 for $5"


def test_condense_terms_prefers_the_more_complete_wording():
    assert condense_terms(["2 for $5", "2 for $5 ($2.50 each)"]) == ["2 for $5 ($2.50 each)"]
    assert condense_terms(["Buy 1, Get 1 Free", "2 for $5"]) == ["Buy 1, Get 1 Free", "2 for $5"]


def test_descriptions_are_capped():
    result = parse_deals(["2 for $5", "Buy 1, Get 1 Free"], max_description_chars=12)
    assert len(result.volume_description) <= 12


def test_as_dict_keys_match_the_export_columns():
    assert set(parse_deals([]).as_dict()) == {
        "volume_deals_description",
        "volume_deals_price",
        "digital_deals_description",
        "digital_deals_price",
    }


def test_junk_evidence_is_ignored_without_raising():
    result = parse_deals([None, "", "  ", "ok", 42])
    assert result.as_dict() == {
        "volume_deals_description": "",
        "volume_deals_price": "",
        "digital_deals_description": "",
        "digital_deals_price": "",
    }
