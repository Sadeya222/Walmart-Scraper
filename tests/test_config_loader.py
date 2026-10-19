"""CSV/env configuration loading and policy validation."""

from __future__ import annotations

import pytest

from walmart_scraper.config_loader import (
    CategoryTarget,
    ConfigError,
    ScraperConfig,
    load_categories,
    load_private_label_brands,
    load_selectors,
    load_stores,
)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_shipped_sample_files():
    categories = load_categories("config/Walmart_Category_URLs.csv")
    stores = load_stores("config/stores.csv")
    assert categories and stores
    assert all(isinstance(target, CategoryTarget) for target in categories)
    assert all(store.zipcode.isdigit() for store in stores)


def test_category_columns_are_parsed(tmp_path):
    path = write(
        tmp_path, "cats.csv",
        "category_url,category,sub_category,max_pages,canary_us_item_id,enabled,notes\n"
        "https://www.walmart.com/search?q=milk,Grocery,Milk,4,123|456,true,sample\n"
        "/cp/grocery/976759,Grocery,All,,,,\n",
    )
    targets = load_categories(path)
    assert targets[0].url == "https://www.walmart.com/search?q=milk"
    assert targets[0].category == "Grocery"
    assert targets[0].sub_category == "Milk"
    assert targets[0].max_pages == 4
    assert targets[0].canary_ids == ("123", "456")
    assert targets[0].label == "Grocery > Milk"
    # Relative URLs are resolved against walmart.com.
    assert targets[1].url == "https://www.walmart.com/cp/grocery/976759"


def test_duplicate_category_urls_are_collapsed(tmp_path):
    path = write(
        tmp_path, "cats.csv",
        "category_url,category\nhttps://www.walmart.com/search?q=milk,Grocery\n"
        "https://www.walmart.com/search?q=milk,Grocery again\n",
    )
    assert len(load_categories(path)) == 1


def test_missing_category_column_is_a_config_error(tmp_path):
    path = write(tmp_path, "cats.csv", "url,category\nhttps://w/x,Grocery\n")
    with pytest.raises(ConfigError, match="category_url"):
        load_categories(path)


def test_empty_category_file_is_a_config_error(tmp_path):
    path = write(tmp_path, "cats.csv", "category_url,category\n,,\n")
    with pytest.raises(ConfigError, match="no usable category rows"):
        load_categories(path)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_categories(tmp_path / "nope.csv")


def test_comment_lines_and_blank_lines_are_skipped(tmp_path):
    path = write(
        tmp_path, "stores.csv",
        "# leading comment\n\nstore_id,zipcode,store_name,store_location\n"
        "# inline comment\n2457,78745,Walmart Supercenter,\"1 Main St, Austin, TX\"\n",
    )
    stores = load_stores(path)
    assert len(stores) == 1
    assert stores[0].store_name == "Walmart Supercenter"
    assert stores[0].store_location == "1 Main St, Austin, TX"


def test_duplicate_store_ids_are_rejected(tmp_path):
    path = write(
        tmp_path, "stores.csv",
        "store_id,zipcode\n2457,78745\n2457,10001\n",
    )
    with pytest.raises(ConfigError, match="duplicate store_id"):
        load_stores(path)


@pytest.mark.parametrize("zipcode", ["7874", "abcde", "787455", ""])
def test_bad_zipcodes_are_rejected(tmp_path, zipcode):
    path = write(tmp_path, "stores.csv", f"store_id,zipcode\n2457,{zipcode}\n")
    with pytest.raises(ConfigError):
        load_stores(path)


def test_disabled_rows_are_flagged_not_dropped(tmp_path):
    path = write(
        tmp_path, "stores.csv",
        "store_id,zipcode,enabled\n2457,78745,true\n3086,10001,false\n",
    )
    stores = load_stores(path)
    assert [store.enabled for store in stores] == [True, False]


def test_private_label_file_ignores_comments(tmp_path):
    path = write(tmp_path, "brands.txt", "# comment\nAcme Brand\n\nAnother\n")
    assert load_private_label_brands(path) == ("Acme Brand", "Another")
    assert load_private_label_brands(tmp_path / "absent.txt") == ()
    assert load_private_label_brands(None) == ()


def test_selectors_json_is_loaded(tmp_path):
    path = write(tmp_path, "sel.json", '{"tile_title": ["a", "b"]}')
    assert load_selectors(path) == {"tile_title": ["a", "b"]}
    assert load_selectors(tmp_path / "absent.json") == {}


def test_invalid_selectors_json_is_a_config_error(tmp_path):
    path = write(tmp_path, "sel.json", "{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_selectors(path)


@pytest.mark.parametrize(
    ("env_key", "bad_value"),
    [
        ("WMT_CRAWL_DATE_FORMAT", "timestamp"),
        ("WMT_PRICE_FORMAT", "euro"),
        ("WMT_SHIPPING_ONLY_MODE", "maybe"),
        ("WMT_FULFILLMENT_MODE", "teleport"),
        ("WMT_STORE_VERIFICATION_MODE", "guess"),
        ("WMT_BROWSER", "netscape"),
    ],
)
def test_policy_values_are_validated(monkeypatch, env_key, bad_value):
    monkeypatch.setenv(env_key, bad_value)
    with pytest.raises(ConfigError):
        ScraperConfig.from_env(load_files=False)


def test_delay_bounds_are_validated(monkeypatch):
    monkeypatch.setenv("WMT_CRAWL_DELAY_MIN", "10")
    monkeypatch.setenv("WMT_CRAWL_DELAY_MAX", "2")
    with pytest.raises(ConfigError, match="WMT_CRAWL_DELAY"):
        ScraperConfig.from_env(load_files=False)


def test_non_numeric_setting_is_a_config_error(monkeypatch):
    monkeypatch.setenv("WMT_CONCURRENCY", "lots")
    with pytest.raises(ConfigError, match="not an integer"):
        ScraperConfig.from_env(load_files=False)


def test_environment_overrides_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("WMT_MIN_RECORDS_PER_RUN", "999")
    monkeypatch.setenv("WMT_PRICE_FORMAT", "display")
    monkeypatch.setenv("WMT_OUTPUT_DIR", str(tmp_path / "data"))
    config = ScraperConfig.from_env(load_files=False)
    assert config.min_records_per_run == 999
    assert config.price_format == "display"
    assert config.output_path == tmp_path / "data" / "walmart_catalog.jsonl"


def test_redacted_config_never_exposes_secrets(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "AccountKey=supersecret")
    monkeypatch.setenv("WMT_PROXY_URL", "http://user:secretpw@proxy:8080")
    config = ScraperConfig.from_env(load_files=False)

    dumped = config.as_redacted_dict()
    as_text = str(dumped)
    assert "supersecret" not in as_text
    assert "secretpw" not in as_text
    assert "user" not in dumped["proxy_url"]
    # The proxy is still visibly configured, just without credentials.
    assert dumped["proxy_url"] == "http://***:***@proxy:8080"
    assert dumped["proxy_configured"] is True
    assert dumped["azure_upload_configured"] is True
    assert "azure_storage_connection_string" not in dumped


def test_redact_url_leaves_credential_free_urls_alone():
    from walmart_scraper.config_loader import redact_url

    assert redact_url("http://proxy:8080") == "http://proxy:8080"
    assert redact_url("") == ""
    assert redact_url("http://u:p@host:3128") == "http://***:***@host:3128"
