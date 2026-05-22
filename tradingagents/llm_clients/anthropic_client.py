import logging
import time
from typing import Any, Optional

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

logger = logging.getLogger(__name__)

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens",
    "callbacks", "http_client", "http_async_client", "effort",
)

# (delay_seconds, label) per retry attempt for transient Anthropic errors.
_OVERLOAD_DELAYS = (5, 15, 30)   # 529 Overloaded
_RATELIMIT_DELAYS = (10, 30, 60) # 429 RateLimitError


def _anthropic_invoke_with_retry(invoke_fn):
    """Call invoke_fn(), retrying on 429 RateLimitError and 529/5xx server errors.

    This SDK version has no OverloadedError — 529 surfaces as InternalServerError
    with status_code=529.  We catch both RateLimitError and InternalServerError
    (checking status code) so the logic is forward-compatible.
    """
    from anthropic import RateLimitError, InternalServerError, APIStatusError

    last_exc = None
    max_attempts = max(len(_OVERLOAD_DELAYS), len(_RATELIMIT_DELAYS)) + 1

    for attempt in range(max_attempts):
        try:
            return invoke_fn()
        except RateLimitError as exc:
            last_exc = exc
            delays, label = _RATELIMIT_DELAYS, "429 RateLimited"
        except (InternalServerError, APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            if status and status not in (500, 503, 529):
                raise
            last_exc = exc
            delays, label = _OVERLOAD_DELAYS, f"{status or '5xx'} ServerError"

        if attempt >= len(delays):
            break
        delay = delays[attempt]
        logger.warning(
            "Anthropic %s (attempt %d/%d) — retrying in %ds",
            label, attempt + 1, len(delays), delay,
        )
        time.sleep(delay)

    raise last_exc


class NormalizedChatAnthropic(ChatAnthropic):
    """ChatAnthropic with normalized content output and transient-error retry.

    Claude models with extended thinking or tool use return content as a
    list of typed blocks; invoke() normalizes to string.

    HTTP 529 Overloaded and 429 RateLimitError are retried with increasing
    backoff so brief API spikes don't abort the pipeline.
    """

    def invoke(self, input, config=None, **kwargs):
        def _call():
            return normalize_content(super(NormalizedChatAnthropic, self).invoke(input, config, **kwargs))
        return _anthropic_invoke_with_retry(_call)


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatAnthropic instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Anthropic."""
        return validate_model("anthropic", self.model)
