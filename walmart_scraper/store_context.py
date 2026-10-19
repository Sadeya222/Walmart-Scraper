"""Store + fulfillment context: cookies, verification, and UI fallback.

Every row this scraper emits is *location specific*, so the store context is not
a nice-to-have - a silently wrong store means every price in the dataset is
wrong.  Three layers, in order of cost:

1. **Cookies** - seed the browser context with Walmart's store cookies before the
   first navigation (:func:`store_cookies`).  Cheap and works for the vast
   majority of runs.
2. **Verification** - after the first page loads, read the store indicator that
   Walmart renders in the header and compare it with the configured store
   (:class:`StoreVerifier`).  Mismatch is a hard failure for that store unless
   ``WMT_REQUIRE_STORE_VERIFIED=false``.
3. **UI fallback** - drive the store picker (ZIP -> store -> "Use this store")
   with Playwright when cookies alone did not stick
   (:func:`select_store_via_ui`).

Because Walmart changes both cookie names and picker markup, every DOM selector
comes from ``config/selectors.json`` and a captured ``storage_state.json`` can be
supplied via ``WMT_STORAGE_STATE`` for maximum reliability.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config_loader import StoreConfig
from .text import clean_text, fold

__all__ = [
    "store_cookies",
    "playwright_context_kwargs",
    "StoreVerification",
    "StoreVerifier",
    "select_store_via_ui",
    "DEFAULT_VIEWPORT",
]

logger = logging.getLogger("walmart.store_context")

DEFAULT_VIEWPORT = {"width": 1680, "height": 1050}
WALMART_ORIGIN = "https://www.walmart.com"
COOKIE_DOMAIN = ".walmart.com"


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #
def store_cookies(store: StoreConfig, *, max_age_days: int = 90) -> list[dict[str, Any]]:
    """Build Walmart's store/location cookies for a Playwright browser context.

    ``wm_st`` carries the preferred store id plus coordinates; ``wm_store`` and
    ``wm_zip`` back it up for pages that read them individually.  Coordinates are
    optional - ZIP alone is enough for Walmart to resolve a preferred store.
    """
    max_age = int(max_age_days) * 24 * 3600
    preferred = {
        "id": store.store_id,
        "prefStore": True,
    }
    if store.latitude and store.longitude:
        try:
            preferred["lat"] = float(store.latitude)
            preferred["lon"] = float(store.longitude)
        except ValueError:  # pragma: no cover - defensive, config is validated
            logger.warning("store %s has non numeric coordinates; using ZIP only", store.store_id)

    raw: dict[str, str] = {
        "wm_st": json.dumps(preferred, separators=(",", ":")),
        "wm_store": store.store_id,
        "wm_zip": store.zipcode,
        "wm_ord": store.store_id,
    }
    if store.latitude and store.longitude:
        raw["wm_lat"] = store.latitude
        raw["wm_lng"] = store.longitude

    return [
        {
            "name": name,
            "value": value,
            "domain": COOKIE_DOMAIN,
            "path": "/",
            "expires": int(time.time()) + max_age,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
        for name, value in raw.items()
    ]


def playwright_context_kwargs(
    store: StoreConfig,
    *,
    user_agent: str = "",
    storage_state_path: str = "",
    viewport: dict[str, int] | None = None,
    proxy: str = "",
    timezone: str = "America/Chicago",
    locale: str = "en-US",
) -> dict[str, Any]:
    """Per-store Playwright context kwargs (each store gets an isolated context).

    Isolating contexts per store is what keeps two locations' cookies from
    bleeding into each other when a run crawls several stores in one process.
    """
    kwargs: dict[str, Any] = {
        "user_agent": user_agent or None,
        "viewport": viewport or DEFAULT_VIEWPORT,
        "locale": locale,
        "timezone_id": timezone,
        "ignore_https_errors": True,
        "java_script_enabled": True,
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        },
    }
    if user_agent:
        kwargs["user_agent"] = user_agent
    else:
        kwargs.pop("user_agent")

    state_path = Path(storage_state_path) if storage_state_path else None
    if state_path and state_path.exists():
        # Operator-captured session (cookies + localStorage) is the most
        # reliable way to pin a store; cookies are merged on top.
        kwargs["storage_state"] = str(state_path)

    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return kwargs


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass
class StoreVerification:
    """Result of checking that a page is showing the store we configured."""

    verified: bool = False
    store: StoreConfig | None = None
    indicator_text: str = ""
    matched_on: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "store_id": self.store.store_id if self.store else "",
            "zipcode": self.store.zipcode if self.store else "",
            "indicator_text": self.indicator_text,
            "matched_on": self.matched_on,
            "detail": self.detail,
        }


# Words that appear in every store name/address and therefore prove nothing.
_GENERIC_LOCATION_WORDS = frozenset(
    {
        "walmart", "supercenter", "neighborhood", "market", "store", "center",
        "street", "st", "avenue", "ave", "road", "rd", "drive", "dr", "lane",
        "ln", "boulevard", "blvd", "highway", "hwy", "parkway", "pkwy", "way",
        "court", "ct", "circle", "cir", "place", "pl", "square", "sq", "loop",
        "north", "south", "east", "west", "suite", "ste", "unit", "apt",
        "shopping", "plaza", "mall", "towne", "crossing", "commons", "usa",
    }
)


def _distinctive_tokens(value: str) -> list[str]:
    """Meaningful words from an address/name (>=5 chars, not a generic suffix)."""
    return [
        token
        for token in fold(value).split()
        if len(token) >= 5 and token not in _GENERIC_LOCATION_WORDS
    ]


class StoreVerifier:
    """Compares the rendered store indicator against the configured store.

    A page is accepted when the indicator contains the ZIP, the store id, or the
    configured store name.  Walmart renders the indicator in several shapes
    ("Store #2457", "Austin - Stassney", "Pickup from 78745"), so all three
    signals are tried and the winning one is reported for the run manifest.
    """

    def __init__(self, selectors: Sequence[str] = ()) -> None:
        self.selectors = tuple(selectors) or (
            "[data-testid='store-picker-trigger']",
            "button[aria-label*='Store']",
            "[data-automation-id='store-picker']",
        )

    # ------------------------------------------------------------------ #
    def verify_from_text(self, indicator_text: str, store: StoreConfig) -> StoreVerification:
        """Verify using text already extracted from the page (no DOM needed)."""
        text = clean_text(indicator_text)
        folded = fold(text)
        if not text:
            return StoreVerification(False, store, "", "", "no_store_indicator_found")

        zip_code = store.zipcode.split("-")[0]
        if zip_code and zip_code in folded.replace(" ", ""):
            return StoreVerification(True, store, text, "zipcode", "")
        if store.store_id and re.search(rf"(?<!\d){re.escape(store.store_id)}(?!\d)", folded):
            return StoreVerification(True, store, text, "store_id", "")
        name_key = fold(store.store_name)
        if name_key and name_key in folded:
            return StoreVerification(True, store, text, "store_name", "")
        # The configured store_name is usually generic ("Walmart Supercenter"),
        # while the header shows the site name ("Austin - Stassney").  Distinctive
        # address tokens are the only reliable third signal.
        address_tokens = _distinctive_tokens(store.store_location)
        if address_tokens and any(token in folded for token in address_tokens):
            return StoreVerification(True, store, text, "address_token", "")
        name_tokens = _distinctive_tokens(store.store_name)
        if name_tokens and all(token in folded for token in name_tokens):
            return StoreVerification(True, store, text, "store_name_tokens", "")

        return StoreVerification(
            False, store, text, "", f"indicator_does_not_match_store_{store.store_id}_{zip_code}"
        )

    # ------------------------------------------------------------------ #
    async def verify_page(self, page: Any, store: StoreConfig) -> StoreVerification:
        """Extract the indicator from a live Playwright page and verify it.

        Never raises: a selector that Walmart removed yields "no indicator
        found", which the caller treats as unverified rather than as a crash.
        """
        for selector in self.selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = clean_text(await locator.inner_text(timeout=2_000))
                if not text:
                    text = clean_text(await locator.get_attribute("aria-label"))
                if not text:
                    continue
                result = self.verify_from_text(text, store)
                if result.verified:
                    return result
                return result
            except Exception as exc:  # pragma: no cover - live browser only
                logger.debug("store indicator %r failed: %s", selector, exc)
                continue
        return StoreVerification(False, store, "", "", "no_store_indicator_found")


# --------------------------------------------------------------------------- #
# UI fallback
# --------------------------------------------------------------------------- #
async def select_store_via_ui(
    page: Any,
    store: StoreConfig,
    selectors: dict[str, list[str]],
    *,
    timeout_ms: int = 15_000,
) -> bool:
    """Drive Walmart's store picker: open it, enter the ZIP, choose the store.

    Returns ``True`` when a "use this store" control was activated.  Every step is
    wrapped so a markup change degrades to ``False`` (the caller then falls back
    to cookie-only context and reports the store as unverified) instead of
    aborting the whole run.
    """
    opened = await _click_first(page, selectors.get("store_picker_open", []), timeout_ms)
    if not opened:
        logger.warning("store picker would not open for store %s", store.store_id)
        return False

    typed = await _fill_first(page, selectors.get("store_picker_zip_input", []), store.zipcode, timeout_ms)
    if not typed:
        logger.warning("no ZIP input found in store picker for store %s", store.store_id)
        return False

    await _click_first(page, selectors.get("store_picker_submit", []), timeout_ms)

    chosen = await _click_matching(
        page,
        selectors.get("store_picker_results", []),
        needles=(store.store_id, store.zipcode, store.store_name),
        timeout_ms=timeout_ms,
    )
    if not chosen:
        logger.warning("store %s not present in picker results", store.store_id)
        return False

    await _click_first(page, selectors.get("store_picker_use_store", []), timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # pragma: no cover - live browser only
        pass
    logger.info("selected store %s via UI for ZIP %s", store.store_id, store.zipcode)
    return True


async def _click_first(page: Any, selectors: Sequence[str], timeout_ms: int) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.click(timeout=timeout_ms)
            return True
        except Exception:  # pragma: no cover - live browser only
            continue
    return False


async def _fill_first(page: Any, selectors: Sequence[str], value: str, timeout_ms: int) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            await locator.fill(value, timeout=timeout_ms)
            return True
        except Exception:  # pragma: no cover - live browser only
            continue
    return False


async def _click_matching(
    page: Any, selectors: Sequence[str], needles: Sequence[str], timeout_ms: int
) -> bool:
    wanted = [fold(needle) for needle in needles if needle]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 20)):
                item = locator.nth(index)
                text = fold(clean_text(await item.inner_text(timeout=2_000)))
                if any(needle and needle in text for needle in wanted):
                    await item.click(timeout=timeout_ms)
                    return True
        except Exception:  # pragma: no cover - live browser only
            continue
    return False
