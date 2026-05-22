from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_conclusion_template,
    get_global_news,
    get_horizon_instruction,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_news,
            get_global_news,
        ]

        system_message = (
            "Your task is to immediately research and write a comprehensive news report for the instrument "
            "specified in the context as of the current date. "
            "Do NOT ask any clarifying questions — begin with tool calls right away.\n\n"
            "Search strategy (perform ALL of these — minimum 4 searches):\n"
            "1. Use get_news with the asset's common name and related terms listed in your context "
            "(e.g. for XAGUSD search 'silver', 'silver price', 'precious metals' — NOT just the ticker symbol). "
            "Run 2-3 different get_news calls with different queries.\n"
            "2. Use get_news to search for active geopolitical conflicts and political events: "
            "'Iran US war sanctions', 'Trump tariffs trade war', 'Russia Ukraine war', "
            "'Middle East conflict', 'sanctions embargo' — always search at least one geopolitical term "
            "that is relevant to your asset's macro drivers.\n"
            "3. Use get_news for macro drivers from your context (Federal Reserve, central bank, inflation CPI, "
            "OPEC production, commodities demand, or whatever is relevant for this specific asset).\n"
            "4. Use get_global_news to capture broad geopolitical and macroeconomic events from the past 7 days.\n\n"
            "Your report MUST cover all of these sections:\n"
            "## Geopolitical Events & Risks (MANDATORY — never skip this section)\n"
            "  List ALL active wars, conflicts, sanctions, political tensions, and government policy changes "
            "  that affect this asset. Include Trump speeches/tweets, Iran-US tensions, Russia-Ukraine, "
            "  trade disputes, tariffs, etc. State clearly if a conflict is escalating or de-escalating "
            "  and its directional impact on the asset's price.\n"
            "## Asset-Specific News\n"
            "  News directly about this asset (price moves, supply/demand, ETF flows, etc.)\n"
            "## Central Bank & Monetary Policy\n"
            "  Rate decisions, forward guidance, inflation data, liquidity conditions.\n"
            "## Economic Data\n"
            "  CPI, GDP, employment, PMI and other releases from the past week.\n"
            "## Market Sentiment\n"
            "  Risk-on/risk-off signals, USD strength, safe-haven flows.\n\n"
            "End with a Markdown table summarising key news events, their geopolitical/macro driver, "
            "and directional price implication (bullish/bearish/neutral)."
            + get_horizon_instruction()
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
            "news_report": report,
        }

    return news_analyst_node
