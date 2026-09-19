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
            timeout_seconds=min(settings.llm_timeout_seconds, settings.provider_timeout_seconds),
            max_tokens=settings.llm_max_tokens,
            keepalive_expiry_seconds=settings.http_keepalive_seconds,
        )

    if provider_name == "gemini":
        from app.providers.llm.openai_provider import OpenAILLMProvider

        if not settings.gemini_api_key:
            raise LLMProviderError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        # Gemini's official OpenAI-compatible endpoint, through the same client.
        return OpenAILLMProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model or "gemini-2.5-flash",
            timeout_seconds=min(settings.llm_timeout_seconds, settings.provider_timeout_seconds),
            max_tokens=settings.llm_max_tokens,
            keepalive_expiry_seconds=settings.http_keepalive_seconds,
            base_url=settings.gemini_base_url,
            provider_name="Gemini",
            reasoning_effort=settings.gemini_reasoning_effort or None,
        )

    if provider_name == "sarvam_m":
        from app.providers.llm.sarvam_m_provider import SarvamMProvider

        return SarvamMProvider(
            model_path=settings.sarvam_m_model_path,
            context_tokens=settings.sarvam_m_context_tokens,
            max_tokens=settings.llm_max_tokens,
            gpu_layers=settings.sarvam_m_gpu_layers,
            timeout_seconds=settings.provider_timeout_seconds,
            require_hardware_check=settings.sarvam_m_require_hardware_check,
        )

    raise LLMProviderError(
        "No LLM provider configured. Set LLM_PROVIDER to 'gemini' (with "
        "GEMINI_API_KEY), 'openai' (with LLM_API_KEY), or 'sarvam_m' (with "
        "SARVAM_M_MODEL_PATH pointing at a local GGUF) for real responses, "
        "or 'mock' for local development/testing only."
    )
