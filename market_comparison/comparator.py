"""
Shopping list price comparator.

For each query, searches up to N product variants and takes the cheapest
available price per market across all variants. This handles cases where
a product is sold under different brands at different chains.
"""
import asyncio
import logging
from typing import Optional

from .models import (
    MarketPrice, Product, ShoppingListItem, CartComparison, ShoppingRecommendation
)
from .scraper import MarketScraper
from .extractors.api_client import MarketApiClient
from .templates import Template, TemplateItem, TemplateStore

log = logging.getLogger(__name__)


class ShoppingComparator:
    def __init__(self, commission_pct: float = 30.0,
                 scraper: Optional[MarketScraper] = None,
                 variants_per_item: int = 5):
        """
        commission_pct:    your take as % of (2nd_lowest - lowest) price delta.
        variants_per_item: how many product variants to search per query
                           (more = better coverage across chains, slower).
        """
        self.commission_pct = commission_pct
        self.variants_per_item = variants_per_item
        self._scraper = scraper or MarketScraper()

    async def compare(self, items: list[str]) -> ShoppingRecommendation:
        """
        items: list of search queries, e.g. ["süt 1L", "ekmek", "yumurta 10lu"]
        """
        tasks = [self._scraper.search_product(q, limit=self.variants_per_item)
                 for q in items]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # For each query: collect cheapest price per market across all variants
        # market_totals[market_name] = {total, breakdown {query: price}, missing}
        market_totals: dict[str, dict] = {}
        found_queries: set[str] = set()

        for query, result in zip(items, all_results):
            if isinstance(result, Exception) or not result:
                log.warning("No products found for: %s", query)
                continue

            # Collect cheapest price per chain across all variants
            chain_best: dict[str, float] = {}  # market_name -> cheapest price
            for product in result:
                for mp in product.prices:
                    if not mp.in_stock or mp.price <= 0:
                        continue
                    mname = mp.market_name
                    if mname not in chain_best or mp.price < chain_best[mname]:
                        chain_best[mname] = mp.price

            if not chain_best:
                log.warning("Products found but no price data for: %s", query)
                continue

            found_queries.add(query)
            for market_name, price in chain_best.items():
                if market_name not in market_totals:
                    market_totals[market_name] = {
                        "total": 0.0, "breakdown": {}, "missing": [],
                    }
                market_totals[market_name]["total"] += price
                market_totals[market_name]["breakdown"][query] = price

        if not market_totals:
            raise ValueError("No market price data could be fetched for any item.")

        # Mark missing items per market
        for query in items:
            for market_name, data in market_totals.items():
                if query not in data["breakdown"]:
                    data["missing"].append(query)

        # Sort markets by total, fewest missing items first (stable sort)
        sorted_markets = sorted(
            market_totals.items(),
            key=lambda x: (len(x[1]["missing"]), x[1]["total"])
        )

        carts = [
            CartComparison(
                market_name=name,
                total_price=data["total"],
                item_breakdown=data["breakdown"],
                missing_items=data["missing"],
            )
            for name, data in sorted_markets
        ]

        cheapest_cart = carts[0]
        second_cart = carts[1] if len(carts) > 1 else None

        savings = (second_cart.total_price - cheapest_cart.total_price
                   if second_cart else 0.0)
        commission = savings * (self.commission_pct / 100)
        user_net = savings - commission

        return ShoppingRecommendation(
            cheapest=cheapest_cart,
            second_cheapest=second_cart,
            savings=savings,
            commission=commission,
            user_net_saving=user_net,
        )

    async def compare_template(self, template: Template,
                               latitude: float = 41.0370,
                               longitude: float = 28.9850) -> ShoppingRecommendation:
        """
        Mode 4: compare exact products from a saved template across all chains.
        Each item is fetched by its specific product ID — no keyword guessing.
        """
        api = MarketApiClient(latitude=latitude, longitude=longitude)

        tasks = [
            api.get_product_prices(item.product_id, item.name)
            for item in template.items
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        market_totals: dict[str, dict] = {}

        for tmpl_item, result in zip(template.items, results):
            if isinstance(result, Exception) or not result:
                log.warning("No price data for product %s (%s)",
                            tmpl_item.product_id, tmpl_item.name)
                continue

            product: Product = result
            for mp in product.prices:
                if not mp.in_stock or mp.price <= 0:
                    continue
                mname = mp.market_name
                if mname not in market_totals:
                    market_totals[mname] = {"total": 0.0, "breakdown": {}, "missing": []}
                market_totals[mname]["total"] += mp.price
                market_totals[mname]["breakdown"][tmpl_item.name] = mp.price

        if not market_totals:
            raise ValueError("No market data found for any template item.")

        for tmpl_item in template.items:
            for mname, data in market_totals.items():
                if tmpl_item.name not in data["breakdown"]:
                    data["missing"].append(tmpl_item.name)

        sorted_markets = sorted(
            market_totals.items(),
            key=lambda x: (len(x[1]["missing"]), x[1]["total"])
        )
        carts = [
            CartComparison(
                market_name=name,
                total_price=data["total"],
                item_breakdown=data["breakdown"],
                missing_items=data["missing"],
            )
            for name, data in sorted_markets
        ]

        cheapest = carts[0]
        second = carts[1] if len(carts) > 1 else None
        savings = (second.total_price - cheapest.total_price) if second else 0.0
        commission = savings * (self.commission_pct / 100)

        return ShoppingRecommendation(
            cheapest=cheapest,
            second_cheapest=second,
            savings=savings,
            commission=commission,
            user_net_saving=savings - commission,
        )

    def format_recommendation(self, rec: ShoppingRecommendation) -> str:
        lines = [
            f"Önerilen Market  : {rec.cheapest.market_name}",
            f"Toplam Fiyat     : {rec.cheapest.total_price:.2f} ₺",
            f"Bulunan Ürünler  : {rec.cheapest.item_count} / {rec.cheapest.item_count + len(rec.cheapest.missing_items)}",
        ]
        if rec.second_cheapest:
            lines += [
                "",
                f"2. En Ucuz       : {rec.second_cheapest.market_name} "
                f"({rec.second_cheapest.total_price:.2f} ₺)",
                f"Tasarruf         : {rec.savings:.2f} ₺",
                f"Komisyon (%{self.commission_pct:.0f})   : {rec.commission:.2f} ₺",
                f"Net Kazancınız   : {rec.user_net_saving:.2f} ₺",
            ]
        lines += ["", "Ürün Dökümü (Önerilen Market):"]
        for item, price in rec.cheapest.item_breakdown.items():
            lines.append(f"  {item:<35} {price:>8.2f} ₺")
        if rec.cheapest.missing_items:
            lines += ["", "Bu markette bulunamayan ürünler:"]
            for m in rec.cheapest.missing_items:
                lines.append(f"  - {m}")
        return "\n".join(lines)
