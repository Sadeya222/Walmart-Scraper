# Walmart Location-Specific Catalog Scraper

A production-grade [Scrapy](https://scrapy.org/) + [Scrapy-Playwright](https://github.com/scrapy-plugins/scrapy-playwright)
crawler that extracts **store-specific** Walmart catalog data — prices, price-per-measure,
weights, UPCs and promotional offers — for a configurable list of store locations and
category URLs, and writes a strictly ordered JSON Lines dataset designed to survive
bot-blocking without ever corrupting the previous good run.

Built to run unattended on Microsoft Azure (Container Instances or Functions with Docker).

---

## 1. What it does

For every **(store × category)** pair:

1. Seeds a dedicated browser context with the store's location cookies and verifies the
   rendered store indicator matches the configured store. A store that cannot be verified
   is **skipped** rather than crawled — a wrong store means every price is wrong.
2. Crawls the category page, parsing Walmart's embedded `__NEXT_DATA__` state (DOM
   selectors in `config/selectors.json` are the fallback), follows deep pagination
   (`?page=N`) and optionally infinite-scroll to force lazy-loaded tiles.
3. Fetches each product detail page once per store to pick up the UPC/GTIN, weight, brand
   and price-per-measure that tiles do not carry.
4. Classifies every offer into **volume deals** (no action needed) or **digital deals**
   (must be clipped), and emits one record per product.

Everything is resumable, and the published dataset is only ever replaced by a run that
passes validation.

---

## 2. Output schema

Every line of the JSON Lines dataset is an object with **exactly these 22 fields, in this
order** — `crawl_date` always last:

| # | Field | Notes |
|---|-------|-------|
| 1 | `zipcode` | Configured store ZIP |
| 2 | `store_name` | From `config/stores.csv` |
| 3 | `store_location` | Store address |
| 4 | `store_logo` | Optional, stamped from config; `""` if unset |
| 5 | `store_brand` | `"yes"` for Walmart private labels, otherwise **`"no"`** |
| 6 | `category` | From the category CSV |
| 7 | `sub_category` | From the category CSV |
| 8 | `product_title` | Longest of the tile/PDP titles |
| 9 | `weight` | As displayed, e.g. `8.6 lb`. `""` if absent |
| 10 | `regular_price` | `was` price when marked down, else the shelf price |
| 11 | `sale_price` | Current price **only** when a genuine markdown exists |
| 12 | `volume_deals_description` | Walmart's complete original terms |
| 13 | `volume_deals_price` | Displayed promo price, or `""` |
| 14 | `digital_deals_description` | Complete terms including limits |
| 15 | `digital_deals_price` | Displayed promo price, or `""` |
| 16 | `image_url` | Preserved exactly, query string included |
| 17 | `url` | Preserved exactly, query string included |
| 18 | `upc` | Walmart's published GTIN/UPC/SKU, digits only |
| 19 | `gupc` | **Always `""`** — never generated, never copied from `upc` |
| 20 | `ppm` | Scraped verbatim, e.g. `$0.25/oz`; `""` if not displayed |
| 21 | `unit_price` | **Always `""`** — computed downstream |
| 22 | `crawl_date` | `YYYY-MM-DD` (or ISO-8601), always the final field |

```json
{"zipcode":"78745","store_name":"Walmart Supercenter","store_location":"4701 W Stassney Ln, Austin, TX 78745","store_logo":"","store_brand":"yes","category":"Grocery","sub_category":"Dairy & Milk","product_title":"Great Value Whole Vitamin D Milk, 1 gal","weight":"8.6 lb","regular_price":"3.98","sale_price":"3.48","volume_deals_description":"2 for $5 ($2.50 each)","volume_deals_price":"2.50","digital_deals_description":"","digital_deals_price":"","image_url":"https://i5.walmartimages.com/asr/milk-1gal.jpeg?odnHeight=180","url":"https://www.walmart.com/ip/Great-Value-Whole-Vitamin-D-Milk-1-gal/104517984","upc":"00078742034165","gupc":"","ppm":"$0.03/fl oz","unit_price":"","crawl_date":"2026-10-19"}
```

### Data rules enforced by the pipeline

- **Missing values are `""`, never `null`.** `WalmartProductItem.to_ordered_dict()` coerces
  every value; a test asserts no `None` can reach the file.
- **Prices** are bare two-decimal numbers (`4.99`) by default. Set `WMT_PRICE_FORMAT=display`
  for `"$4.99"`.
- **Approximate weights exclude the product entirely.** Any of `approx`, `approximately`,
  `~`, `+/-`, `about`, `varies`, `assorted`, … in the weight string drops the record
  (`drop_reason/approximate_weight`).
- **PPM is never calculated.** It is taken from Walmart's own `priceInfo.unitPrice.priceString`
  (or the offer block on the PDP). If Walmart does not display it, the field is `""`.
- **Marketplace sellers are excluded** (`WMT_EXCLUDE_MARKETPLACE=true`), as are shipping-only
  offers (`WMT_SHIPPING_ONLY_MODE=exclude`) — neither is a local Walmart price.
- **No `remarks`, no `gupc14`, no extra columns.**

> Naming trap: Walmart's payload calls its price-per-measure object `unitPrice`. That value
> is exported as **`ppm`**. The exported `unit_price` column is a different, always-empty
> field computed downstream.

---

## 3. Deal parsing

Classification is by **what the shopper must do**, not by wording.

| | Volume deals | Digital deals |
|---|---|---|
| Requires action? | No — applies at the register | Yes — must be clipped/activated |
| Examples | `2 for $5`, `Buy 1, Get 1 Free`, `Must buy 4 at $1.99 each` | `Clip Coupon: Save $2`, `$3.99 each with Digital Coupon` |
| Price rule | The price Walmart **displays** | Only a price Walmart **displays** |

- `2 for $5 ($2.50 each)` → description kept verbatim, price `2.50` (the displayed each-price
  wins over the offer total; disable with `WMT_PREFER_EACH_PRICE=false`).
- `2 for $5` with no each-price → price `5.00` (the displayed offer total).
- `Buy 1, Get 1 Free` → description only, price `""`. **No price is ever invented for BOGO.**
- `Clip Coupon: Save $2` → description only, price `""`. `$2` is a discount, not a shelf
  price. Set `WMT_APPLY_COUPON_DISCOUNT=true` only if you explicitly want the post-coupon
  price derived from the displayed shelf price (off by default).
- Descriptions keep Walmart's **complete original wording**, including limits, deduped and
  condensed (`2 for $5` is superseded by `2 for $5 ($2.50 each)`).
- **Never treated as deals:** Walmart Cash / cashback, mail-in rebates, Walmart+ Savings Pass,
  and straight price cuts (`Rollback`, `Clearance`) — those are already reflected in
  `sale_price`.
- **Manufacturer coupons** may appear in `digital_deals_description` but never contribute a
  `digital_deals_price` (turn them off entirely with
  `WMT_INCLUDE_MANUFACTURER_OFFERS=false`).

---

## 4. Project layout

```
├── run_scraper.py              Entry point: config, logging, checkpoint, exit codes
├── scrapy.cfg                  Points `scrapy crawl` at the settings module
├── walmart_scraper/
│   ├── settings.py             Environment-driven Scrapy configuration
│   ├── items.py                The 22-field record + assembly rules
│   ├── middlewares.py          Pacing, block detection, exponential-backoff retries
│   ├── pipelines.py            Policy drops → dedupe → atomic publication
│   ├── extensions.py           Run lifecycle, checkpoints, manifest, Blob upload
│   ├── spiders/walmart_catalog.py
│   ├── config_loader.py        .env / CSV / selector loading + validation
│   ├── constants.py            Field order, private labels, marker vocabularies
│   ├── deals.py                Volume vs. digital classification (pure logic)
│   ├── parsing.py              __NEXT_DATA__ extraction
│   ├── weight.py               Weight extraction + approximate-weight exclusion
│   ├── blocking.py             Silent-block detection and thin-result grading
│   ├── atomic_io.py            Atomic writes + the publication gate
│   ├── state.py                Resume checkpoints
│   ├── exporters.py            Ordered JSON Lines writer
│   └── store_context.py        Store cookies, verification, picker fallback
├── config/
│   ├── Walmart_Category_URLs.csv   Categories to crawl
│   ├── stores.csv                  Store locations
│   ├── selectors.json              DOM fallback selectors
│   └── private_label_brands.txt    Extra private-label names
├── tests/                      279 tests incl. a full end-to-end crawl
├── azure/                      ACI template + deploy script, Function app
├── Dockerfile / entrypoint.sh / docker-compose.yml
└── requirements.txt            Pinned dependency stack
```

---

## 5. Local setup

```bash
git clone https://github.com/Sadeya222/Walmart-Scraper.git
cd Walmart-Scraper

make install          # python3 -m venv .venv && pip install -r requirements-dev.txt
make browsers         # playwright install --with-deps chromium

cp .env.example .env  # then edit .env
make plan             # print the crawl plan, no network
make test             # full suite
make crawl            # real crawl
```

### ⚠️ Dependency pinning

`requirements.txt` pins **Scrapy 2.13.3 / Twisted 24.11.0**. The combination
`scrapy 2.19 + twisted 26.4` was observed to start the engine but never run the scheduled
start-requests task — the crawler exits having fetched nothing, with no error. Do not bump
these two casually; if you do, run `make test` (the end-to-end crawl test catches exactly
this failure).

---

## 6. Running

```bash
python run_scraper.py --dry-run                     # validate config, print the plan
python run_scraper.py                               # crawl everything enabled
python run_scraper.py --store 2457 --store 3086     # only these stores
python run_scraper.py --category milk               # only matching categories
python run_scraper.py --resume                      # continue after an interruption
python run_scraper.py --reset-state                 # forget the checkpoint
python run_scraper.py --pages 5 --concurrency 3     # override crawl shape
python run_scraper.py --no-pdp                      # tiles only (no UPC/weight)
python run_scraper.py --min-records 200             # stricter publication gate
```

### Exit codes (usable directly by Azure alerts)

| Code | Meaning |
|------|---------|
| 0 | Dataset published |
| 1 | Unexpected error |
| 2 | Configuration invalid |
| 3 | Run aborted — scraping was blocked |
| 4 | Dataset **withheld** — previous valid dataset retained |
| 5 | Run published but the dataset is empty |

Every run writes `output/run_manifest.json` with the config in effect, drop reasons, block
counts, the dataset SHA-256 and the exit code. Proxy credentials and connection strings are
redacted from it.

---

## 7. Configuration

### `config/Walmart_Category_URLs.csv`

```csv
category_url,category,sub_category,max_pages,canary_us_item_id,enabled,notes
https://www.walmart.com/search?q=whole+milk,Grocery,Dairy & Milk,3,,true,
https://www.walmart.com/cp/grocery/976759,Grocery,All Grocery,5,104517984,true,
```

- `/search?q=`, `/cp/…` and `/browse/…` URL shapes are all supported; relative paths are
  resolved against `walmart.com`.
- `canary_us_item_id` (repeatable with `|`) is a product that *must* appear on the page.
  A canary that vanishes while the page still renders is the classic silent-block signature
  and hard-fails that page.
- `enabled=false` keeps a row in the file without crawling it. Duplicate URLs are collapsed.
- `#` comment lines and blank lines are ignored.

### `config/stores.csv`

```csv
store_id,zipcode,store_name,store_location,store_logo,latitude,longitude,enabled
2457,78745,Walmart Supercenter,"4701 W Stassney Ln, Austin, TX 78745",,30.2175,-97.8235,true
```

- `store_id` is Walmart's numeric store number; `zipcode` drives the ZIP-based fallback.
- `store_logo` is optional and is stamped verbatim onto every row from that store.
- Duplicate `store_id`s and malformed ZIPs fail the run at startup (exit 2).

### `config/selectors.json`

Every DOM selector the scraper uses, tried in order. When Walmart changes markup, **edit this
file** — no code change and no redeploy of logic.

### `config/private_label_brands.txt`

Extra store brands, one per line, merged with the built-in list
(`walmart_scraper/constants.py`). Matching is case/accent/punctuation insensitive and
whole-phrase, so `onn` matches `Onn.` but not `Bonnie Plants`.

### Environment

All knobs are documented in [`.env.example`](.env.example). Nothing secret lives in source:
proxy URLs, storage connection strings and tokens come from the environment or Azure app
settings.

---

## 8. Robustness

### Anti-blocking

- Randomised pacing between every navigation (`WMT_CRAWL_DELAY_MIN`/`MAX`) plus AutoThrottle.
- Exponential backoff with jitter on `403/412/429/5xx` and on challenge pages. Implemented as
  a middleware that returns a Deferred, because Scrapy does not honour a per-request
  `download_delay`.
- **Silent block detection** (`walmart_scraper/blocking.py`): challenge markers, HTTP status,
  empty or abnormally thin bodies, missing embedded state, thin result sets and missing
  canaries. Challenge wording on a *large* page is treated as prose, not a wall.
- Images, fonts, media and analytics are aborted before download.

### Fail-safe publication

Records are staged to a temp file; the dataset is only promoted if the run passes:

- at least `WMT_MIN_RECORDS_PER_RUN` records,
- no more than `WMT_MAX_SUSPECT_RATIO` suspect pages,
- not hard-blocked (`WMT_MAX_BLOCKS_PER_RUN`).

A rejected run is **quarantined** with its reason, the previous dataset is left untouched,
and the process exits `4`. All writes use temp-file + `fsync` + `os.replace`, so a killed
container cannot leave a half-written file.

### Resume

`--resume` reloads `state/run_state.json` (completed listings, fetched products, seen items,
blocked URLs). A resumed run still loads one page per store to re-verify the store context,
then skips all completed pagination and re-fetches nothing. Critically, the staged file is
**seeded from the existing dataset**, so a partial resume can never publish a partial file
over a complete one.

---

## 9. Azure deployment

### Option A — Container Instances (recommended)

```bash
az login
az group create -n rg-walmart-scraper -l eastus
az acr create -g rg-walmart-scraper -n wmscraper --sku Basic --admin-enabled true

./azure/aci/deploy-aci.sh rg-walmart-scraper wmscraper
az container logs -g rg-walmart-scraper -n walmart-scraper --follow
```

The template mounts an Azure Files share at `/data` so `output/`, `state/` and `quarantine/`
survive restarts, and `WMT_RESUME=true` is set so an interrupted run continues.

### Option B — Azure Functions (timer trigger)

`azure/function-app/` contains a Python v2 model function with:

- `walmart_daily_crawl` — cron-scheduled; runs `run_scraper.py` in the same image and raises
  when the exit code is non-zero, so Application Insights alerts fire on a withheld dataset.
- `walmart_health` — HTTP endpoint returning the last run manifest (`200` published,
  `503` withheld).

```bash
cd azure/function-app
cp local.settings.json.example local.settings.json
az functionapp create -g rg-walmart-scraper -n fn-walmart-scraper \
    --storage-account wmscraperdata --plan asp-walmart \
    --deployment-container-image-name wmscraper.azurecr.io/walmart-scraper:latest
```

### Option C — GitHub Actions

`azure/github-actions/azure-deploy.yml` builds, pushes, starts the container group, waits
for completion and fails the run if no dataset was published. Trigger it on a schedule or
manually.

> The workflow files live in `azure/github-actions/` rather than `.github/workflows/`
> because pushing workflow files needs the `workflows` permission on the GitHub App token.
> Copy them into `.github/workflows/` to activate them - see
> [`azure/github-actions/README.md`](azure/github-actions/README.md).

### Blob upload

Set `AZURE_STORAGE_CONNECTION_STRING` and `AZURE_STORAGE_CONTAINER` and the published dataset
is uploaded to `{prefix}/{timestamp}/{run_id}/walmart_catalog.jsonl`. Without
`azure-storage-blob` installed the scraper logs an error and keeps the local file.

### Local Docker

```bash
docker build -t walmart-scraper:local .
docker run --rm -v "$PWD/config:/app/config:ro" -v "$PWD/output:/app/output" \
    --env-file .env walmart-scraper:local --crawl
```

---

## 10. Tests

```bash
make test       # 375 tests
make lint       # flake8
make coverage
```

The suite covers the pure logic (deals, weight exclusion, price/measure normalisation, block
detection, atomic publication, resume state, config validation, store verification, the
22-field contract) **and** an end-to-end crawl: `run_scraper.py` is executed as a subprocess
against a local HTTP server serving captured Walmart fixtures, exercising settings load, store
verification, listing parse, pagination, PDP merge, deal classification, policy drops, dedupe,
atomic publication, the validation gate and the exit code. A blocked scenario asserts the
previous dataset survives byte-for-byte.

### Real captured data vs. hand-written fixtures

Two different kinds of fixture are in play, and the difference matters:

- **`tests/fixtures/real/`** — real Walmart pages and payloads, reproduced from public
  repository commits and otherwise unchanged. `tests/fixtures/real/SOURCES.md` records the
  exact source of each file, what it proves, and which captures were evaluated and rejected.
- **`tests/fixtures/`** (everything else) — HTML/JSON **written for this project**. These are
  useful for end-to-end wiring but they can only prove the parser agrees with its own author's
  assumptions, never that it agrees with Walmart.

`tests/test_real_captures.py` (96 tests) runs the real code paths against the real captures and
asserts the values Walmart actually sent. That is what found these defects, all of which the
hand-written fixtures were consistent with:

| Real-data finding | Defect it exposed |
| --- | --- |
| Product payload is at `props.pageProps.initialData.data.product` | The parser lacked the extra `data` level, so `extract_pdp_product()` returned `None` on a real PDP |
| Listing tiles serve `itemPrice`/`linePrice`/`wasPrice` as `""` and `minPrice` as `0`, with the amount only in `priceInfo.priceDetails.priceLines[]` | Real prices exported as `0.00`; a real `DISCOUNTED_PRICE` + `COMPARISON/WAS_PRICE` markdown was invisible |
| One spec sheet can state several different measures (`Size: 16 oz` *and* `Weight: 1.12 lb`) | Taking the first parseable candidate reported the size as the weight |
| `product.location` is a structured object | It was being stringified into a text field |
| There is no `gtin` key in the product payload | A `gtin` → `upc` fallback would have read nothing; the real key is `product.upc` |

Reverting any of those fixes makes the real-capture tests fail, which is the point: they fail
for the right reason.

Playwright is switched off for those tests (`WMT_USE_PLAYWRIGHT=false`), so the browser
layer is the one part not covered by CI — see the next section.

---

## 11. Known limitations

- **Walmart's markup and payload shape change.** Parsing prefers the embedded `__NEXT_DATA__`
  state and falls back to `config/selectors.json`, but a layout change can still require
  selector or path updates. The store cookie names in `store_context.py` are likewise observed
  values, which is exactly why store verification is mandatory rather than best-effort.
- **Real-capture coverage is narrow.** Only three genuine captures are in the repository (one
  PDP, one search listing, 15 transcribed products). They cover pricing, weight, ppm, brand,
  seller, and payload-based store verification, but **not** volume deals, clipped digital
  coupons, marketplace sellers, or shipping-only offers — those rules are only exercised by the
  hand-written fixtures and therefore remain assumptions about Walmart until confirmed against
  live pages. The parser is written to degrade to `""` rather than guess when a shape is
  unrecognised.
- **No live run has been performed here.** `walmart.com` was unreachable from the build
  environment (DNS resolved, TCP connect refused). The sample rows in
  `config/Walmart_Category_URLs.csv` use `/search?q=…` URLs, whose shape is guaranteed valid;
  if you replace them with `/cp/<id>` category URLs, verify those ids against a live site first.
  The first live run should be `--pages 1 --store <id>` to confirm the payload paths still hold.
- **The browser layer is not exercised by CI** (the Chromium binary cannot be downloaded
  there). `make browsers` plus one `--pages 1 --store <id>` run against a live store is the
  check to run after any change to `spiders/` or `store_context.py`.
- **Store verification cannot always read an indicator.** Verification now prefers the location
  Walmart resolved *into the payload* (`location.postalCode`, `location.storeIds`,
  `fulfillmentLabel[].locationText`) over scraping the rendered header, and only falls back to
  the indicator text. When neither is present the optional `ui` step drives the picker; if that
  also fails the store is skipped. Supply a captured `WMT_STORAGE_STATE` session for the most
  reliable pinning.
- **`ppm` coverage is only as good as the page.** Many tiles do not display a unit price; those
  rows correctly carry `""` rather than a computed value.

## 12. Legal and responsible use

This tool issues automated requests to a third-party website. `ROBOTSTXT_OBEY` is `False`
because the target paths are disallowed for crawlers — **you** are responsible for having the
authorisation to collect this data (a commercial data agreement, an affiliate/partner
arrangement, or Walmart's own APIs), and for complying with Walmart's Terms of Use and
applicable law. Keep `WMT_CONCURRENCY` low and the pacing delays on: they are both a
politeness control and the cheapest defence against being blocked.

## 13. Secrets

`.env` is git-ignored. Only `.env.example` (with placeholders) is committed. Proxy URLs are
redacted in the run manifest, and `AZURE_STORAGE_CONNECTION_STRING` is passed to containers as
a **secure** environment variable in the ACI template.
