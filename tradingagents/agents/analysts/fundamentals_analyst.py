from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_conclusion_template,
    get_fundamentals,
    get_horizon_instruction,
    get_income_statement,
    get_insider_transactions,
    get_language_instruction,
)
from tradingagents.dataflows.config import get_config


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]

        system_message = (
            "Your task is to immediately perform a comprehensive fundamental analysis of the instrument "
            "specified in the context as of the current date. "
            "Do NOT ask any clarifying questions -- begin with tool calls right away.\n\n"
            "For equities and ETFs: call get_fundamentals, get_balance_sheet, get_cashflow, and "
            "get_income_statement to build a complete financial picture.\n\n"
            "For non-equity instruments (metals, commodities, crypto, forex): "
            "financial statements will be unavailable or empty -- this is expected. "
            "Instead focus your analysis on: supply/demand dynamics, production data, "
            "institutional positioning (COT report insights), ETF/fund flows, "
            "historical price-to-fundamentals relationships, and any available market-structure data. "
            "Call get_fundamentals anyway to see what data is available.\n\n"
            "Your report must include as much detail as possible: valuation ratios, "
            "profitability, debt levels, cash flow strength, and forward outlook for equities; "
            "or supply/demand balance, major producers/consumers, and structural price drivers for non-equity assets. "
            "Append a Markdown table summarising key fundamental metrics."
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
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
