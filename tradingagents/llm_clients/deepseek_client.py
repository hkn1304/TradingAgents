"""DeepSeek via its Anthropic-compatible API endpoint.

DeepSeek exposes an Anthropic-SDK-compatible surface at
https://api.deepseek.com/anthropic, so we reuse NormalizedChatAnthropic
(same content normalisation, same tool-use path) with a different base URL
and the DEEPSEEK_API_KEY instead of ANTHROPIC_API_KEY.

Supported models (Anthropic-compat endpoint):
  deepseek-v4-pro[1m]  — flagship, 1M-context window
  deepseek-v4-flash    — fast / low-cost
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .anthropic_client import NormalizedChatAnthropic
from .base_client import BaseLLMClient

DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


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
        return NormalizedChatAnthropic(
            model=self.model,
            base_url=self.base_url or DEEPSEEK_ANTHROPIC_BASE_URL,
            api_key=api_key,
        )

    def validate_model(self) -> bool:
        return True  # DeepSeek models are not in Anthropic's known-model list
