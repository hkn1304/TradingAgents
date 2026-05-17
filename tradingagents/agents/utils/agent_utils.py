from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)

__all__ = [
    "get_stock_data", "get_indicators",
    "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement",
    "get_news", "get_insider_transactions", "get_global_news",
    "get_language_instruction", "build_instrument_context", "get_conclusion_template",
    "create_msg_delete",
]


_HORIZON_GUIDANCE: dict[str, tuple[str, str]] = {
    "intraday": (
        "Intraday (today)",
        "Focus on the next 4–8 hours. Use 1H/4H price action, intraday key levels, "
        "and immediate catalysts only. Targets should be reachable within the session.",
    ),
    "1day": (
        "1 Day",
        "Focus on the next 24 hours. Reference daily chart structure, overnight news "
        "catalysts, and today's key levels. Targets should be reachable within 1–2 sessions.",
    ),
    "1week": (
        "1 Week",
        "Focus on the next 5–7 trading days. Reference weekly pivots, any scheduled macro "
        "events (FOMC, CPI, earnings), and the multi-day trend.",
    ),
    "1month": (
        "1 Month",
        "Focus on the next 3–4 weeks. Reference monthly trend, key macro data releases, "
        "and medium-term support/resistance zones.",
    ),
    "longterm": (
        "Long-term (3+ months)",
        "Focus on the next 3 or more months. Reference structural fundamentals, the "
        "long-term trend, and major macro/geopolitical drivers.",
    ),
}


def get_conclusion_template(horizon: str = "1week") -> str:
    key = horizon.lower().replace(" ", "").replace("-", "")
    label, guidance = _HORIZON_GUIDANCE.get(key, _HORIZON_GUIDANCE["1week"])
    return _build_conclusion_template(label, guidance)


def _build_conclusion_template(label: str, guidance: str) -> str:
    return (
        f"\n\n---\n**Forecast Horizon: {label}** — {guidance}\n\n"
        "At the end of your report you MUST include a structured **CONCLUSION** section "
        "using EXACTLY the headings below (do not rename or skip any):\n\n"
        "## CONCLUSION\n\n"
        "### Key Highlights\n"
        "- [3–5 concise bullet points summarising the most critical findings for this horizon]\n\n"
        f"### {label} Price Outlook\n"
        "**Directional Bias:** [Bullish / Bearish / Neutral — one sentence with the primary reason]\n\n"
        "**Key Levels:** Support $X.XX · Resistance $X.XX · Pivot / Breakout trigger $X.XX\n\n"
        "### 🐂 Bull Scenario\n"
        "**Trigger:** [The specific condition or price level that would confirm the bullish case]\n"
        "**Entry Zone:** $X.XX – $X.XX\n"
        "**Target:** $X.XX\n"
        "**Invalidated if:** [price level or event that cancels this scenario]\n\n"
        "### 🐻 Bear Scenario\n"
        "**Trigger:** [The specific condition or price level that confirms the bearish case]\n"
        "**Entry Zone:** $X.XX – $X.XX\n"
        "**Target:** $X.XX\n"
        "**Invalidated if:** [price level or event that cancels this scenario]\n\n"
        "### ⚡ Geopolitical & Event Risk\n"
        "- [List SPECIFIC named events that could materially move price: active conflicts, "
        "sanctions, central bank meetings with dates, trade disputes, elections, political "
        "statements from key figures. Do NOT use generic placeholders — name the actual events "
        "you know about as of today.]\n\n"
        "### Recommended Strategy\n"
        f"- [2–3 actionable bullets calibrated to the **{label}** horizon: "
        "entry zone, stop-loss level, price target, and overall bias]\n"
        "---"
    )


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


# Canonical ticker → (human-readable description, news search terms, macro factors)
_ASSET_CONTEXT: dict[str, tuple[str, str, str]] = {
    # Metals
    "XAUUSD": (
        "Gold spot price (XAU/USD) — precious metal",
        "gold, gold price, gold futures, XAU, precious metals, safe haven, bullion",
        "USD strength/weakness, Federal Reserve interest rate policy, inflation (CPI/PPI), "
        "geopolitical tensions (wars, sanctions, trade disputes), central bank gold purchases, "
        "real yields, risk-off/risk-on sentiment",
    ),
    "XAGUSD": (
        "Silver spot price (XAG/USD) — precious and industrial metal",
        "silver, silver price, silver futures, XAG, precious metals, industrial metals, bullion",
        "USD strength/weakness, Federal Reserve interest rate policy, inflation (CPI/PPI), "
        "industrial demand (solar panels, electronics, EVs), geopolitical tensions, "
        "gold-silver ratio, mining supply disruptions",
    ),
    "XPTUSD": (
        "Platinum spot price (XPT/USD) — precious metal",
        "platinum, platinum price, platinum futures, precious metals, PGM, autocatalyst",
        "automotive sector demand (catalytic converters), hydrogen fuel cells, "
        "South African mining supply, USD strength, geopolitical tensions",
    ),
    "XPDUSD": (
        "Palladium spot price (XPD/USD) — precious metal",
        "palladium, palladium price, precious metals, PGM, autocatalyst, palladium futures",
        "automotive sector demand, Russian supply (Russia produces ~40% of world palladium), "
        "sanctions, USD strength, EV adoption impact",
    ),
    # Energy
    "USOIL": (
        "WTI Crude Oil (USD/bbl)",
        "crude oil, WTI oil, oil price, oil futures, OPEC, energy prices, petroleum, barrel",
        "OPEC+ production decisions, US inventory data (EIA weekly report), "
        "geopolitical tensions (Middle East, Russia-Ukraine), US dollar strength, "
        "global growth outlook, Iran sanctions, trade war impact on demand",
    ),
    "UKOIL": (
        "Brent Crude Oil (USD/bbl)",
        "Brent crude, oil price, OPEC, energy prices, petroleum, Brent futures",
        "OPEC+ production decisions, geopolitical tensions (Middle East, Russia), "
        "North Sea production, global demand outlook, US dollar strength",
    ),
    # Crypto
    "BTCUSD": (
        "Bitcoin (BTC/USD) — cryptocurrency",
        "Bitcoin, BTC, cryptocurrency, crypto market, blockchain, digital asset",
        "SEC/regulatory news, ETF flows, halving cycle, macro risk-on/risk-off, "
        "Fed policy and liquidity, on-chain metrics, whale activity",
    ),
    "ETHUSD": (
        "Ethereum (ETH/USD) — cryptocurrency",
        "Ethereum, ETH, cryptocurrency, DeFi, smart contracts, blockchain, Ether",
        "DeFi activity, ETH staking yields, Layer 2 adoption, SEC/regulatory news, "
        "macro risk-on/risk-off, Bitcoin correlation",
    ),
    "BNBUSD": (
        "BNB (BNB/USD) — Binance Smart Chain token",
        "BNB, Binance, cryptocurrency, crypto exchange, Binance Smart Chain",
        "Binance regulatory news, crypto exchange volumes, DeFi activity",
    ),
    # Forex
    "EURUSD": (
        "EUR/USD — Euro vs. US Dollar forex pair",
        "EUR/USD, Euro, dollar, ECB, European Central Bank, Federal Reserve, eurozone",
        "ECB rate decisions and guidance, Federal Reserve rate decisions, "
        "eurozone inflation (HICP), US CPI, GDP data, trade balance, "
        "EU political risks, US-EU trade tariffs",
    ),
    "GBPUSD": (
        "GBP/USD — British Pound vs. US Dollar forex pair",
        "GBP/USD, British pound, sterling, Bank of England, BoE",
        "Bank of England rate decisions, UK CPI/GDP, UK-EU trade relations, "
        "US Federal Reserve policy, US-UK trade relations",
    ),
    "USDJPY": (
        "USD/JPY — US Dollar vs. Japanese Yen forex pair",
        "USD/JPY, Japanese yen, Bank of Japan, BOJ, yen carry trade",
        "Bank of Japan rate policy (YCC), Federal Reserve rate decisions, "
        "Japan inflation (CPI), US Treasury yields, carry trade unwinding",
    ),
    "AUDUSD": (
        "AUD/USD — Australian Dollar vs. US Dollar forex pair",
        "AUD/USD, Australian dollar, RBA, Reserve Bank of Australia",
        "RBA rate decisions, China economic data (key AUD driver), "
        "commodity prices (iron ore, coal), US Federal Reserve policy",
    ),
    "USDCAD": (
        "USD/CAD — US Dollar vs. Canadian Dollar forex pair",
        "USD/CAD, Canadian dollar, Bank of Canada, Loonie, oil price CAD",
        "Bank of Canada rate decisions, oil prices (key CAD driver), "
        "US-Canada trade relations, US Federal Reserve policy",
    ),
}

# Conclusion template injected into every analyst system message
_CONCLUSION_TEMPLATE = (
    "\n\nAt the end of your report you MUST include a structured **CONCLUSION** section formatted exactly as follows:\n\n"
    "---\n"
    "## CONCLUSION\n\n"
    "### Key Highlights\n"
    "- [3–5 concise bullet points summarising the most critical findings]\n\n"
    "### Price Outlook\n"
    "**Short-term (today / tomorrow / this week):** [1–2 sentences with a directional bias and key price levels]\n\n"
    "**Long-term (next 2–4 weeks):** [1–2 sentences with a directional bias and key price levels]\n\n"
    "### Geopolitical & Macro Risks\n"
    "- [List any geopolitical events, central bank actions, or macro data releases that could materially move price]\n\n"
    "### Recommended Strategy\n"
    "- [2–3 actionable bullet points: entry zone, stop-loss level, target, or bias]\n"
    "---"
)


def build_instrument_context(ticker: str) -> str:
    """Return full instrument context including search term hints and macro factors."""
    canonical = ticker.upper().lstrip("$")
    info = _ASSET_CONTEXT.get(canonical)
    if info:
        desc, search_terms, macro_factors = info
        return (
            f"The instrument to analyze is `{ticker}` ({desc}). "
            "Use this exact ticker in every tool call and report. "
            f"When searching for news or sentiment, use these search terms (NOT just the ticker symbol): {search_terms}. "
            f"Always investigate these macro and geopolitical factors that directly drive this asset: {macro_factors}."
        )
    # Equity / ETF fallback
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
