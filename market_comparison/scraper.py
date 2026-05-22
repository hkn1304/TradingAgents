"""
Main scraper: Layer 1 (direct API) → Layer 2 (DOM/stealth) → Layer 3 (AI).
The direct API is the primary path; DOM and AI are fallbacks for resilience.
"""
import asyncio
import logging
import re
from typing import Optional

from .models import Product
from .extractors.api_client import MarketApiClient
from .extractors.dom_extractor import DomExtractor
from .extractors.ai_fallback import AiFallbackExtractor

log = logging.getLogger(__name__)

BASE_URL = "https://marketfiyati.org.tr"
SITEMAP_URLS = [
    "https://marketfiyati.org.tr/sitemaps/sitemap-1.xml",
    "https://marketfiyati.org.tr/sitemaps/sitemap-2.xml",
]


class MarketScraper:
    def __init__(self, use_ai_fallback: bool = True):
        self._api = MarketApiClient()
        self._dom = DomExtractor()
        self._ai = AiFallbackExtractor() if use_ai_fallback else None

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_product(self, product_id: str, slug: str = "") -> Optional[Product]:
        """
        Fetch a product with automatic layer fallback.
        Layer 1 (direct API) → Layer 2 (stealth DOM) → Layer 3 (AI).
        """
        # Layer 1: direct API
        try:
            product = await self._api.get_product_prices(product_id, slug.replace("-", " "))
            if product:
                log.debug("Layer 1 (API) succeeded for %s", product_id)
                return product
        except Exception as e:
            log.warning("Layer 1 (API) failed for %s: %s", product_id, e)

        # Layer 2: stealth DOM extraction
        try:
            product = await self._dom.extract_product(product_id, slug)
            if product:
                log.info("Layer 2 (DOM) succeeded for %s", product_id)
                return product
        except Exception as e:
            log.warning("Layer 2 (DOM) failed for %s: %s", product_id, e)

        # Layer 3: AI fallback
        if self._ai:
            try:
                product = await self._ai.extract_product(product_id, slug)
                if product:
                    log.info("Layer 3 (AI) succeeded for %s", product_id)
                    return product
            except Exception as e:
                log.warning("Layer 3 (AI) failed for %s: %s", product_id, e)

        log.error("All layers failed for %s/%s", product_id, slug)
        return None

    async def search_product(self, query: str, limit: int = 5) -> list[Product]:
        """
        Search by text query. Returns up to `limit` products with prices.
        Automatically normalizes ASCII-typed Turkish words (e.g. sut → süt).
        """
        try:
            products = await self._api.search_with_prices(query, limit=limit,
                                                           normalize=True)
            if products:
                return products
        except Exception as e:
            log.warning("API search failed for '%s': %s", query, e)

        # Fallback: sitemap grep search
        try:
            sitemap_hits = await self._sitemap_search(query, limit)
            tasks = [self.get_product(pid, slug) for pid, slug in sitemap_hits]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [r for r in results if isinstance(r, Product)]
        except Exception as e:
            log.warning("Sitemap search failed for '%s': %s", query, e)

        return []

    async def get_category(self, category_slug: str,
                           concurrency: int = 4) -> list[Product]:
        """Fetch all products in a category page."""
        links = await self._category_product_links(category_slug)
        sem = asyncio.Semaphore(concurrency)

        async def fetch_one(product_id: str, slug: str) -> Optional[Product]:
            async with sem:
                return await self.get_product(product_id, slug)

        tasks = [fetch_one(pid, slug) for pid, slug in links]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, Product)]

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _sitemap_search(self, query: str,
                              limit: int) -> list[tuple[str, str]]:
        import httpx
        query_clean = query.lower().replace(" ", "-")
        hits = []
        async with httpx.AsyncClient(timeout=12) as client:
            for url in SITEMAP_URLS:
                if len(hits) >= limit:
                    break
                resp = await client.get(url)
                for m in re.finditer(r"/detay/([^/]+)/([^/<\s]+)", resp.text):
                    slug = m.group(2)
                    if any(w in slug for w in query_clean.split("-") if len(w) > 2):
                        hits.append((m.group(1), slug))
                        if len(hits) >= limit:
                            break
        return hits

    async def _category_product_links(self, slug: str) -> list[tuple[str, str]]:
        from playwright.async_api import async_playwright
        from .browser import stealth_page
        url = f"{BASE_URL}/kategori/{slug}"
        async with async_playwright() as p:
            browser, ctx, page = await stealth_page(p)
            try:
                await page.goto(url, wait_until="networkidle", timeout=40_000)
                links = await page.eval_on_selector_all(
                    "a[href*='/detay/']",
                    "els => [...new Set(els.map(e => e.href))]"
                )
            finally:
                await browser.close()

        result = []
        for link in links:
            m = re.search(r"/detay/([^/]+)/([^/?#]+)", link)
            if m:
                result.append((m.group(1), m.group(2)))
        return result
