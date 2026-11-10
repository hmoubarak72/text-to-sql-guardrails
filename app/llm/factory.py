from __future__ import annotations

from app.config import Settings
from app.llm.base import LLMProvider


def make_provider(settings: Settings) -> LLMProvider:
    kind = settings.llm_provider.lower()
    if kind == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), settings.anthropic_model,
                                 settings.llm_timeout_s, settings.llm_temperature)
    if kind == "openai":
        if not settings.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(settings.openai_api_key.get_secret_value(), settings.openai_model,
                              settings.llm_timeout_s, settings.llm_temperature)
    if kind == "mock":
        from app.llm.mock_provider import MockProvider

        return MockProvider(settings.mock_fixtures_path)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")
