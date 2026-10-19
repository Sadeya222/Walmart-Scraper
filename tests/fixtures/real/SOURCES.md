# Real captured Walmart data

These are **real** Walmart pages and payloads, not synthetic fixtures. They are
used by `tests/test_real_captures.py`, which asserts the exact values Walmart
served. They were retrieved through `api.github.com` (the `contents` endpoint)
because `walmart.com` is not reachable from the test environment; each file is
reproduced from a public repository commit and is unchanged except where noted.

| File | Source repository / path | What it proves |
| --- | --- | --- |
| `poc_pdp.html` | `IR-System-TTU/poc-walmart` → `product_detail.html` | A real product detail page (306 KB). Product payload lives at `props.pageProps.initialData.data.product` — note the extra `data` level. Also: `priceInfo.unitPrice.priceString`, `idml.specifications` as a *sibling* of the product node, `product.location.{postalCode,storeIds}`, `fulfillmentLabel[].locationText`. There is **no** `gtin` key; the UPC is `product.upc`. |
| `location_listing.json` | `Montaga-dev/walmart-location-crawler` → `tests/fixtures/search_listing.json` | A real `/search` payload. Listing tiles keep amounts in `priceInfo.priceDetails.priceLines[]` and leave `itemPrice`/`linePrice`/`wasPrice`/`unitPrice` as `""` with `minPrice: 0`. 2 of 4 entries in `itemStacks[0].items[]` are placeholders carrying only `__typename`. |
| `whatnut_capture.json` | `MaxGhenis/whatnut` → `data/prices/raw/walmart/walmart_browser_capture_2026-09-23.json` | 15 products transcribed live on 2026-09-23/24 with location context ZIP 20001 / store 3035. Ground truth for `ppm` (including the cent-sign form `41.9 ¢/oz`) and for the spec strings that `weight` must be selected from. |

## Known defect in one capture (left as-is on purpose)

In `poc_pdp.html` the bytes for the cent sign in `"unitPrice": … "priceString"`
are `EF BF BD` — the UTF-8 encoding of U+FFFD. The corruption is present in the
upstream repository's own file, not introduced here. It is **not** repaired,
because doing so would fabricate data; instead `test_real_captures.py` asserts
that a PPM string Walmart serves with an undecodable unit character is rejected
(`ppm == ""`) rather than exported corrupted.

## Evaluated and deliberately excluded

* `andreeaplaton20/WorkerPool` → `data/2025-12-23/us/15_walmart.com.html` and
  `mrcampbell/remart_garden` → `src/walmart-category-products/3620-…-1.html`:
  real pages containing `__NEXT_DATA__` but no product array — no assertion value.
* `nexscope-ai/nexscope-ecommerce-api` → `examples/responses/*`, `schemas/*`:
  a third-party aggregator's **normalized** response shapes. Useful only for
  corroborating field names; they are not Walmart payloads and would have
  re-introduced exactly the synthetic-fixture problem these files exist to avoid.
