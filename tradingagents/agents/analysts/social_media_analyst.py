from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import build_instrument_context, get_conclusion_template, get_language_instruction, get_news
from tradingagents.dataflows.config import get_config


def create_social_media_analyst(llm):
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_news,
        ]

        system_message = (
            "Your task is to immediately analyse public sentiment, social media discussions, and recent news "
            "for the instrument specified in the context as of the current date. "
            "Do NOT ask any clarifying questions — begin with tool calls right away.\n\n"
            "Search strategy (use the search terms listed in your context, NOT just the raw ticker symbol):\n"
            "1. Search for public sentiment and social discussions using the asset's common name "
            "(e.g. for XAGUSD use 'silver', 'silver price', 'silver forecast' — investors and traders "
            "discuss commodities/crypto/forex by name, not by ticker code).\n"
            "2. Search for recent news events driving sentiment: use the asset's common name "
            "AND relevant macro/geopolitical keywords from your context "
            "(e.g. 'Iran US tensions silver', 'Federal Reserve silver', 'Trump tariffs gold', etc.).\n"
            "3. Include influential analyst opinions, social media trends, and market participant positioning.\n\n"
            "Your report must cover:\n"
            "- Overall sentiment (bullish / bearish / neutral) with evidence\n"
            "- Key narratives and talking points driving market participants\n"
            "- Geopolitical and macro events being discussed (conflicts, sanctions, speeches, tweets)\n"
            "- Retail vs. institutional sentiment divergence if observable\n"
            "- Recent news that is shifting market opinion\n\n"
            "Provide specific, actionable insights. Append a Markdown table summarising sentiment signals and their implications."
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
            "sentiment_report": report,
        }

    return social_media_analyst_node
