# Source & Test Verification — Walmart #86

**Date:** 2026-11-19
**Environment:** Linux (x86_64), Python 3.11.2, dependencies pinned in `requirements.txt`
**Repository (public):** https://github.com/Sadeya222/Walmart-Scraper
**Branch:** `arena/e1754fad-walmart-scraper`
**This file is part of the commit it describes — clone that commit to verify everything below.**

Nothing in this document is aspirational. Everything listed under "Recorded results" was
executed on the machine that produced this commit; everything under "Not done yet" has not
been performed, and nothing here should be read as claiming otherwise.

## Reproduce

```bash
git clone https://github.com/Sadeya222/Walmart-Scraper.git
cd Walmart-Scraper
git checkout <commit-of-this-file>
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests          # expect: 387 passed, 3 skipped
.venv/bin/python -m pytest tests --cov=walmart_scraper --cov-report=term
.venv/bin/python run_scraper.py --dry-run # no network access required
```

The test suite needs no browser binary and no Walmart access. Playwright is switched off
in the integration tests (`WMT_USE_PLAYWRIGHT=false`); every layer below the browser —
config validation, store verification, listing parse, pagination, PDP merge, deal
classification, policy drops, dedupe, atomic publication, the validation gate and the exit
code — executes exactly as it does in production, against a local HTTP fixture server.

## Recorded results (this machine, 2026-11-19)

```
387 passed, 3 skipped in ~52s
```

Coverage (`--cov=walmart_scraper`, platform linux, python 3.11.2):

```
TOTAL                    2570 stmts   276 miss   89%
  items.py                95                99%
  atomic_io.py           137                 4    97%
  blocking.py            126                 5    96%
  deals.py               144                 6    96%
  parsing.py             367                29    92%
  evidence.py            151                13    91%
  store_context.py       188                63    66%   # browser layer; not CI-testable
```

Two different kinds of fixture back the suite (see `tests/fixtures/real/SOURCES.md`):

* `tests/test_real_captures.py` — assertions run against **real Walmart pages/payloads**
  (one PDP, one search listing, a 15-product capture), reproduced verbatim from public
  repositories, covering pricing, PPM, weight selection, seller fields and payload-based
  store verification.
* `tests/test_integration_crawl.py` — the real entry point (`run_scraper.py`) executed as a
  subprocess against a local fixture server. This proves the pipeline wiring end to end.
  **It is fixture-based and is not, and must not be represented as, a collection of Walmart
  data.**

`python run_scraper.py --dry-run` (offline) validates the committed configuration and prints:

```
Walmart catalog scraper - crawl plan (1 store(s) x 8 categor(ies) = 8 listing sequence(s))
  store 2457  ZIP 78745  Walmart Supercenter  4701 W Stassney Ln, Austin, TX 78745
  Grocery > Dairy & Milk      https://www.walmart.com/search?q=whole+milk        (max 3 pages)
  Grocery > Breakfast Cereal  https://www.walmart.com/search?q=breakfast+cereal  (max 3 pages)
  Household Essentials > Paper Goods  .../search?q=toilet+paper                  (max 3 pages)
  Household Essentials > Laundry      .../search?q=laundry+detergent             (max 3 pages)
  Health > OTC Pain Relief    .../search?q=pain+relief                           (max 2 pages)
  Baby > Infant Formula       .../search?q=baby+formula                          (max 2 pages)
  Pets > Dog Food             .../search?q=dog+food                              (max 2 pages)
  Grocery > Frozen Foods      .../search?q=frozen+pizza                          (max 2 pages)
  publish requires: >= 25 records, <= 34% suspect pages, <= 10 blocked pages
  policy          : marketplace=excluded, shipping-only=exclude, price_format=numeric
```

## Not done yet (nothing hidden)

* **No Azure deployment has been performed.** No Azure subscription, credential or resource
  was used; `az` is not even installed in the build environment. The `azure/` directory
  contains deployment *assets* (ACI template + script, Function app, GitHub Actions) that
  have never been executed.
* **No live collection has been performed.** `walmart.com` was unreachable from the build
  environment, so no dataset (`walmart_catalog.jsonl`), no `run_manifest.json`, no
  timestamped run log, no pickup-store export and no raw product/store response behind a
  displayed price exists yet.
* Everything the reviewer listed for live-run verification — pickup-store export, original
  product and store/offer response behind a price, run manifest, coverage of categories and
  fields in collected data, timestamped log — will be produced only on a US-side run with
  Walmart access, pending the contest holder's decision. The committed `--evidence` flag
  is the mechanism: a live run writes `output/evidence/<run_id>/store_context_store<id>.json`
  (the store/fulfillment context Walmart resolved, verification outcome, seeded location
  cookies) and `price_evidence_store<id>_<item>.json` (the raw listing tile and raw PDP
  product node exactly as served, plus the exported record), alongside a timestamped log
  and `run_manifest.json`.

Collection and deployment are on hold until the holder authorises a US-side acceptance run.
