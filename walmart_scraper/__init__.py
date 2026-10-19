"""Walmart location-specific catalog scraper.

A Scrapy + Scrapy-Playwright project that crawls Walmart category pages for one or
more configured store locations and emits a strictly ordered JSON Lines dataset.

Package layout
--------------
``constants``   Field order, private-label brands, marker vocabularies, exit codes.
``text``        Deterministic text/price/measure normalisation helpers.
``deals``       Volume vs. digital deal classification (pure, unit tested).
``weight``      Weight extraction + approximate-weight exclusion.
``blocking``    Silent block / challenge detection and thin-result validation.
``atomic_io``   Crash safe atomic file writes and dataset publication.
``state``       Resume-after-interruption checkpoints.
``store_context`` Store + fulfillment context cookies, verification, selectors.
``exporters``   Ordered JSON Lines exporter with atomic publication.
``items``       The single exported item type (exact 22 field order).
``middlewares`` Retry/backoff, block detection, store context enforcement.
``pipelines``   Validation, dedupe, normalisation, atomic write + validation gate.
``settings``    Environment driven Scrapy configuration.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
