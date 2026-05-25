"""
Narrow Q&A chat handler for Tab 3.

The assistant answers questions ONLY from the completed analysis report
stored in SQLite — no fresh tool calls, no external data fetches.
Keeps token cost low and responses grounded.
"""

from __future__ import annotations

import logging
import os

import anthropic

from web.database import chat_append, chat_get_history, report_get_full_text

logger = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """\
You are a concise trading research assistant.
Answer questions ONLY based on the analysis report provided below.
If the answer is not in the report, say so clearly rather than guessing.
Keep responses focused — use bullet points for lists, plain prose otherwise.
Do not repeat the question back. Do not add disclaimers about seeking financial advice.

## Analysis Report
{report}
"""

_FALLBACK_MESSAGE = (
    "The analysis report for this session is not yet available. "
    "Please wait for the analysis to complete before asking questions."
)


async def chat(session_id: str, user_message: str) -> str:
    """
    Append user message to history, call Claude with full report as context,
    persist the assistant reply, and return it.
    """
    report_text = report_get_full_text(session_id)
    if not report_text.strip():
        chat_append(session_id, "user", user_message)
        chat_append(session_id, "assistant", _FALLBACK_MESSAGE)
        return _FALLBACK_MESSAGE

    history = chat_get_history(session_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_message})

    system_prompt = _SYSTEM_TEMPLATE.format(report=report_text)

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model   = "claude-haiku-4-5-20251001",
            max_tokens = 1024,
            system  = system_prompt,
            messages = messages,
        )
        reply = response.content[0].text.strip()
    except Exception as exc:
        logger.exception("Chat LLM call failed for session %s", session_id)
        reply = f"Sorry, I encountered an error: {exc}"

    chat_append(session_id, "user",      user_message)
    chat_append(session_id, "assistant", reply)
    return reply
