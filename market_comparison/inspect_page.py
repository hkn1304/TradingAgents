"""Show unit-price normalized comparison for a query."""
import asyncio
import httpx

API_BASE = "https://api.marketfiyati.org.tr"
HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://marketfiyati.org.tr/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://marketfiyati.org.tr",
}
LAT, LON = 41.0370, 28.9850


async def show_unit_prices(query: str, limit: int = 8):
    print(f"\n{'='*70}")
    print(f"Query: '{query}'  (unit-price normalized per depot)")
    print(f"{'='*70}")

    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        resp = await client.post(f"{API_BASE}/api/v2/search",
                                 json={"keywords": query, "pages": 0, "size": limit})
        results = resp.json().get("content", [])

        rows = []
        for r in results:
            resp2 = await client.post(f"{API_BASE}/api/v2/searchByIdentity", json={
                "identity": r["id"], "identityType": "id",
                "keywords": r.get("title", ""),
                "pages": 0, "size": 1,
                "latitude": LAT, "longitude": LON,
            })
            if resp2.status_code != 200:
                continue
            content = resp2.json().get("content", [{}])
            if not content:
                continue
            prod = content[0]
            for depot in prod.get("productDepotInfoList", []):
                rows.append({
                    "product": prod.get("title", "?"),
                    "weight": prod.get("refinedVolumeOrWeight", "?"),
                    "chain": depot.get("marketAdi", "?"),
                    "price": depot.get("price", 0),
                    "unit_price": depot.get("unitPriceValue", 0),
                    "unit_label": depot.get("unitPrice", ""),
                })

        # Sort by unit price
        rows.sort(key=lambda r: r["unit_price"] or r["price"])
        print(f"{'Chain':<12} {'Price':>8}  {'Unit Price':>16}  Product")
        print("-" * 70)
        for r in rows:
            up = f"{r['unit_price']:.2f} ₺/kg" if r["unit_price"] else r["unit_label"]
            print(f"{r['chain']:<12} {r['price']:>7.2f}₺  {up:>16}  {r['product'][:35]}")


async def main():
    for q in ["yumurta", "süt 1 lt", "ekmek", "tereyağı"]:
        await show_unit_prices(q)


asyncio.run(main())
