"""
Demo: direct API fetch + shopping list comparison.
Run: uv run python -m market_comparison.demo
"""
import asyncio
import logging
from market_comparison.extractors.api_client import MarketApiClient
from market_comparison.comparator import ShoppingComparator
from market_comparison.scraper import MarketScraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main():
    api = MarketApiClient()

    # ── 1. Single product price lookup ────────────────────────────────────────
    print("\n=== Product: Çokkolata Bar 250gr ===")
    product = await api.get_product_prices("0002", "Çokkolata Çikolatalı Bar 250 Gr")
    if product:
        print(f"Name : {product.name}")
        print(f"Brand: {product.brand}  |  Unit: {product.prices[0].unit if product.prices else '?'}")
        for mp in product.sorted_prices():
            print(f"  {mp.market_name:<20} {mp.price:>8.2f} ₺")
        cheapest = product.cheapest()
        second = product.second_cheapest()
        if cheapest and second:
            delta = second.price - cheapest.price
            print(f"\n  Cheapest : {cheapest.market_name} — {cheapest.price:.2f} ₺")
            print(f"  2nd      : {second.market_name} — {second.price:.2f} ₺")
            print(f"  Δ saving : {delta:.2f} ₺")
    else:
        print("Product not found.")

    # ── 2. Text search ────────────────────────────────────────────────────────
    print("\n=== Search: 'sut 1 litre' ===")
    results = await api.search_with_prices("sut 1 litre", limit=3)
    for p in results:
        cheapest = p.cheapest()
        chain_count = len(p.prices)
        if cheapest:
            print(f"  {p.name:<45}  {cheapest.price:>7.2f} ₺  ({chain_count} market)")

    # ── 3. Shopping list comparison ───────────────────────────────────────────
    print("\n=== Shopping List Comparison ===")
    shopping_list = ["sut 1 litre", "yumurta 10lu", "ekmek", "tereyagi"]
    scraper = MarketScraper(use_ai_fallback=False)
    comparator = ShoppingComparator(commission_pct=30.0, scraper=scraper)

    try:
        rec = await comparator.compare(shopping_list)
        print(comparator.format_recommendation(rec))
    except ValueError as e:
        print(f"Comparison error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
