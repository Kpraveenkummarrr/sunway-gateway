from app.core.config import Settings
from app.providers.llm.base import LLMProvider, LLMProviderError
from app.providers.llm.mock import MockLLMProvider


def get_llm_provider(settings: Settings) -> LLMProvider:
    """Selects the LLM provider from LLM_PROVIDER.

    No implicit fallback to the mock provider — "mock" must be selected
    explicitly (tests/dev only) so a misconfigured deployment fails loudly
    instead of silently returning canned responses to real users.
    """
    provider_name = (settings.llm_provider or "").strip().lower()

    if provider_name == "mock":
        return MockLLMProvider()

    if provider_name == "openai":
        from app.providers.llm.openai_provider import OpenAILLMProvider

        if not settings.llm_api_key:
            raise LLMProviderError("LLM_PROVIDER=openai but LLM_API_KEY is not set")
        return OpenAILLMProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model or "gpt-4o-mini",
            timeout_seconds=settings.provider_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
        )

    if provider_name == "gemini":
        from app.providers.llm.openai_provider import OpenAILLMProvider

        if not settings.gemini_api_key:
            raise LLMProviderError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        # Gemini's official OpenAI-compatible endpoint, through the same client.
        return OpenAILLMProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model or "gemini-2.5-flash",
            timeout_seconds=settings.provider_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            base_url=settings.gemini_base_url,
            provider_name="Gemini",
            reasoning_effort=settings.gemini_reasoning_effort or None,
        )

    raise LLMProviderError(
        "No LLM provider configured. Set LLM_PROVIDER to 'gemini' (with "
        "GEMINI_API_KEY) or 'openai' (with LLM_API_KEY) for real responses, "
        "or 'mock' for local development/testing only."
    )
