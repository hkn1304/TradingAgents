"""
FastAPI server for the market price comparison demo.
Run: uv run python -m market_comparison.server
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .extractors.api_client import MarketApiClient
from .comparator import ShoppingComparator
from .templates import TemplateStore, Template, TemplateItem

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"
store = TemplateStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Market Price Comparison", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Request / Response models ─────────────────────────────────────────────────

class SearchResult(BaseModel):
    id: str
    name: str
    brand: Optional[str]
    unit: str
    category: str
    image_url: Optional[str] = None


class DepotPrice(BaseModel):
    chain: str
    price: float
    unit_price_label: str   # "396,00 ₺/Kg"
    unit_price_value: float


class ProductPrices(BaseModel):
    id: str
    name: str
    brand: Optional[str]
    unit: str
    prices: list[DepotPrice]


class CompareRequest(BaseModel):
    product_ids: list[str]
    latitude: float = 41.0370
    longitude: float = 28.9850
    commission_pct: float = 30.0


class ChainTotal(BaseModel):
    chain: str
    total: float
    items: dict[str, float]
    missing: list[str]


class CompareResponse(BaseModel):
    chains: list[ChainTotal]
    cheapest: str
    second_cheapest: Optional[str]
    savings: float
    commission: float
    user_net_saving: float


class TemplateItemIn(BaseModel):
    product_id: str
    name: str
    brand: Optional[str] = None
    unit: str = ""
    category: str = ""


class TemplateSaveRequest(BaseModel):
    name: str
    items: list[TemplateItemIn]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/search", response_model=list[SearchResult])
async def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(8, ge=1, le=20),
    lat: float = Query(41.0370),
    lon: float = Query(28.9850),
):
    api = MarketApiClient(latitude=lat, longitude=lon)
    try:
        raw = await api.search(q, size=limit)
    except Exception as e:
        raise HTTPException(502, f"Search failed: {e}")

    return [
        SearchResult(
            id=r["id"],
            name=r.get("title", ""),
            brand=r.get("brand"),
            unit=r.get("refinedVolumeOrWeight", ""),
            category=r.get("menu_category") or r.get("main_category", ""),
            image_url=r.get("imageUrl"),
        )
        for r in raw
    ]


@app.get("/api/product/{product_id}/prices", response_model=ProductPrices)
async def product_prices(
    product_id: str,
    name: str = Query(""),
    lat: float = Query(41.0370),
    lon: float = Query(28.9850),
):
    api = MarketApiClient(latitude=lat, longitude=lon)
    try:
        raw_resp = await api._client().post(
            "https://api.marketfiyati.org.tr/api/v2/searchByIdentity",
            json={
                "identity": product_id,
                "identityType": "id",
                "keywords": name,
                "pages": 0,
                "size": 1,
                "latitude": lat,
                "longitude": lon,
            },
        )
        data = raw_resp.json()
    except Exception as e:
        raise HTTPException(502, f"Price fetch failed: {e}")

    content = data.get("content", [])
    if not content:
        raise HTTPException(404, "Product not found")

    raw = content[0]
    chain_map: dict[str, DepotPrice] = {}
    for depot in raw.get("productDepotInfoList", []):
        chain = (depot.get("marketAdi") or "unknown").strip()
        price = float(depot.get("price") or 0)
        if price <= 0:
            continue
        if chain not in chain_map or price < chain_map[chain].price:
            chain_map[chain] = DepotPrice(
                chain=chain,
                price=price,
                unit_price_label=depot.get("unitPrice", ""),
                unit_price_value=float(depot.get("unitPriceValue") or 0),
            )

    return ProductPrices(
        id=raw["id"],
        name=raw.get("title", ""),
        brand=raw.get("brand"),
        unit=raw.get("refinedVolumeOrWeight", ""),
        prices=sorted(chain_map.values(), key=lambda p: p.price),
    )


@app.post("/api/compare", response_model=CompareResponse)
async def compare(body: CompareRequest):
    api = MarketApiClient(latitude=body.latitude, longitude=body.longitude)

    import httpx
    async with httpx.AsyncClient(
        headers={
            "Content-Type": "application/json",
            "Referer": "https://marketfiyati.org.tr/",
            "Origin": "https://marketfiyati.org.tr",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=12,
    ) as client:
        tasks = [
            client.post(
                "https://api.marketfiyati.org.tr/api/v2/searchByIdentity",
                json={
                    "identity": pid,
                    "identityType": "id",
                    "keywords": "",
                    "pages": 0,
                    "size": 1,
                    "latitude": body.latitude,
                    "longitude": body.longitude,
                },
            )
            for pid in body.product_ids
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Build per-chain totals
    market_totals: dict[str, dict] = {}
    product_names: dict[str, str] = {}

    for pid, resp in zip(body.product_ids, responses):
        if isinstance(resp, Exception):
            continue
        data = resp.json()
        content = data.get("content", [])
        if not content:
            continue
        raw = content[0]
        pname = raw.get("title", pid)
        product_names[pid] = pname

        for depot in raw.get("productDepotInfoList", []):
            chain = (depot.get("marketAdi") or "unknown").strip()
            price = float(depot.get("price") or 0)
            if price <= 0:
                continue
            if chain not in market_totals:
                market_totals[chain] = {"total": 0.0, "breakdown": {}, "missing": []}
            if pname not in market_totals[chain]["breakdown"] or \
               price < market_totals[chain]["breakdown"][pname]:
                old = market_totals[chain]["breakdown"].get(pname, 0)
                market_totals[chain]["total"] += price - old
                market_totals[chain]["breakdown"][pname] = price

    all_names = list(product_names.values())
    for chain, data in market_totals.items():
        data["missing"] = [n for n in all_names if n not in data["breakdown"]]

    if not market_totals:
        raise HTTPException(422, "No price data found for any product near this location.")

    sorted_chains = sorted(
        market_totals.items(),
        key=lambda x: (len(x[1]["missing"]), x[1]["total"])
    )

    chains_out = [
        ChainTotal(
            chain=name,
            total=round(data["total"], 2),
            items=data["breakdown"],
            missing=data["missing"],
        )
        for name, data in sorted_chains
    ]

    cheapest = chains_out[0]
    second = chains_out[1] if len(chains_out) > 1 else None
    savings = round((second.total - cheapest.total) if second else 0.0, 2)
    commission = round(savings * body.commission_pct / 100, 2)

    return CompareResponse(
        chains=chains_out,
        cheapest=cheapest.chain,
        second_cheapest=second.chain if second else None,
        savings=savings,
        commission=commission,
        user_net_saving=round(savings - commission, 2),
    )


# ── Template endpoints ────────────────────────────────────────────────────────

@app.get("/api/templates")
async def list_templates():
    return {"templates": store.list_templates()}


@app.get("/api/templates/{name}")
async def get_template(name: str):
    tmpl = store.get(name)
    if not tmpl:
        raise HTTPException(404, "Template not found")
    return tmpl


@app.post("/api/templates")
async def save_template(body: TemplateSaveRequest):
    tmpl = Template(
        name=body.name,
        items=[TemplateItem(**i.model_dump()) for i in body.items],
    )
    store.save(tmpl)
    return {"saved": body.name}


@app.delete("/api/templates/{name}")
async def delete_template(name: str):
    if not store.delete(name):
        raise HTTPException(404, "Template not found")
    return {"deleted": name}


@app.post("/api/templates/{name}/items")
async def add_template_item(name: str, item: TemplateItemIn):
    store.add_item(name, TemplateItem(**item.model_dump()))
    return {"added": item.product_id}


@app.delete("/api/templates/{name}/items/{product_id}")
async def remove_template_item(name: str, product_id: str):
    store.remove_item(name, product_id)
    return {"removed": product_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("market_comparison.server:app", host="0.0.0.0", port=8001, reload=True)
