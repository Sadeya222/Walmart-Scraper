"""End-to-end crawl through the real entry point.

``run_scraper.py`` is executed as a subprocess against a local HTTP server that
serves captured Walmart fixtures, so everything below the browser runs exactly as
it does in production: settings load, store verification, listing parse,
pagination, PDP fetch and merge, deal classification, policy drops, dedupe,
atomic publication, the validation gate and the exit code.

Playwright is switched off (``WMT_USE_PLAYWRIGHT=false``) because the browser
binary is not available in CI; that is the only layer not exercised here.  Each
scenario runs in its own process because a Twisted reactor cannot be restarted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_FIELDS = [
    "zipcode", "store_name", "store_location", "store_logo", "store_brand",
    "category", "sub_category", "product_title", "weight", "regular_price",
    "sale_price", "volume_deals_description", "volume_deals_price",
    "digital_deals_description", "digital_deals_price", "image_url", "url",
    "upc", "gupc", "ppm", "unit_price", "crawl_date",
]


def run_scraper(env_overrides: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, "run_scraper.py", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture()
def crawl_env(tmp_path, fake_walmart):
    """Config files + environment pointing the scraper at the fixture server."""
    base = fake_walmart.base_url
    categories = tmp_path / "categories.csv"
    categories.write_text(
        "category_url,category,sub_category,max_pages,canary_us_item_id,enabled,notes\n"
        f"{base}/search?q=whole+milk,Grocery,Dairy & Milk,3,,true,integration\n",
        encoding="utf-8",
    )
    stores = tmp_path / "stores.csv"
    stores.write_text(
        "store_id,zipcode,store_name,store_location,store_logo,latitude,longitude,enabled\n"
        '2457,78745,Walmart Supercenter,"4701 W Stassney Ln, Austin, TX 78745",'
        "https://example.test/walmart-logo.png,30.2175,-97.8235,true\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "output"
    env = {
        "WMT_CATEGORY_CSV": str(categories),
        "WMT_STORES_CSV": str(stores),
        "WMT_OUTPUT_DIR": str(output_dir),
        "WMT_STATE_DIR": str(tmp_path / "state"),
        "WMT_QUARANTINE_DIR": str(tmp_path / "quarantine"),
        "WMT_USE_PLAYWRIGHT": "false",
        "WMT_PACING_ENABLED": "false",
        "WMT_SCROLL_ENABLED": "false",
        "WMT_CONCURRENCY": "1",
        "WMT_CRAWL_DELAY_MIN": "0",
        "WMT_CRAWL_DELAY_MAX": "0",
        "WMT_MIN_BODY_BYTES": "500",
        "WMT_MIN_ITEMS_PER_PAGE": "1",
        "WMT_MIN_RECORDS_PER_RUN": "2",
        "WMT_RETRY_TIMES": "1",
        "WMT_LOG_LEVEL": "WARNING",
        "WMT_ALLOWED_DOMAINS": "127.0.0.1",
        "WMT_VISIT_PDP": "true",
    }
    return {"env": env, "output": output_dir, "tmp": tmp_path, "server": fake_walmart}


def test_dry_run_prints_the_plan_without_crawling(crawl_env):
    result = run_scraper(crawl_env["env"], "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "crawl plan" in result.stdout
    assert "Grocery > Dairy & Milk" in result.stdout
    assert "store 2457" in result.stdout


def test_full_crawl_publishes_a_valid_dataset(crawl_env):
    result = run_scraper(crawl_env["env"])
    assert result.returncode == 0, result.stdout + result.stderr

    dataset = crawl_env["output"] / "walmart_catalog.jsonl"
    assert dataset.exists(), result.stdout
    records = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]

    # Milk (Walmart.com, exact weight) and spinach survive.  Tide is dropped as a
    # marketplace shipping-only offer; Equate is dropped for an approximate
    # weight; the sponsored repeat of the milk is deduplicated.
    assert len(records) == 2
    titles = [record["product_title"] for record in records]
    assert any("Whole Vitamin D Milk" in title for title in titles)
    assert any("Fresh Spinach" in title for title in titles)
    assert not any("Tide" in title for title in titles)
    assert not any("Ibuprofen" in title for title in titles)

    # Every record has exactly the required columns, in order.
    for record in records:
        assert list(record.keys()) == EXPECTED_FIELDS
        assert None not in record.values()

    milk = next(r for r in records if "Milk" in r["product_title"])
    assert milk["zipcode"] == "78745"
    assert milk["store_name"] == "Walmart Supercenter"
    assert milk["store_location"] == "4701 W Stassney Ln, Austin, TX 78745"
    assert milk["store_logo"] == "https://example.test/walmart-logo.png"
    assert milk["store_brand"] == "yes"                 # Great Value
    assert milk["category"] == "Grocery"
    assert milk["sub_category"] == "Dairy & Milk"
    assert milk["weight"] == "8.6 lb"
    assert milk["regular_price"] == "3.98"
    assert milk["sale_price"] == "3.48"
    assert milk["volume_deals_price"] == "2.50"
    assert "2 for $5" in milk["volume_deals_description"]
    assert milk["digital_deals_price"] == ""
    assert milk["upc"] == "00078742034165"
    assert milk["gupc"] == ""
    assert milk["ppm"] == "$0.03/fl oz"
    assert milk["unit_price"] == ""
    assert milk["crawl_date"]

    # Cashback wording must not become a deal.
    spinach = next(r for r in records if "Spinach" in r["product_title"])
    assert spinach["volume_deals_description"] == ""
    assert spinach["digital_deals_description"] == ""


def test_pagination_is_followed_and_stops_at_an_empty_page(crawl_env):
    result = run_scraper(crawl_env["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    paths = crawl_env["server"].requests
    assert any("page=2" in path for path in paths), paths
    # Page 2 is empty, so page 3 must never be requested.
    assert not any("page=3" in path for path in paths), paths


def test_each_product_page_is_fetched_once(crawl_env):
    """Four unique tiles -> four PDP fetches, none repeated (the sponsored repeat
    was already deduplicated while parsing the listing)."""
    result = run_scraper(crawl_env["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    pdp_hits = [p for p in crawl_env["server"].requests if p.startswith("/ip/")]
    assert len(pdp_hits) == 4, pdp_hits
    assert len(set(pdp_hits)) == 4, pdp_hits


def test_run_manifest_is_written(crawl_env):
    result = run_scraper(crawl_env["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((crawl_env["output"] / "run_manifest.json").read_text())
    assert manifest["exit_code"] == 0
    assert manifest["dataset"]["published"] is True
    assert manifest["dataset"]["record_count"] == 2
    assert manifest["dataset"]["records_added_this_run"] == 2
    assert len(manifest["dataset"]["sha256"]) == 64
    assert manifest["crawl"]["stores_verified"] == 1
    assert manifest["drop_reasons"].get("marketplace_seller") == 1
    assert manifest["drop_reasons"].get("approximate_weight") == 1
    assert "AccountKey" not in json.dumps(manifest)


def test_checkpoint_is_saved_and_can_be_reset(crawl_env):
    assert run_scraper(crawl_env["env"]).returncode == 0
    state_file = crawl_env["tmp"] / "state" / "run_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["records_staged"] == 2
    assert len(state["completed_listings"]) >= 2      # page 1 and the empty page 2


def test_blocked_run_is_withheld_and_keeps_the_previous_dataset(crawl_env):
    """The core fail-safe: a blocked crawl must not destroy good data."""
    assert run_scraper(crawl_env["env"]).returncode == 0
    dataset = crawl_env["output"] / "walmart_catalog.jsonl"
    good_data = dataset.read_text()

    crawl_env["server"].mode = "blocked"
    result = run_scraper(crawl_env["env"])

    assert result.returncode == 4, result.stdout + result.stderr
    assert dataset.read_text() == good_data, "previous dataset must survive a blocked run"

    manifest = json.loads((crawl_env["output"] / "run_manifest.json").read_text())
    assert manifest["dataset"]["published"] is False
    assert "zero_records" in manifest["dataset"]["withheld_reason"]
    assert manifest["crawl"]["blocked_pages"] >= 1

    quarantined = list((crawl_env["tmp"] / "quarantine").iterdir())
    assert any(path.name.endswith(".withheld.jsonl") for path in quarantined)


def test_unverified_store_is_skipped_entirely(crawl_env):
    """No rows may be emitted for a store whose context cannot be confirmed."""
    stores = Path(crawl_env["env"]["WMT_STORES_CSV"])
    stores.write_text(
        "store_id,zipcode,store_name,store_location\n"
        '9999,10001,Different Store,"1 Other Rd, New York, NY"\n',
        encoding="utf-8",
    )
    result = run_scraper(crawl_env["env"])

    assert result.returncode == 4, result.stdout + result.stderr
    dataset = crawl_env["output"] / "walmart_catalog.jsonl"
    assert not dataset.exists() or not dataset.read_text().strip()
    manifest = json.loads((crawl_env["output"] / "run_manifest.json").read_text())
    assert manifest["crawl"]["stores_verified"] == 0


def test_resume_skips_completed_listings(crawl_env):
    assert run_scraper(crawl_env["env"]).returncode == 0
    before = len(crawl_env["server"].requests)

    result = run_scraper(crawl_env["env"], "--resume")
    assert result.returncode == 0, result.stdout + result.stderr
    after = crawl_env["server"].requests[before:]
    # A resumed run still loads the first page once, because the store context has
    # to be verified before anything can be trusted - but it must not repeat any
    # already-completed pagination and must not re-fetch any product page.
    assert [p for p in after if p.startswith("/search")] == ["/search?q=whole+milk"], after
    assert not [p for p in after if "page=" in p], after
    assert not [p for p in after if p.startswith("/ip/")], after

    # The published dataset must still be complete: a resume run seeds the staged
    # file from the existing dataset rather than publishing a partial one.
    dataset = crawl_env["output"] / "walmart_catalog.jsonl"
    records = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    manifest = json.loads((crawl_env["output"] / "run_manifest.json").read_text())
    assert manifest["dataset"]["records_added_this_run"] == 0


def test_bad_config_exits_with_code_2(crawl_env):
    env = {**crawl_env["env"], "WMT_CATEGORY_CSV": str(crawl_env["tmp"] / "missing.csv")}
    result = run_scraper(env)
    assert result.returncode == 2
    assert "configuration error" in (result.stdout + result.stderr).lower()


def test_help_is_available():
    result = run_scraper({}, "--help")
    assert result.returncode == 0
    assert "--resume" in result.stdout
    assert "--categories" in result.stdout
