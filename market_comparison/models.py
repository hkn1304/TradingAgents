from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MarketPrice:
    market_name: str       # e.g. "Migros", "Carrefour"
    price: float           # TRY
    unit: str              # e.g. "250 gr", "1 kg", "1 adet"
    url: str               # direct link to product on market site
    in_stock: bool = True
    scraped_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Product:
    id: str                          # hex id from marketfiyati, e.g. "0002"
    slug: str                        # e.g. "cokkolata-cikolatali-bar-250-gr"
    name: str
    brand: Optional[str]
    category: str
    prices: list[MarketPrice] = field(default_factory=list)

    @property
    def page_url(self) -> str:
        return f"https://marketfiyati.org.tr/detay/{self.id}/{self.slug}"

    def sorted_prices(self) -> list[MarketPrice]:
        return sorted([p for p in self.prices if p.in_stock], key=lambda p: p.price)

    def cheapest(self) -> Optional[MarketPrice]:
        s = self.sorted_prices()
        return s[0] if s else None

    def second_cheapest(self) -> Optional[MarketPrice]:
        s = self.sorted_prices()
        return s[1] if len(s) > 1 else None


@dataclass
class ShoppingListItem:
    query: str              # user-typed query, e.g. "süt 1L"
    product: Optional[Product] = None
    chosen_market: Optional[str] = None  # override: force a specific market


@dataclass
class CartComparison:
    """Total cost breakdown per market for a full shopping list."""
    market_name: str
    total_price: float
    item_breakdown: dict[str, float]  # item query -> price
    missing_items: list[str]          # items not available at this market

    @property
    def item_count(self) -> int:
        return len(self.item_breakdown)


@dataclass
class ShoppingRecommendation:
    cheapest: CartComparison
    second_cheapest: Optional[CartComparison]
    savings: float          # cheapest vs second_cheapest total
    commission: float       # your fee (configurable %)
    user_net_saving: float  # savings - commission
