from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_conclusion_template,
    get_indicators,
    get_language_instruction,
    get_stock_data,
)
from tradingagents.dataflows.config import get_config


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_stock_data,
            get_indicators,
        ]

        system_message = (
            "Your task is to immediately perform a complete technical analysis of the instrument "
            "specified in the context as of the current date. "
            "Do NOT ask any clarifying questions — begin with tool calls right away. "
            "Step 1: call get_stock_data to fetch recent price history. "
            "Step 2: call get_indicators for up to 8 complementary indicators chosen from the list below. "
            "Step 3: write your full analysis report.\n\n"
            "Available indicators:\n"
            "Moving Averages:\n"
            "- close_50_sma: 50-day SMA — medium-term trend and dynamic support/resistance.\n"
            "- close_200_sma: 200-day SMA — long-term trend benchmark; golden/death cross signals.\n"
            "- close_10_ema: 10-day EMA — fast short-term momentum; sensitive to recent price action.\n"
            "MACD:\n"
            "- macd: MACD line — momentum via EMA differences; look for crossovers.\n"
            "- macds: MACD Signal — EMA of MACD; crossovers trigger entries.\n"
            "- macdh: MACD Histogram — gap between MACD and signal; spot divergence.\n"
            "Momentum:\n"
            "- rsi: RSI — overbought (>70) / oversold (<30); divergence signals reversals.\n"
            "Volatility:\n"
            "- boll: Bollinger Middle — 20-day SMA baseline.\n"
            "- boll_ub: Bollinger Upper — potential overbought / breakout zone.\n"
            "- boll_lb: Bollinger Lower — potential oversold zone.\n"
            "- atr: ATR — volatility measure for stop-loss sizing.\n"
            "Volume:\n"
            "- vwma: VWMA — volume-weighted moving average; confirms trend strength.\n\n"
            "Select indicators that complement each other (avoid redundancy). "
            "Use the exact indicator names above in tool calls. "
            "Write a detailed nuanced technical analysis. Include specific price levels, "
            "trend direction, momentum strength, support/resistance zones, and actionable entry/exit signals. "
            "Append a Markdown table summarising key indicator readings."
            + get_conclusion_template()
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
