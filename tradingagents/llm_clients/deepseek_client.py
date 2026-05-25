"""DeepSeek via its Anthropic-compatible API endpoint.

DeepSeek exposes an Anthropic-SDK-compatible surface at
https://api.deepseek.com/anthropic, so we reuse ChatAnthropic
(same content normalisation, same tool-use path) with a different base URL
and the DEEPSEEK_API_KEY instead of ANTHROPIC_API_KEY.

DeepSeek V4 models (deepseek-v4-flash, deepseek-v4-pro) are thinking models
that return thinking blocks by default. Those blocks must be echoed back in
subsequent messages or the API returns HTTP 400. DeepSeekChatAnthropic
handles this round-trip transparently.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from langchain_core.messages import AIMessage

from .anthropic_client import NormalizedChatAnthropic, _anthropic_invoke_with_retry
from .base_client import BaseLLMClient, normalize_content

DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


class DeepSeekChatAnthropic(NormalizedChatAnthropic):
    """ChatAnthropic subclass that handles DeepSeek V4 thinking-block round-trips.

    DeepSeek V4 models return thinking blocks in responses. These must be
    included verbatim in the next assistant message or the API rejects the
    request. We capture them before normalize_content strips them, store
    them in additional_kwargs, and re-inject them into outgoing payloads.
    """

    def invoke(self, input, config=None, **kwargs):
        from langchain_anthropic import ChatAnthropic

        def _call():
            raw = ChatAnthropic.invoke(self, input, config, **kwargs)
            if isinstance(raw.content, list):
                thinking = [
                    b for b in raw.content
                    if isinstance(b, dict) and b.get("type") == "thinking"
                ]
                if thinking:
                    raw.additional_kwargs["thinking_blocks"] = thinking
            return normalize_content(raw)

        return _anthropic_invoke_with_retry(_call)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            messages = self._convert_input(input_).to_messages()
        except Exception:
            return payload
        formatted = payload.get("messages", [])
        for msg, fmt in zip(messages, formatted):
            if not isinstance(msg, AIMessage):
                continue
            thinking = msg.additional_kwargs.get("thinking_blocks", [])
            if not thinking:
                continue
            content = fmt.get("content", "")
            if isinstance(content, str):
                fmt["content"] = thinking + [{"type": "text", "text": content}]
            elif isinstance(content, list):
                already = any(
                    isinstance(b, dict) and b.get("type") == "thinking"
                    for b in content
                )
                if not already:
                    fmt["content"] = thinking + content
        return payload


class DeepSeekAnthropicClient(BaseLLMClient):
    """DeepSeek client using the Anthropic-compatible endpoint."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        api_key = os.getenv("DEEPSEEK_API_KEY") or self.kwargs.get("api_key", "")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY is not set. "
                "Add it to your .env file: DEEPSEEK_API_KEY=sk-..."
            )
        return DeepSeekChatAnthropic(
            model=self.model,
            base_url=self.base_url or DEEPSEEK_ANTHROPIC_BASE_URL,
            api_key=api_key,
        )

    def validate_model(self) -> bool:
        return True  # DeepSeek models are not in Anthropic's known-model list
