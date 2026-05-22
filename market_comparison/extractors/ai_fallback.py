"""
Layer 3: AI Fallback Extractor.

When DOM strategies all fail (e.g. after a major site redesign), render the page
with Playwright, capture the inner text, and send it to Claude to extract
structured price data. This layer never fully breaks, only slows down.
"""
import os
import json
from typing import Optional

from anthropic import AsyncAnthropic
from playwright.async_api import async_playwright

from ..models import Product, MarketPrice
from .base import BaseExtractor

BASE_URL = "https://marketfiyati.org.tr"
MODEL = "claude-haiku-4-5-20251001"   # fast + cheap for extraction tasks

EXTRACTION_PROMPT = """You are a data extraction assistant. Below is the visible text of a Turkish price-comparison webpage for a single grocery product.

Extract ALL market/supermarket prices listed. Return ONLY a JSON object in this exact format, no prose:

{{
  "product_name": "<Turkish product name>",
  "brand": "<brand or null>",
  "unit": "<e.g. 250 gr, 1 kg, 1 adet>",
  "prices": [
    {{"market": "<market name>", "price": <float TRY>, "in_stock": <true|false>, "url": "<direct URL or empty string>"}},
    ...
  ]
}}

Page text:
---
{page_text}
---
"""


class AiFallbackExtractor(BaseExtractor):
    name = "ai_fallback"

    def __init__(self):
        self._client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    async def _get_page_text(self, url: str) -> str:
        from ..browser import stealth_page
        async with async_playwright() as p:
            browser, context, page = await stealth_page(p)
            try:
                await page.goto(url, wait_until="networkidle", timeout=40_000)
                text = await page.inner_text("body")
            finally:
                await browser.close()
        return text[:12_000]  # cap to ~3k tokens

    async def extract_product(self, product_id: str, slug: str) -> Optional[Product]:
        url = f"{BASE_URL}/detay/{product_id}/{slug}"
        page_text = await self._get_page_text(url)

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(page_text=page_text)
            }]
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        prices = [
            MarketPrice(
                market_name=p["market"],
                price=float(p["price"]),
                unit=data.get("unit", ""),
                url=p.get("url", url),
                in_stock=p.get("in_stock", True),
            )
            for p in data.get("prices", [])
            if p.get("price", 0) > 0
        ]

        return Product(
            id=product_id,
            slug=slug,
            name=data.get("product_name", slug),
            brand=data.get("brand"),
            category="",
            prices=prices,
        )

    async def extract_category(self, category_slug: str) -> list[Product]:
        # For category listing we still use Playwright DOM (link hrefs are stable)
        from .dom_extractor import DomExtractor
        dom = DomExtractor()
        # Get product links via DOM, then extract each with AI
        url = f"{BASE_URL}/kategori/{category_slug}"
        import re
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            links = await page.eval_on_selector_all(
                "a[href*='/detay/']",
                "els => [...new Set(els.map(e => e.href))]"
            )
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
