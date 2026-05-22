"""
Layer 1: API Interception.

On first visit, Playwright (stealth mode) intercepts all XHR/fetch network
requests made by the SPA frontend.  It also checks for Next.js __NEXT_DATA__
embedded JSON, which many Next.js sites use for SSR hydration.

Discovered API endpoint pattern is cached in .api_endpoints.json so subsequent
runs bypass the browser entirely.  If the cached endpoint stops returning price
data, the cache is invalidated and re-discovery runs automatically.
"""
import json
import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Response

from ..browser import stealth_page

log = logging.getLogger(__name__)

API_CACHE_FILE = Path(__file__).parent.parent / ".api_endpoints.json"

_PRICE_SIGNAL_KEYS = {"fiyat", "price", "priceTL", "marketFiyat", "markets",
                      "stores", "kampanyaFiyati", "normalFiyat"}


def _looks_like_price_data(body) -> bool:
    if isinstance(body, list):
        body = body[0] if body else {}
    if not isinstance(body, dict):
        return False
    keys = {k.lower() for k in body.keys()}
    return bool(keys & {k.lower() for k in _PRICE_SIGNAL_KEYS})


def _extract_next_data(raw_html: str) -> dict | None:
    """Pull __NEXT_DATA__ out of raw page HTML without running JS."""
    import re
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw_html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


class ApiInterceptor:
    """
    Discovers the backend API by watching network traffic on a product page,
    then provides a direct async HTTP client for subsequent calls.
    """

    def __init__(self):
        self._endpoints: dict = self._load_cache()
        # Shared lock: only one discover() runs at a time across coroutines
        self._discover_lock = asyncio.Lock()
        self._discovering = False

    def _load_cache(self) -> dict:
        if API_CACHE_FILE.exists():
            try:
                return json.loads(API_CACHE_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        API_CACHE_FILE.write_text(json.dumps(self._endpoints, indent=2))

    def invalidate(self):
        self._endpoints = {}
        if API_CACHE_FILE.exists():
            API_CACHE_FILE.unlink()

    async def discover(self, sample_product_id: str = "0002",
                       sample_slug: str = "cokkolata-cikolatali-bar-250-gr"):
        """
        Load a product page with stealth Playwright, intercept XHR calls, check
        __NEXT_DATA__, and persist the discovered API pattern.
        """
        async with self._discover_lock:
            # Another coroutine may have already discovered while we waited
            if self._endpoints.get("product_template"):
                return self._endpoints

            url = f"https://marketfiyati.org.tr/detay/{sample_product_id}/{sample_slug}"
            candidates: list[dict] = []
            next_data: dict | None = None

            async with async_playwright() as p:
                browser, context, page = await stealth_page(p)

                async def on_response(response: Response):
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    try:
                        body = await response.json()
                    except Exception:
                        return
                    if _looks_like_price_data(body):
                        parsed = urlparse(response.url)
                        candidates.append({
                            "url": response.url,
                            "path": parsed.path,
                            "host": parsed.netloc,
                            "sample_body_keys": list(body.keys())
                            if isinstance(body, dict) else
                            list(body[0].keys()) if body else [],
                        })

                page.on("response", on_response)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=40_000)
                    # Also try __NEXT_DATA__ from rendered HTML
                    raw_html = await page.content()
                    next_data = _extract_next_data(raw_html)
                except Exception as e:
                    log.warning("Page load during discovery failed: %s", e)
                finally:
                    await browser.close()

            if next_data:
                log.info("Found __NEXT_DATA__ — site uses Next.js SSR")
                self._endpoints = {
                    "type": "next_data",
                    "product_template": "/detay/{product_id}/{slug}",
                    "host": "https://marketfiyati.org.tr",
                    "next_data_sample_keys": list(_flatten_keys(next_data))[:20],
                }
                self._save_cache()
                return self._endpoints

            if not candidates:
                raise RuntimeError(
                    "Could not discover any price API endpoints. "
                    "The site may have changed its network protocol."
                )

            best = next(
                (c for c in candidates if sample_product_id in c["path"]),
                candidates[0],
            )
            template = best["path"]
            template = template.replace(sample_product_id, "{product_id}")
            template = template.replace(sample_slug, "{slug}")

            self._endpoints = {
                "type": "xhr",
                "host": f"https://{best['host']}",
                "product_template": template,
                "raw_candidates": candidates,
            }
            self._save_cache()
            return self._endpoints

    def product_url(self, product_id: str, slug: str) -> str | None:
        if not self._endpoints:
            return None
        tmpl = self._endpoints.get("product_template", "")
        path = tmpl.format(product_id=product_id, slug=slug)
        return self._endpoints.get("host", "https://marketfiyati.org.tr") + path

    @property
    def is_ready(self) -> bool:
        return bool(self._endpoints.get("product_template"))

    @property
    def endpoint_type(self) -> str:
        return self._endpoints.get("type", "unknown")


def _flatten_keys(obj, prefix="", depth=0) -> set:
    keys = set()
    if depth > 4:
        return keys
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else k
            keys.add(full)
            keys |= _flatten_keys(v, full, depth + 1)
    elif isinstance(obj, list) and obj:
        keys |= _flatten_keys(obj[0], prefix, depth + 1)
    return keys
