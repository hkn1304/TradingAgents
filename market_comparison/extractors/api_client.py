"""
Direct API client for api.marketfiyati.org.tr.

Discovered endpoints (no auth required):
  POST /api/v2/search              — text search, returns product list (no prices)
  POST /api/v2/searchByIdentity    — product by ID, returns per-depot prices
  GET  /api/v1/info/categories     — category tree
"""
import logging
from typing import Optional

import httpx

from ..models import Product, MarketPrice

log = logging.getLogger(__name__)

API_BASE = "https://api.marketfiyati.org.tr"
SITE_BASE = "https://marketfiyati.org.tr"

_HEADERS = {
    "Content-Type": "application/json",
    "Referer": f"{SITE_BASE}/",
    "Origin": SITE_BASE,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "tr-TR,tr;q=0.9",
}


# Default location: Istanbul city center (Taksim)
DEFAULT_LAT = 41.0370
DEFAULT_LON = 28.9850


class MarketApiClient:
    """Thin async wrapper around the public marketfiyati.org.tr API."""

    def __init__(self, timeout: float = 12.0,
                 latitude: float = DEFAULT_LAT,
                 longitude: float = DEFAULT_LON):
        self._timeout = timeout
        self.latitude = latitude
        self.longitude = longitude

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=_HEADERS, timeout=self._timeout)

    async def search(self, keywords: str, page: int = 0, size: int = 10) -> list[dict]:
        """
        Text search. Returns raw product dicts (no price data).
        Use get_product_prices() to fetch prices for each result.
        """
        async with self._client() as client:
            resp = await client.post(
                f"{API_BASE}/api/v2/search",
                json={"keywords": keywords, "pages": page, "size": size},
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("content", [])

    async def get_product_prices(self, product_id: str,
                                 keywords: str = "") -> Optional[Product]:
        """
        Fetch a product with full per-depot price data.
        Groups depots by marketAdi (chain name) → cheapest price per chain.
        """
        async with self._client() as client:
            resp = await client.post(
                f"{API_BASE}/api/v2/searchByIdentity",
                json={
                    "identity": product_id,
                    "identityType": "id",
                    "keywords": keywords,
                    "pages": 0,
                    "size": 1,
                    "latitude": self.latitude,
                    "longitude": self.longitude,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data.get("content", [])
        if not content:
            return None

        raw = content[0]
        product_url = f"{SITE_BASE}/detay/{raw['id']}/{_to_slug(raw.get('title',''))}"

        # Group depots by chain, keep cheapest price per chain
        chain_prices: dict[str, MarketPrice] = {}
        for depot in raw.get("productDepotInfoList", []):
            chain = depot.get("marketAdi") or depot.get("depotName", "Unknown")
            chain = chain.strip().title()
            price = float(depot.get("price") or 0)
            if price <= 0:
                continue
            if chain not in chain_prices or price < chain_prices[chain].price:
                chain_prices[chain] = MarketPrice(
                    market_name=chain,
                    price=price,
                    unit=raw.get("refinedVolumeOrWeight", ""),
                    url=product_url,
                    in_stock=True,
                )

        return Product(
            id=raw["id"],
            slug=_to_slug(raw.get("title", raw["id"])),
            name=raw.get("title", ""),
            brand=raw.get("brand"),
            category=raw.get("menu_category") or raw.get("main_category", ""),
            prices=list(chain_prices.values()),
        )

    async def search_with_prices(self, keywords: str,
                                  limit: int = 5,
                                  normalize: bool = True) -> list[Product]:
        """
        Combined: text search → fetch prices for top results.
        Returns up to `limit` Product objects with price data.
        """
        import asyncio
        if normalize:
            keywords = normalize_query(keywords)
        raw_results = await self.search(keywords, size=limit)
        tasks = [
            self.get_product_prices(r["id"], r.get("title", keywords))
            for r in raw_results[:limit]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        products = []
        for r in results:
            if isinstance(r, Product):
                products.append(r)
            elif isinstance(r, Exception):
                log.warning("Price fetch failed: %s", r)
        return products

    async def get_categories(self) -> list[dict]:
        async with self._client() as client:
            resp = await client.get(f"{API_BASE}/api/v1/info/categories")
            resp.raise_for_status()
            return resp.json().get("content", [])


def _to_slug(title: str) -> str:
    """Convert Turkish product title to URL slug."""
    import re
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")
    slug = title.lower().translate(tr_map)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


# Common ASCII → Turkish spelling corrections for shopping list queries
_TR_CORRECTIONS = {
    "sut": "süt", "yogurt": "yoğurt", "yumurta": "yumurta",
    "ekmek": "ekmek", "tereyagi": "tereyağı", "peynir": "peynir",
    "kirmizi": "kırmızı", "tavuk": "tavuk", "et": "et",
    "elma": "elma", "domates": "domates", "patates": "patates",
    "sogan": "soğan", "seker": "şeker", "tuz": "tuz",
    "un": "un", "makarna": "makarna", "pirinc": "pirinç",
    "kahve": "kahve", "cay": "çay", "su": "su",
    "zeytin": "zeytin", "bal": "bal", "recel": "reçel",
}


def normalize_query(query: str) -> str:
    """Convert ASCII-typed Turkish queries to proper Turkish spelling."""
    words = query.lower().split()
    corrected = [_TR_CORRECTIONS.get(w, w) for w in words]
    return " ".join(corrected)
