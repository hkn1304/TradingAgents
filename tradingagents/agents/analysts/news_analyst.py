from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_conclusion_template,
    get_global_news,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config


def create_news_analyst(llm, forecast_horizon: str = "1week"):
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
            "Search strategy (perform ALL of these):\n"
            "1. Use get_news with the asset's common name and related terms listed in your context "
            "(e.g. for XAGUSD search 'silver', 'silver price', 'precious metals' — NOT just the ticker symbol). "
            "Run multiple get_news calls with different queries to be thorough.\n"
            "2. Use get_news to search for macro and geopolitical factors from your context "
            "(e.g. Federal Reserve, inflation, Iran sanctions, Trump tariffs, trade wars, OPEC, "
            "central bank policy — whatever is listed as relevant macro drivers for this asset).\n"
            "3. Use get_global_news to capture broad macroeconomic and geopolitical events from the past 7 days.\n\n"
            "Your report must cover:\n"
            "- Specific news events and how they affect the instrument's price\n"
            "- Geopolitical risks and tensions (wars, sanctions, government policy, political speeches/tweets)\n"
            "- Central bank and monetary policy developments\n"
            "- Economic data releases (CPI, GDP, employment, PMI, etc.)\n"
            "- Supply/demand dynamics specific to the asset class\n"
            "- Market sentiment and risk-on/risk-off signals\n\n"
            "Provide specific, actionable insights. Append a Markdown table summarising key news events and their price implications."
            + get_conclusion_template(forecast_horizon)
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
