"""Shared, dependency free constants.

Everything in here is *data*, not logic: the canonical output field order, the
Walmart private-label vocabulary, and the marker vocabularies used by the deal,
weight and block detection modules.  Keeping them together means an analyst can
audit "what does this scraper consider a coupon?" by reading one file, and
operations can extend the private-label list from
``config/private_label_brands.txt`` without touching code.
"""

from __future__ import annotations

EMPTY = ""

# --------------------------------------------------------------------------- #
# Output schema.  This tuple is the single source of truth for the exported
# record: ``items.WalmartProductItem`` declares its fields in this order and
# ``exporters`` serialises by this tuple, so field order cannot drift.
# --------------------------------------------------------------------------- #
FIELD_ORDER: tuple[str, ...] = (
    "zipcode",
    "store_name",
    "store_location",
    "store_logo",
    "store_brand",
    "category",
    "sub_category",
    "product_title",
    "weight",
    "regular_price",
    "sale_price",
    "volume_deals_description",
    "volume_deals_price",
    "digital_deals_description",
    "digital_deals_price",
    "image_url",
    "url",
    "upc",
    "gupc",          # always ""  - never generated, never copied from upc
    "ppm",           # verbatim from the page, e.g. "$0.25/oz", or ""
    "unit_price",    # always ""  - computed downstream, never here
    "crawl_date",    # always the final field
)

# Fields that must be present but always empty in this scraper's output.
FORCED_EMPTY_FIELDS: tuple[str, ...] = ("gupc", "unit_price")

# Value used for ``store_brand`` when the brand is not a Walmart private label.
STORE_BRAND_NO = "no"

# --------------------------------------------------------------------------- #
# Walmart private labels.  Matching is case/punctuation/diacritic insensitive
# and whole-phrase based (see ``deals``/``text.brand_is_private_label``).
# --------------------------------------------------------------------------- #
DEFAULT_PRIVATE_LABEL_BRANDS: frozenset[str] = frozenset(
    {
        "great value",
        "marketside",
        "freshness guaranteed",
        "equate",
        "spring valley",
        "parent's choice",
        "parents choice",
        "sam's choice",
        "sams choice",
        "mainstays",
        "better homes & gardens",
        "better homes and gardens",
        "george",
        "george home",
        "home essentials",
        "holiday time",
        "wonder nation",
        "athletic works",
        "no boundaries",
        "faded glory",
        "white stag",
        "avia",
        "danskin",
        "danskin now",
        "joyspun",
        "cate & chloe",
        "cate and chloe",
        "onn",
        "onn.",
        "ozark trail",
        "everstart",
        "reliOn",
        "durabrand",
        "rival",
        "my essentials",
        "simply right",
        "member's mark",
        "members mark",
        "time and tru",
        "metaphor",
        "just for you",
        "walmart basics",
        "walmart",
    }
)

# --------------------------------------------------------------------------- #
# Weight hygiene.  A product whose weight string is approximate is excluded from
# the dataset entirely (requirement 2, "Data Rules & Formatting").
# --------------------------------------------------------------------------- #
APPROXIMATE_WEIGHT_TOKENS: tuple[str, ...] = (
    "approx",
    "approx.",
    "approximate",
    "approximately",
    "approximtely",  # common typo on live PDPs
    "about",
    "circa",
    "roughly",
    "around",
    "estimated",
    "est.",
    "~",
    "±",
    "+/-",
    "plus or minus",
    "or more",
    "or less",
    "varies",
    "may vary",
    "assorted",
    "range of",
)

# --------------------------------------------------------------------------- #
# Deal vocabulary.
#
# A *volume* deal needs no action from the shopper ("2 for $5",
# "Buy 1, Get 1 Free").  A *digital* deal must be clipped/activated in the
# customer's account ("Clip Coupon: Save $2").  Cash-back style offers
# (Walmart Cash, rebates, manufacturer money back) are NOT price reductions and
# must never populate a ``*_deals_price`` field.
# --------------------------------------------------------------------------- #
CASHBACK_MARKERS: tuple[str, ...] = (
    "walmart cash",
    "walmartcash",
    "cash back",
    "cashback",
    "cash reward",
    "rebate",
    "mail in rebate",
    "mail-in rebate",
    "money back offer",
    "earn $",
)

MEMBERSHIP_MARKERS: tuple[str, ...] = (
    "savings pass",
    "walmart+",
    "walmart plus",
    "membership price",
    "member price",
)

DIGITAL_DEAL_MARKERS: tuple[str, ...] = (
    "clip",
    "clip coupon",
    "clip to save",
    "coupon clipped",
    "coupon applied",
    "digital coupon",
    "digital deal",
    "add coupon",
    "activate coupon",
    "activate offer",
    "activate deal",
    "with coupon",
    "with digital coupon",
    "coupon available",
    "apply coupon",
)

VOLUME_DEAL_MARKERS: tuple[str, ...] = (
    "for $",
    "buy 1 get 1",
    "buy 1, get 1",
    "buy one get one",
    "buy one, get one",
    "bogo",
    "b1g1",
    "must buy",
    "when you buy",
    "each when you buy",
    "mix & match",
    "mix and match",
    "multi buy",
    "multipack savings",
)

# Straight price reductions: already reflected in ``sale_price``, never reported
# as a coupon.  Kept explicit so a future Walmart label cannot silently leak in.
PRICE_CUT_ONLY_MARKERS: tuple[str, ...] = (
    "rollback",
    "reduced price",
    "clearance",
    "special buy",
    "price cut",
    "was $",
    "flash deals",
    "free shipping",
    "free delivery",
    "free returns",
    "price match",
)

# Offers whose text describes a discount but never a resulting shelf price.
MANUFACTURER_OFFER_MARKERS: tuple[str, ...] = (
    "manufacturer",
    "manufacturer's coupon",
    "manufacturers coupon",
    "mfr coupon",
)

# --------------------------------------------------------------------------- #
# Silent block detection.  A page is treated as a challenge/block when any of
# these markers is present, or when validation in ``blocking.py`` trips.
# --------------------------------------------------------------------------- #
BLOCKING_MARKERS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "robot or human",
    "are you a robot",
    "verify you are human",
    "prove you're not a robot",
    "unusual traffic",
    "access denied",
    "request blocked",
    "perimeterx",
    "px-captcha",
    "_pxhd",
    "datadome",
    "cf-challenge",
    "cf-turnstile",
    "challenge-platform",
    "attention required",
    "something went wrong",
    "enable javascript and cookies to continue",
)

# HTTP statuses that always mean "do not trust this page".
BLOCKING_STATUS_CODES: frozenset[int] = frozenset({401, 403, 412, 429, 503, 999})

# --------------------------------------------------------------------------- #
# Measure units accepted in a scraped price-per-measure string.  Used only to
# *validate* text already on the page - we never build a PPM ourselves.
# --------------------------------------------------------------------------- #
MEASURE_UNITS: tuple[str, ...] = (
    "fl oz", "fluid oz", "fluid ounce", "fluid ounces", "floz",
    "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds",
    "g", "gram", "grams", "kg", "kilogram", "kilograms",
    "ml", "milliliter", "milliliters", "cl", "dl",
    "l", "liter", "liters", "litre", "litres",
    "gal", "gallon", "gallons", "qt", "quart", "quarts", "pt", "pint", "pints",
    "cup", "cups",
    "ct", "count", "counts", "ea", "each", "piece", "pieces", "pc", "pcs",
    "roll", "rolls", "sheet", "sheets",
    "pack", "packs", "bag", "bags", "box", "boxes", "can", "cans",
    "bottle", "bottles", "jar", "jars", "pod", "pods", "load", "loads",
    "wipe", "wipes", "diaper", "diapers",
    "sq ft", "sqft", "square foot", "square feet", "sq in", "sq inch",
    "in", "inch", "inches", "ft", "foot", "feet", "yd", "yard", "yards",
    "m", "meter", "meters", "cm", "millimeter", "millimeters", "mm",
    "servings", "serving", "cal", "kcal",
)

# --------------------------------------------------------------------------- #
# Seller / fulfillment.  Walmart.com is the only first-party seller; anything
# else is marketplace and must not be reported as a local Walmart price.
# --------------------------------------------------------------------------- #
WALMART_SELLER_NAMES: tuple[str, ...] = ("walmart.com", "walmart", "walmart inc")
WALMART_SELLER_IDS: tuple[str, ...] = ("0", "wmt", "walmart", "walmart.com")

# Fulfillment tokens that prove the offer exists at the selected store.
IN_STORE_FULFILLMENT_TOKENS: tuple[str, ...] = (
    "store", "in_store", "instore", "pickup", "curbside", "ship_to_store",
    "free_pickup", "free_store_pickup", "in store",
)

# --------------------------------------------------------------------------- #
# Process exit codes used by ``run_scraper.py`` so Azure can alert on outcome.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_GENERIC_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_BLOCKED = 3
EXIT_VALIDATION_FAILED = 4      # dataset withheld, previous valid data retained
EXIT_NO_DATA = 5

# Default artifact locations (overridable via settings/env).
DEFAULT_CATEGORY_CSV = "config/Walmart_Category_URLs.csv"
DEFAULT_STORES_CSV = "config/stores.csv"
DEFAULT_SELECTORS_JSON = "config/selectors.json"
DEFAULT_PRIVATE_LABEL_FILE = "config/private_label_brands.txt"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_STATE_DIR = "state"
DEFAULT_QUARANTINE_DIR = "quarantine"
RUN_MANIFEST_NAME = "run_manifest.json"
LATEST_LINK_NAME = "latest.jsonl"
