"""Configuration loading: ``.env``, category CSV, store CSV, selector map.

Nothing secret lives in source.  Everything here is read from the environment
(populated from ``.env`` by python-dotenv, or by Azure app settings) with a
documented default, and every file-backed config is validated up front so a bad
CSV fails the run in seconds instead of producing an empty dataset at the end.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from .constants import (
    DEFAULT_CATEGORY_CSV,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PRIVATE_LABEL_FILE,
    DEFAULT_QUARANTINE_DIR,
    DEFAULT_SELECTORS_JSON,
    DEFAULT_STATE_DIR,
    DEFAULT_STORES_CSV,
)

try:  # pragma: no cover - import guard for environments without dotenv
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

__all__ = [
    "ConfigError",
    "redact_url",
    "CategoryTarget",
    "StoreConfig",
    "ScraperConfig",
    "load_env",
    "load_categories",
    "load_stores",
    "load_private_label_brands",
    "load_selectors",
]

ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


class ConfigError(RuntimeError):
    """Raised for unusable configuration - maps to exit code 2."""


def load_env(path: str | Path | None = ".env") -> None:
    """Load ``.env`` without overriding variables already set by the platform."""
    if load_dotenv is None:  # pragma: no cover
        return
    if path and Path(path).exists():
        load_dotenv(dotenv_path=Path(path), override=False)
    else:
        load_dotenv(override=False)


def redact_url(url: str) -> str:
    """Strip credentials from a URL so they cannot leak into the run manifest.

    ``http://user:pass@proxy:8080`` -> ``http://***:***@proxy:8080``.  The manifest
    is written to disk (and may be uploaded), so proxy credentials must never be
    serialised into it.
    """
    if not url:
        return url
    try:
        parts = urlparse(url)
    except ValueError:
        return "***"
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunparse(parts._replace(netloc=f"***:***@{host}"))


def _get(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value.strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


# --------------------------------------------------------------------------- #
# Category targets
# --------------------------------------------------------------------------- #
@dataclass
class CategoryTarget:
    """One row of ``Walmart_Category_URLs.csv``."""

    url: str
    category: str = ""
    sub_category: str = ""
    max_pages: int = 10
    canary_ids: tuple[str, ...] = ()
    enabled: bool = True
    notes: str = ""

    @property
    def label(self) -> str:
        return " > ".join(part for part in (self.category, self.sub_category) if part) or self.url


CATEGORY_REQUIRED_COLUMNS = ("category_url",)
CATEGORY_OPTIONAL_COLUMNS = (
    "category", "sub_category", "subcategory", "max_pages", "pages",
    "canary_us_item_id", "canary_ids", "enabled", "notes",
)


def _read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    """Read a delimited config file.

    Leading ``#`` comment lines and blank lines are stripped *before* header
    detection (the shipped sample files document their columns that way), and
    data rows whose first cell starts with ``#`` are skipped.  The remaining text
    is handed to :mod:`csv` untouched so quoted, embedded-newline fields survive.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"Config file not found: {file_path}")

    text = file_path.read_text(encoding="utf-8-sig")
    body = re.sub(r"\A(?:\s*(?:#[^\n]*)?\n)+", "", text)
    if not body.strip():
        raise ConfigError(f"{file_path} is empty")

    sample = body[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(body, newline=""), dialect=dialect)
    if not reader.fieldnames:
        raise ConfigError(f"{file_path} has no header row")
    reader.fieldnames = [name.strip().lstrip("\ufeff") for name in reader.fieldnames]

    rows: list[dict[str, str]] = []
    for row in reader:
        cleaned = {
            (key or "").strip(): (value or "").strip() for key, value in row.items() if key
        }
        if not any(cleaned.values()):
            continue
        first = str(cleaned.get(reader.fieldnames[0], ""))
        if first.startswith("#"):
            continue  # comment line
        rows.append(cleaned)
    return rows


def load_categories(path: str | Path, *, default_max_pages: int = 10) -> list[CategoryTarget]:
    """Parse the category CSV into :class:`CategoryTarget` objects."""
    rows = _read_csv_rows(path)
    if rows:
        header = set(rows[0].keys())
        missing = [col for col in CATEGORY_REQUIRED_COLUMNS if col not in header]
        if missing:
            raise ConfigError(
                f"{path} is missing required column(s): {', '.join(missing)}. "
                f"Expected a header containing: {', '.join(CATEGORY_REQUIRED_COLUMNS)}"
            )
    targets: list[CategoryTarget] = []
    seen: set[str] = set()
    for number, row in enumerate(rows, start=2):
        url = row.get("category_url", "").strip()
        if not url:
            continue
        if url in seen:
            continue  # a repeated category URL would create duplicate records
        seen.add(url)
        if not re.match(r"^https?://", url):
            url = f"https://www.walmart.com{url if url.startswith('/') else '/' + url}"
        canary_raw = row.get("canary_us_item_id") or row.get("canary_ids") or ""
        canaries = tuple(part.strip() for part in re.split(r"[|;,]", canary_raw) if part.strip())
        raw_pages = row.get("max_pages") or row.get("pages") or ""
        try:
            max_pages = int(float(raw_pages)) if raw_pages else default_max_pages
        except ValueError:
            max_pages = default_max_pages
        enabled_raw = (row.get("enabled") or "true").strip().lower()
        targets.append(
            CategoryTarget(
                url=url,
                category=row.get("category", "").strip(),
                sub_category=(row.get("sub_category") or row.get("subcategory") or "").strip(),
                max_pages=max(1, min(max_pages, 200)),
                canary_ids=canaries,
                enabled=enabled_raw not in {"false", "0", "no", "n", "off"},
                notes=row.get("notes", "").strip(),
            )
        )
    if not targets:
        raise ConfigError(f"{path} contained no usable category rows")
    return targets


# --------------------------------------------------------------------------- #
# Store locations
# --------------------------------------------------------------------------- #
@dataclass
class StoreConfig:
    """One row of ``config/stores.csv`` - a location to crawl."""

    store_id: str
    zipcode: str
    store_name: str = ""
    store_location: str = ""
    store_logo: str = ""
    latitude: str = ""
    longitude: str = ""
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.store_name or f"Walmart #{self.store_id}"


STORE_REQUIRED_COLUMNS = ("store_id", "zipcode")


def load_stores(path: str | Path) -> list[StoreConfig]:
    """Parse the store CSV.  Duplicate store ids and malformed ZIPs are errors."""
    rows = _read_csv_rows(path)
    header = set(rows[0].keys()) if rows else set()
    missing = [col for col in STORE_REQUIRED_COLUMNS if col not in header]
    if missing:
        raise ConfigError(
            f"{path} is missing required column(s): {', '.join(missing)}"
        )
    stores: list[StoreConfig] = []
    seen: set[str] = set()
    for number, row in enumerate(rows, start=2):
        store_id = row.get("store_id", "").strip()
        zipcode = row.get("zipcode", "").strip()
        if not store_id or not zipcode:
            continue
        if store_id in seen:
            raise ConfigError(f"{path} line {number}: duplicate store_id {store_id!r}")
        if not ZIP_RE.match(zipcode):
            raise ConfigError(f"{path} line {number}: zipcode {zipcode!r} is not a 5-digit US ZIP")
        seen.add(store_id)
        enabled_raw = (row.get("enabled") or "true").strip().lower()
        stores.append(
            StoreConfig(
                store_id=store_id,
                zipcode=zipcode,
                store_name=row.get("store_name", "").strip(),
                store_location=row.get("store_location", "").strip(),
                store_logo=row.get("store_logo", "").strip(),
                latitude=row.get("latitude", "").strip(),
                longitude=row.get("longitude", "").strip(),
                enabled=enabled_raw not in {"false", "0", "no", "n", "off"},
            )
        )
    if not stores:
        raise ConfigError(f"{path} contained no enabled store rows")
    return stores


def load_private_label_brands(path: str | Path | None) -> tuple[str, ...]:
    """Extra private-label names from a text file (``#`` comments allowed)."""
    if not path:
        return ()
    file_path = Path(path)
    if not file_path.exists():
        return ()
    brands: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        brands.append(line)
    return tuple(brands)


def load_selectors(path: str | Path | None) -> dict[str, list[str]]:
    """DOM selector map, so selector drift is a config change, not a code change."""
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file_path} must contain a JSON object")
    return {key: list(value) for key, value in raw.items() if isinstance(value, list)}


# --------------------------------------------------------------------------- #
# Aggregate configuration
# --------------------------------------------------------------------------- #
@dataclass
class ScraperConfig:
    """Every tunable the scraper reads from the environment."""

    # inputs
    category_csv: str = DEFAULT_CATEGORY_CSV
    stores_csv: str = DEFAULT_STORES_CSV
    selectors_json: str = DEFAULT_SELECTORS_JSON
    private_labels_file: str = DEFAULT_PRIVATE_LABEL_FILE

    # outputs
    output_dir: str = DEFAULT_OUTPUT_DIR
    state_dir: str = DEFAULT_STATE_DIR
    quarantine_dir: str = DEFAULT_QUARANTINE_DIR
    dataset_name: str = "walmart_catalog.jsonl"
    crawl_date_format: str = "date"      # "date" -> YYYY-MM-DD, "iso" -> full ISO-8601

    # validation gate
    min_records_per_run: int = 25
    min_items_per_page: int = 5
    min_body_bytes: int = 20_000
    max_suspect_ratio: float = 0.34
    max_blocked_pages: int = 10

    # crawl shape
    max_pages_per_category: int = 10
    scroll_enabled: bool = True
    scroll_max_rounds: int = 6
    visit_pdp: bool = True
    max_pdp_per_category: int = 60
    concurrency: int = 2
    download_timeout: int = 60
    crawl_delay_min: float = 2.0
    crawl_delay_max: float = 6.0
    resume: bool = False

    # store / fulfillment context
    store_verification_mode: str = "cookie_then_ui"   # cookie | ui | cookie_then_ui | off
    require_store_verified: bool = True
    fulfillment_mode: str = "any"        # pickup | in_store | any
    exclude_marketplace: bool = True
    shipping_only_mode: str = "exclude"  # exclude | include
    weight_from_title: bool = True

    # pricing / deals policy
    price_format: str = "numeric"                  # numeric | display
    apply_coupon_discount: bool = False            # never derive a coupon price by default
    prefer_each_price: bool = True                 # "2 for $5 ($2.50 each)" -> 2.50
    include_manufacturer_offers: bool = True       # description only, never a price

    # browser
    headless: bool = True
    browser_type: str = "chromium"
    user_agent: str = ""
    proxy_url: str = ""

    # azure
    azure_storage_connection_string: str = ""
    azure_storage_container: str = ""
    azure_blob_prefix: str = "walmart"

    # resolved at runtime
    categories: list[CategoryTarget] = field(default_factory=list)
    stores: list[StoreConfig] = field(default_factory=list)
    extra_private_labels: tuple[str, ...] = ()
    selectors: dict[str, list[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, *, load_files: bool = True) -> "ScraperConfig":
        """Build configuration from the environment (``.env`` or Azure settings)."""
        load_env()
        config = cls(
            category_csv=_get("WMT_CATEGORY_CSV", DEFAULT_CATEGORY_CSV),
            stores_csv=_get("WMT_STORES_CSV", DEFAULT_STORES_CSV),
            selectors_json=_get("WMT_SELECTORS_JSON", DEFAULT_SELECTORS_JSON),
            private_labels_file=_get("WMT_PRIVATE_LABEL_BRANDS_FILE", DEFAULT_PRIVATE_LABEL_FILE),
            output_dir=_get("WMT_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
            state_dir=_get("WMT_STATE_DIR", DEFAULT_STATE_DIR),
            quarantine_dir=_get("WMT_QUARANTINE_DIR", DEFAULT_QUARANTINE_DIR),
            dataset_name=_get("WMT_DATASET_NAME", "walmart_catalog.jsonl"),
            crawl_date_format=_get("WMT_CRAWL_DATE_FORMAT", "date").lower(),
            min_records_per_run=_get_int("WMT_MIN_RECORDS_PER_RUN", 25),
            min_items_per_page=_get_int("WMT_MIN_ITEMS_PER_PAGE", 5),
            min_body_bytes=_get_int("WMT_MIN_BODY_BYTES", 20_000),
            max_suspect_ratio=_get_float("WMT_MAX_SUSPECT_RATIO", 0.34),
            max_blocked_pages=_get_int("WMT_MAX_BLOCKS_PER_RUN", 10),
            max_pages_per_category=_get_int("WMT_MAX_PAGES_PER_CATEGORY", 10),
            scroll_enabled=_get_bool("WMT_SCROLL_ENABLED", True),
            scroll_max_rounds=_get_int("WMT_SCROLL_MAX_ROUNDS", 6),
            visit_pdp=_get_bool("WMT_VISIT_PDP", True),
            max_pdp_per_category=_get_int("WMT_MAX_PDP_PER_CATEGORY", 60),
            concurrency=_get_int("WMT_CONCURRENCY", 2),
            download_timeout=_get_int("WMT_DOWNLOAD_TIMEOUT", 60),
            crawl_delay_min=_get_float("WMT_CRAWL_DELAY_MIN", 2.0),
            crawl_delay_max=_get_float("WMT_CRAWL_DELAY_MAX", 6.0),
            resume=_get_bool("WMT_RESUME", False),
            store_verification_mode=_get("WMT_STORE_VERIFICATION_MODE", "cookie_then_ui").lower(),
            require_store_verified=_get_bool("WMT_REQUIRE_STORE_VERIFIED", True),
            fulfillment_mode=_get("WMT_FULFILLMENT_MODE", "any").lower(),
            exclude_marketplace=_get_bool("WMT_EXCLUDE_MARKETPLACE", True),
            shipping_only_mode=_get("WMT_SHIPPING_ONLY_MODE", "exclude").lower(),
            weight_from_title=_get_bool("WMT_WEIGHT_FROM_TITLE", True),
            price_format=_get("WMT_PRICE_FORMAT", "numeric").lower(),
            apply_coupon_discount=_get_bool("WMT_APPLY_COUPON_DISCOUNT", False),
            prefer_each_price=_get_bool("WMT_PREFER_EACH_PRICE", True),
            include_manufacturer_offers=_get_bool("WMT_INCLUDE_MANUFACTURER_OFFERS", True),
            headless=_get_bool("WMT_HEADLESS", True),
            browser_type=_get("WMT_BROWSER", "chromium").lower(),
            user_agent=_get("WMT_USER_AGENT", ""),
            proxy_url=_get("WMT_PROXY_URL") or _get("HTTPS_PROXY") or _get("HTTP_PROXY"),
            azure_storage_connection_string=_get("AZURE_STORAGE_CONNECTION_STRING"),
            azure_storage_container=_get("AZURE_STORAGE_CONTAINER"),
            azure_blob_prefix=_get("AZURE_BLOB_PREFIX", "walmart"),
        )
        config.validate_policy_values()
        if load_files:
            config.categories = [c for c in load_categories(config.category_csv) if c.enabled]
            if not config.categories:
                raise ConfigError(f"{config.category_csv} has no enabled rows")
            config.stores = [s for s in load_stores(config.stores_csv) if s.enabled]
            if not config.stores:
                raise ConfigError(f"{config.stores_csv} has no enabled rows")
            config.extra_private_labels = load_private_label_brands(config.private_labels_file)
            config.selectors = load_selectors(config.selectors_json)
        return config

    # ------------------------------------------------------------------ #
    def validate_policy_values(self) -> None:
        """Reject nonsense policy values early rather than silently misbehaving."""
        if self.crawl_date_format not in {"date", "iso"}:
            raise ConfigError("WMT_CRAWL_DATE_FORMAT must be 'date' or 'iso'")
        if self.price_format not in {"numeric", "display"}:
            raise ConfigError("WMT_PRICE_FORMAT must be 'numeric' or 'display'")
        if self.shipping_only_mode not in {"exclude", "include"}:
            raise ConfigError("WMT_SHIPPING_ONLY_MODE must be 'exclude' or 'include'")
        if self.fulfillment_mode not in {"pickup", "in_store", "any"}:
            raise ConfigError("WMT_FULFILLMENT_MODE must be 'pickup', 'in_store' or 'any'")
        if self.store_verification_mode not in {"cookie", "ui", "cookie_then_ui", "off"}:
            raise ConfigError(
                "WMT_STORE_VERIFICATION_MODE must be cookie, ui, cookie_then_ui or off"
            )
        if self.browser_type not in {"chromium", "firefox", "webkit"}:
            raise ConfigError("WMT_BROWSER must be chromium, firefox or webkit")
        if self.crawl_delay_min < 0 or self.crawl_delay_max < self.crawl_delay_min:
            raise ConfigError(
                "WMT_CRAWL_DELAY_MIN must be >= 0 and WMT_CRAWL_DELAY_MAX >= WMT_CRAWL_DELAY_MIN"
            )
        if self.concurrency < 1:
            raise ConfigError("WMT_CONCURRENCY must be >= 1")

    # ------------------------------------------------------------------ #
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir) / self.dataset_name

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir) / "run_state.json"

    def as_redacted_dict(self) -> dict[str, Any]:
        """Config dump for the run manifest, with secrets removed."""
        data = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"categories", "stores", "selectors", "azure_storage_connection_string"}
        }
        data["proxy_url"] = redact_url(data.get("proxy_url", ""))
        data["categories"] = [target.url for target in self.categories]
        data["stores"] = [store.store_id for store in self.stores]
        data["proxy_configured"] = bool(self.proxy_url)
        data["azure_upload_configured"] = bool(self.azure_storage_connection_string)
        return data
