"""Provider factory — creates and caches DataProvider instances from config/env."""

from __future__ import annotations

import os
from functools import lru_cache

from .base import DataProvider, ProviderError


# Registry maps provider name → import path (lazy to avoid heavy imports at startup)
_REGISTRY: dict[str, str] = {
    "yfinance": "providers.yfinance_provider.YFinanceProvider",
    "polygon":  "providers.polygon_provider.PolygonProvider",
    "oanda":    "providers.oanda_provider.OandaProvider",
    "binance":  "providers.binance_provider.BinanceProvider",
}


def _load_class(dotted_path: str):
    """Import and return a class from a dotted module path string."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class ProviderFactory:
    @staticmethod
    def create(name: str) -> DataProvider:
        """Instantiate a provider by name. Raises ProviderError on unknown name."""
        key = name.strip().lower()
        if key not in _REGISTRY:
            raise ProviderError(
                f"Unknown provider '{name}'. "
                f"Available: {', '.join(_REGISTRY)}"
            )
        cls = _load_class(_REGISTRY[key])
        return cls()

    @staticmethod
    def available() -> list[str]:
        return list(_REGISTRY)

    @staticmethod
    def capability_matrix() -> dict[str, list[str]]:
        """
        Return which capabilities each provider supports.
        Useful for the API endpoint that drives UI feature flags.
        """
        matrix: dict[str, list[str]] = {}
        for name, dotted in _REGISTRY.items():
            try:
                cls = _load_class(dotted)
                matrix[name] = [c.value for c in cls.CAPABILITIES]
            except Exception:
                matrix[name] = []
        return matrix


# ── Env-based singletons ──────────────────────────────────────────────────────
# Both are cached so the same instance (and its price cache) is reused across
# requests within a single server process.

@lru_cache(maxsize=1)
def get_market_provider() -> DataProvider:
    """
    Provider for Tab 1 (Markets): live price, OHLCV, indicators, news, pivots.
    Controlled by MARKET_DATA_PROVIDER env var. Defaults to yfinance.
    """
    name = os.getenv("MARKET_DATA_PROVIDER", "yfinance")
    return ProviderFactory.create(name)


@lru_cache(maxsize=1)
def get_pipeline_provider() -> DataProvider:
    """
    Provider for Tab 2/3 TradingAgents pipeline.
    Always yfinance for now (the pipeline's dataflows are tightly coupled to it).
    Controlled by PIPELINE_DATA_PROVIDER env var.
    """
    name = os.getenv("PIPELINE_DATA_PROVIDER", "yfinance")
    return ProviderFactory.create(name)


def reset_provider_cache() -> None:
    """Clear cached provider instances (useful in tests or after .env changes)."""
    get_market_provider.cache_clear()
    get_pipeline_provider.cache_clear()
