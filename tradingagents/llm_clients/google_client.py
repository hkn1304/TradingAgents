import logging
import time
from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

logger = logging.getLogger(__name__)

_GOOGLE_OVERLOAD_DELAYS = (5, 15, 30)
_GOOGLE_RATELIMIT_DELAYS = (10, 30, 60)


def _google_invoke_with_retry(invoke_fn):
    """Retry invoke_fn on Google 429 ResourceExhausted and 503 ServiceUnavailable."""
    try:
        from google.api_core import exceptions as gexc
        _quota_exc = (gexc.ResourceExhausted,)
        _service_exc = (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.DeadlineExceeded)
    except ImportError:
        _quota_exc = _service_exc = ()

    last_exc = None
    max_attempts = max(len(_GOOGLE_OVERLOAD_DELAYS), len(_GOOGLE_RATELIMIT_DELAYS)) + 1
    for attempt in range(max_attempts):
        try:
            return invoke_fn()
        except Exception as exc:
            # Match by type if google.api_core is available; fall back to string check.
            is_quota = _quota_exc and isinstance(exc, _quota_exc)
            is_service = _service_exc and isinstance(exc, _service_exc)
            if not (is_quota or is_service):
                err = str(exc).lower()
                is_quota = "resource_exhausted" in err or "429" in err or "quota" in err
                is_service = "unavailable" in err or "503" in err or "500" in err or "deadline" in err
            if not (is_quota or is_service):
                raise
            last_exc = exc
            delays = _GOOGLE_RATELIMIT_DELAYS if is_quota else _GOOGLE_OVERLOAD_DELAYS
            label = "429 Quota" if is_quota else "5xx ServiceError"

        if attempt >= len(delays):
            break
        delay = delays[attempt]
        logger.warning(
            "Google %s (attempt %d/%d) — retrying in %ds",
            label, attempt + 1, len(delays), delay,
        )
        time.sleep(delay)

    raise last_exc


class NormalizedChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """ChatGoogleGenerativeAI with normalized content output and transient-error retry.

    Gemini models return content as list of typed blocks.
    Retries on 429 ResourceExhausted and 503 ServiceUnavailable.
    """

    def invoke(self, input, config=None, **kwargs):
        return _google_invoke_with_retry(
            lambda: normalize_content(super(NormalizedChatGoogleGenerativeAI, self).invoke(input, config, **kwargs))
        )


class GoogleClient(BaseLLMClient):
    """Client for Google Gemini models."""

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured ChatGoogleGenerativeAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in ("timeout", "max_retries", "callbacks", "http_client", "http_async_client"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Unified api_key maps to provider-specific google_api_key
        google_api_key = self.kwargs.get("api_key") or self.kwargs.get("google_api_key")
        if google_api_key:
            llm_kwargs["google_api_key"] = google_api_key

        # Map thinking_level to appropriate API param based on model
        # Gemini 3 Pro: low, high
        # Gemini 3 Flash: minimal, low, medium, high
        # Gemini 2.5: thinking_budget (0=disable, -1=dynamic)
        thinking_level = self.kwargs.get("thinking_level")
        if thinking_level:
            model_lower = self.model.lower()
            if "gemini-3" in model_lower:
                # Gemini 3 Pro doesn't support "minimal", use "low" instead
                if "pro" in model_lower and thinking_level == "minimal":
                    thinking_level = "low"
                llm_kwargs["thinking_level"] = thinking_level
            else:
                # Gemini 2.5: map to thinking_budget
                llm_kwargs["thinking_budget"] = -1 if thinking_level == "high" else 0

        return NormalizedChatGoogleGenerativeAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for Google."""
        return validate_model("google", self.model)
