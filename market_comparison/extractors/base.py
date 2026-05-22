"""Abstract base for all extraction layers."""
from abc import ABC, abstractmethod
from ..models import Product


class BaseExtractor(ABC):
    name: str = "base"

    @abstractmethod
    async def extract_product(self, product_id: str, slug: str) -> Product | None:
        """Return a Product with prices populated, or None if extraction fails."""
        ...

    @abstractmethod
    async def extract_category(self, category_slug: str) -> list[Product]:
        """Return all products in a category."""
        ...
