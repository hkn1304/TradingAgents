from typing import Any, Optional

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

# Models that support adaptive thinking (Opus 4.6+, Sonnet 4.6+)
_ADAPTIVE_THINKING_MODELS = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
})

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens",
    "callbacks", "http_client", "http_async_client",
)


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks. This normalizes to string for consistent
    downstream handling.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def _supports_adaptive_thinking(self) -> bool:
        """Return True if the model supports adaptive thinking."""
        return any(name in self.model.lower() for name in _ADAPTIVE_THINKING_MODELS)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Wire up adaptive thinking and effort for supported models.
        # `effort` is not a direct ChatAnthropic parameter — it belongs in
        # output_config inside model_kwargs, and requires thinking to be
        # explicitly enabled.
        effort = self.kwargs.get("effort")
        if effort and self._supports_adaptive_thinking():
            llm_kwargs["thinking"] = {"type": "adaptive"}
            llm_kwargs["model_kwargs"] = {"output_config": {"effort": effort}}

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)
