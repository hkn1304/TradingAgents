"""Shared model catalog for CLI selections and validation."""

from __future__ import annotations

from typing import Dict, List, Tuple

ModelOption = Tuple[str, str]
ProviderModeOptions = Dict[str, Dict[str, List[ModelOption]]]


MODEL_OPTIONS: ProviderModeOptions = {
    "openai": {
        "quick": [
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Nano - Cheapest, high-volume tasks", "gpt-5.4-nano"),
            ("GPT-5.4 - Latest frontier, 1M context", "gpt-5.4"),
            ("GPT-4.1 - Smartest non-reasoning model", "gpt-4.1"),
        ],
        "deep": [
            ("GPT-5.4 - Latest frontier, 1M context", "gpt-5.4"),
            ("GPT-5.2 - Strong reasoning, cost-effective", "gpt-5.2"),
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Pro - Most capable, expensive ($30/$180 per 1M tokens)", "gpt-5.4-pro"),
        ],
    },
    "anthropic": {
        "quick": [
            ("Claude Haiku 4.5 - Fastest, cheapest (~$0.25/1M)", "claude-haiku-4-5-20251001"),
            ("Claude Sonnet 4.6 - Balanced speed & quality", "claude-sonnet-4-6"),
            ("Claude Opus 4.7 - Most capable (expensive)", "claude-opus-4-7"),
        ],
        "deep": [
            ("Claude Haiku 4.5 - Dev/budget (~$1.25/1M out)", "claude-haiku-4-5-20251001"),
            ("Claude Sonnet 4.6 - Balanced speed & quality", "claude-sonnet-4-6"),
            ("Claude Opus 4.7 - Best reasoning (expensive)", "claude-opus-4-7"),
        ],
    },
    "google": {
        "quick": [
            ("Gemini 3 Flash - Next-gen fast", "gemini-3-flash-preview"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
            ("Gemini 3.1 Flash Lite - Most cost-efficient", "gemini-3.1-flash-lite-preview"),
            ("Gemini 2.5 Flash Lite - Fast, low-cost", "gemini-2.5-flash-lite"),
        ],
        "deep": [
            ("Gemini 3.1 Pro - Reasoning-first, complex workflows", "gemini-3.1-pro-preview"),
            ("Gemini 3 Flash - Next-gen fast", "gemini-3-flash-preview"),
            ("Gemini 2.5 Pro - Stable pro model", "gemini-2.5-pro"),
            ("Gemini 2.5 Flash - Balanced, stable", "gemini-2.5-flash"),
        ],
    },
    "xai": {
        "quick": [
            ("Grok 4.1 Fast (Non-Reasoning) - Speed optimized, 2M ctx", "grok-4-1-fast-non-reasoning"),
            ("Grok 4 Fast (Non-Reasoning) - Speed optimized", "grok-4-fast-non-reasoning"),
            ("Grok 4.1 Fast (Reasoning) - High-performance, 2M ctx", "grok-4-1-fast-reasoning"),
        ],
        "deep": [
            ("Grok 4 - Flagship model", "grok-4-0709"),
            ("Grok 4.1 Fast (Reasoning) - High-performance, 2M ctx", "grok-4-1-fast-reasoning"),
            ("Grok 4 Fast (Reasoning) - High-performance", "grok-4-fast-reasoning"),
            ("Grok 4.1 Fast (Non-Reasoning) - Speed optimized, 2M ctx", "grok-4-1-fast-non-reasoning"),
        ],
    },
    "deepseek": {
        "quick": [
            ("DeepSeek V3 - Fast, low-cost ($0.27/1M in)", "deepseek-chat"),
            ("DeepSeek R1 - Reasoning model ($0.55/1M in)", "deepseek-reasoner"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("DeepSeek R1 - Reasoning model ($0.55/1M in)", "deepseek-reasoner"),
            ("DeepSeek V3 - Fast, low-cost ($0.27/1M in)", "deepseek-chat"),
            ("Custom model ID", "custom"),
        ],
    },
    "deepseek_anthropic": {
        "quick": [
            ("DeepSeek V3 - Fast, Anthropic-compat endpoint", "deepseek-chat"),
        ],
        "deep": [
            ("DeepSeek R1 - Reasoning, Anthropic-compat endpoint", "deepseek-reasoner"),
            ("DeepSeek V3 - Fast, Anthropic-compat endpoint", "deepseek-chat"),
        ],
    },
    "qwen": {
        "quick": [
            ("Qwen 3.5 Flash", "qwen3.5-flash"),
            ("Qwen Plus", "qwen-plus"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("Qwen 3.6 Plus", "qwen3.6-plus"),
            ("Qwen 3.5 Plus", "qwen3.5-plus"),
            ("Qwen 3 Max", "qwen3-max"),
            ("Custom model ID", "custom"),
        ],
    },
    "glm": {
        "quick": [
            ("GLM-4.7", "glm-4.7"),
            ("GLM-5", "glm-5"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("GLM-5.1", "glm-5.1"),
            ("GLM-5", "glm-5"),
            ("Custom model ID", "custom"),
        ],
    },
    # OpenRouter: fetched dynamically. Azure: any deployed model name.
    "ollama": {
        "quick": [
            ("Qwen3:latest (8B, local)", "qwen3:latest"),
            ("GPT-OSS:latest (20B, local)", "gpt-oss:latest"),
            ("GLM-4.7-Flash:latest (30B, local)", "glm-4.7-flash:latest"),
        ],
        "deep": [
            ("GLM-4.7-Flash:latest (30B, local)", "glm-4.7-flash:latest"),
            ("GPT-OSS:latest (20B, local)", "gpt-oss:latest"),
            ("Qwen3:latest (8B, local)", "qwen3:latest"),
        ],
    },
}


def get_model_options(provider: str, mode: str) -> List[ModelOption]:
    """Return shared model options for a provider and selection mode."""
    return MODEL_OPTIONS[provider.lower()][mode]


def get_known_models() -> Dict[str, List[str]]:
    """Build known model names from the shared CLI catalog."""
    return {
        provider: sorted(
            {
                value
                for options in mode_options.values()
                for _, value in options
            }
        )
        for provider, mode_options in MODEL_OPTIONS.items()
    }
