"""Shared pytest fixtures.

Two kinds of test live in this suite:

* **unit tests** - the pure logic modules (deals, weight, text, blocking,
  atomic_io, state, config, store verification).  No network, no browser.
* **integration tests** - the real Scrapy engine running the real spider against
  a local HTTP server that serves captured Walmart fixtures.  Playwright is
  switched off for these, so every layer *below* the browser (navigation,
  pagination, parsing, deal classification, policy drops, dedupe, atomic
  publication, validation gate) executes exactly as it does in production.
"""

from __future__ import annotations

import functools
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


# --------------------------------------------------------------------------- #
class FakeWalmartServer:
    """Serves fixture pages; ``mode`` switches it into a bot-blocked state."""

    def __init__(self) -> None:
        self.mode = "healthy"          # healthy | blocked | thin
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def start(self) -> "FakeWalmartServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # keep pytest output clean
                return

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                outer.requests.append(self.path)
                body, status = outer._route(self.path)
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------ #
    def _route(self, path: str) -> tuple[str, int]:
        if self.mode == "blocked":
            return fixture_text("challenge_page.html"), 200
        if self.mode == "thin":
            return "<html><body>no results</body></html>", 200

        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/ip/"):
            return _pdp_page(parsed.path), 200
        if parsed.path.startswith("/search"):
            page = int(query.get("page", ["1"])[0])
            if page > 1:
                # A valid payload with no items: the normal end of pagination.
                return _empty_search_page(), 200
            return fixture_text("category_page.html"), 200
        return "<html><body>not found</body></html>", 404


# The real PDP fixture carries one product's data.  Serving it verbatim for every
# product id would give every product the same GTIN, which would then make the
# dedupe pipeline collapse them - masking the policy drops under test.  So the
# canned PDP is served only for its own product, and every other product gets a
# minimal PDP carrying just its own id (and no barcode).
_CANNED_PDP_ID = "104517984"


@functools.lru_cache(maxsize=64)
def _pdp_page(path: str) -> str:
    product_id = path.rstrip("/").rsplit("/", 1)[-1]
    if product_id == _CANNED_PDP_ID:
        return fixture_text("pdp_page.html")
    payload = json.dumps(
        {
            "props": {
                "pageProps": {
                    "initialData": {
                        "product": {
                            "usItemId": product_id,
                            "name": f"Product {product_id}",
                            "canonicalUrl": f"/ip/Product-{product_id}/{product_id}",
                            "price": {"currentPrice": {"price": 1.0}},
                            "sellerName": "Walmart.com",
                        }
                    }
                }
            }
        }
    )
    return (
        "<!DOCTYPE html><html><head><title>pdp</title></head><body>"
        f"<script id=\"__NEXT_DATA__\" type=\"application/json\">{payload}</script>"
        "</body></html>"
    )


@functools.lru_cache(maxsize=1)
def _empty_search_page() -> str:
    empty_payload = json.dumps(
        {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"itemsV2": []}]}}}}}
    )
    padding = "<!-- " + ("x" * 40_000) + " -->"
    return (
        "<!DOCTYPE html><html><head><title>walmart</title></head><body>"
        f"{padding}"
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{empty_payload}</script></body></html>"
    )


@pytest.fixture()
def fake_walmart():
    server = FakeWalmartServer().start()
    try:
        yield server
    finally:
        server.stop()
