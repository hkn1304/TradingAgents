"""
Deep probe: full product search response + cart/checkout endpoint discovery.
Run: uv run python -m market_comparison.probe_migros
"""
import asyncio
import json
import httpx

BASE = "https://www.migros.com.tr"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Referer": "https://www.migros.com.tr/",
    "Origin": "https://www.migros.com.tr",
    "x-device-type": "web",
}


async def hit(client, method, path, body=None, label="", full=False):
    url = BASE + path
    try:
        r = await client.request(method, url, json=body)
        status = r.status_code
        try:
            data = r.json()
            out = json.dumps(data, ensure_ascii=False, indent=2)
            snippet = out if full else out[:600]
        except Exception:
            snippet = r.text[:300]
        print(f"\n{'='*60}")
        print(f"[{label}]  {method} {status}")
        print(snippet)
        return r.status_code, data if status < 400 else None
    except Exception as e:
        print(f"\n[{label}]  ERROR: {e}")
        return 0, None


async def main():
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as c:

        # ── Full product search — get all fields ──────────────────────────
        print("\n\n### PRODUCT SEARCH — full structure ###")
        _, search_data = await hit(c, "GET",
            "/rest/products/search?q=s%C3%BCt&pageSize=2",
            label="product-search-full", full=True)

        if search_data:
            products = search_data.get("data", {}).get("storeProductInfos", [])
            if products:
                p = products[0]
                print("\n── First product fields ──")
                for k, v in p.items():
                    if not isinstance(v, (dict, list)):
                        print(f"  {k}: {v}")
                    else:
                        print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:120]}")

        # ── Cart endpoint candidates ──────────────────────────────────────
        print("\n\n### CART ENDPOINT DISCOVERY ###")
        cart_paths = [
            ("GET",  "/rest/cart-bff/cart"),
            ("GET",  "/rest/order-bff/cart"),
            ("GET",  "/rest/checkout-bff/cart"),
            ("GET",  "/rest/cart/summary"),
            ("GET",  "/rest/order/cart"),
            ("POST", "/rest/cart/add"),
            ("POST", "/rest/cart-bff/add"),
            ("POST", "/rest/cart-bff/items"),
        ]
        working_carts = []
        for method, path in cart_paths:
            status, _ = await hit(c, method, path, label=path)
            if status not in (404, 0):
                working_carts.append((method, path, status))

        # ── Location / address endpoints ──────────────────────────────────
        print("\n\n### ADDRESS / DELIVERY ENDPOINTS ###")
        addr_paths = [
            ("GET",  "/rest/delivery-bff/locations/neighborhood/search?query=Kadik%C3%B6y"),
            ("GET",  "/rest/delivery-bff/locations/search?query=Kadık%C3%B6y"),
            ("GET",  "/rest/delivery-bff/address/list"),
            ("GET",  "/rest/user-bff/addresses"),
            ("GET",  "/rest/delivery-bff/store/list"),
            ("GET",  "/rest/delivery-bff/warehouse/list"),
        ]
        for method, path in addr_paths:
            await hit(c, method, path, label=path[:50])

        # ── Auth / login endpoint ─────────────────────────────────────────
        print("\n\n### AUTH ENDPOINTS ###")
        auth_paths = [
            ("POST", "/rest/user-bff/login"),
            ("POST", "/rest/auth/login"),
            ("POST", "/rest/user-bff/token"),
            ("GET",  "/rest/user-bff/sessions"),
        ]
        for method, path in auth_paths:
            await hit(c, method, path, body={"username": "test", "password": "test"}, label=path)

        print("\n\n### SUMMARY ###")
        print("Working cart endpoints:", working_carts)


asyncio.run(main())
