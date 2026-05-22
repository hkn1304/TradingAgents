"""
Layer 2: DOM Extraction (stealth Playwright).

Tries strategies in order from most stable to least stable:
  1. __NEXT_DATA__ embedded JSON (Next.js SSR hydration payload)
  2. JSON-LD / Schema.org structured data
  3. data-* attributes
  4. CSS price table pattern matching (text regex)

Returns a Product or None (triggers AI fallback).
"""
import json
import re
import logging
from typing import Optional

from playwright.async_api import async_playwright, Page

from ..models import Product, MarketPrice
from ..browser import stealth_page
from .base import BaseExtractor

log = logging.getLogger(__name__)
BASE_URL = "https://marketfiyati.org.tr"
PRICE_RE = re.compile(r"(\d{1,5}[.,]\d{2})\s*[₺TL]?")


# ── Strategy helpers ──────────────────────────────────────────────────────────

def _parse_price_str(s: str) -> float:
    """'12,99' or '12.99' → 12.99"""
    try:
        return float(str(s).replace(",", ".").replace("₺", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _deep_find_prices(obj, depth=0) -> list[dict] | None:
    """Recursively search any JSON structure for a list of market+price pairs."""
    if depth > 8:
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = {k.lower() for k in obj[0].keys()}
        price_keys = {"fiyat", "price", "kampanyafiyati", "pricetl", "normalfiyat"}
        market_keys = {"market", "marketadi", "store", "storename", "magaza"}
        if keys & price_keys:
            result = []
            for item in obj:
                market = (item.get("marketAdi") or item.get("market")
                          or item.get("store") or item.get("storeName")
                          or item.get("magaza") or "Unknown")
                price_raw = (item.get("kampanyaFiyati") or item.get("fiyat")
                             or item.get("price") or item.get("priceTL")
                             or item.get("normalFiyat") or 0)
                price = _parse_price_str(price_raw)
                result.append({
                    "market_name": str(market),
                    "price": price,
                    "unit": str(item.get("miktar") or item.get("unit") or ""),
                    "url": str(item.get("url") or item.get("link") or ""),
                    "in_stock": bool(item.get("stokDurumu", True)),
                })
            if any(r["price"] > 0 for r in result):
                return result
    if isinstance(obj, dict):
        for v in obj.values():
            found = _deep_find_prices(v, depth + 1)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj:
            found = _deep_find_prices(item, depth + 1)
            if found:
                return found
    return None


async def _try_next_data(page: Page) -> list[dict] | None:
    """Extract from Next.js __NEXT_DATA__ hydration JSON."""
    raw = await page.evaluate(
        "() => { const el = document.getElementById('__NEXT_DATA__'); "
        "return el ? el.textContent : null; }"
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    log.debug("__NEXT_DATA__ keys: %s", list(data.keys())[:10])
    return _deep_find_prices(data)


async def _try_json_ld(page: Page) -> list[dict] | None:
    """Schema.org JSON-LD."""
    scripts = await page.eval_on_selector_all(
        "script[type='application/ld+json']",
        "els => els.map(e => e.textContent)"
    )
    for raw in scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        offers = data.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        result = []
        for offer in offers:
            seller = offer.get("seller") or {}
            result.append({
                "market_name": seller.get("name", "Unknown") if isinstance(seller, dict) else str(seller),
                "price": _parse_price_str(offer.get("price", 0)),
                "unit": data.get("description", ""),
                "url": offer.get("url", ""),
                "in_stock": offer.get("availability", "InStock") != "OutOfStock",
            })
        if result:
            return result
    return None


async def _try_data_attributes(page: Page) -> list[dict] | None:
    items = await page.eval_on_selector_all(
        "[data-market][data-price]",
        "els => els.map(e => ({market_name: e.dataset.market || '', "
        "price: parseFloat(e.dataset.price)||0, unit: e.dataset.unit||'', "
        "url: e.dataset.url||'', in_stock: true}))"
    )
    return items if items else None


async def _try_css_table(page: Page) -> list[dict] | None:
    rows = await page.eval_on_selector_all(
        "table tr, [class*='market-row'], [class*='price-item'], "
        "[class*='marketRow'], [class*='priceRow']",
        "els => els.map(e => e.innerText)"
    )
    result = []
    for row_text in rows:
        m = PRICE_RE.search(row_text)
        if not m:
            continue
        price = _parse_price_str(m.group(1))
        if price <= 0:
            continue
        before = row_text[:m.start()].strip()
        market_name = before.split("\n")[0].strip() or "Unknown"
        result.append({
            "market_name": market_name,
            "price": price,
            "unit": "",
            "url": "",
            "in_stock": True,
        })
    return result if result else None


_STRATEGIES = [
    ("next_data", _try_next_data),
    ("json_ld", _try_json_ld),
    ("data_attributes", _try_data_attributes),
    ("css_table", _try_css_table),
]


# ── Extractor class ───────────────────────────────────────────────────────────

class DomExtractor(BaseExtractor):
    name = "dom"

    async def extract_product(self, product_id: str, slug: str) -> Optional[Product]:
        url = f"{BASE_URL}/detay/{product_id}/{slug}"
        async with async_playwright() as p:
            browser, context, page = await stealth_page(p)
            try:
                await page.goto(url, wait_until="networkidle", timeout=40_000)
                title = await page.title()

                price_rows = None
                used_strategy = None
                for strategy_name, strategy_fn in _STRATEGIES:
                    try:
                        price_rows = await strategy_fn(page)
                    except Exception as e:
                        log.debug("Strategy %s failed: %s", strategy_name, e)
                        price_rows = None
                    if price_rows:
                        used_strategy = strategy_name
                        break

                if not price_rows:
                    # Dump page text for debugging
                    body_text = await page.inner_text("body")
                    log.debug("Page body (first 500 chars): %s", body_text[:500])
            finally:
                await browser.close()

        if not price_rows:
            return None

        log.info("DOM strategy '%s' succeeded for %s", used_strategy, slug)
        name = title.replace("| Market Fiyatı", "").replace("- Market Fiyatı", "").strip()
        prices = [
            MarketPrice(
                market_name=r["market_name"],
                price=r["price"],
                unit=r.get("unit", ""),
                url=r.get("url") or url,
                in_stock=r.get("in_stock", True),
            )
            for r in price_rows
            if r["price"] > 0
        ]

        return Product(
            id=product_id,
            slug=slug,
            name=name,
            brand=None,
            category="",
            prices=prices,
        )

    async def extract_category(self, category_slug: str) -> list[Product]:
        url = f"{BASE_URL}/kategori/{category_slug}"
        async with async_playwright() as p:
            browser, context, page = await stealth_page(p)
            try:
                await page.goto(url, wait_until="networkidle", timeout=40_000)
                links = await page.eval_on_selector_all(
                    "a[href*='/detay/']",
                    "els => [...new Set(els.map(e => e.href))]"
                )
            finally:
                await browser.close()

        products = []
        for link in links:
            m = re.search(r"/detay/([^/]+)/([^/?#]+)", link)
            if not m:
                continue
            product = await self.extract_product(m.group(1), m.group(2))
            if product:
                product.category = category_slug
                products.append(product)
        return products
