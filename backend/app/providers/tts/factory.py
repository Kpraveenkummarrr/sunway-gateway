from app.core.config import Settings
from app.providers.tts.base import TTSProvider, TTSProviderError
from app.providers.tts.mock import MockTTSProvider


def get_tts_provider(settings: Settings) -> TTSProvider:
    """Selects the TTS provider from TTS_PROVIDER.

    No implicit fallback to the mock provider — "mock" must be selected
    explicitly (tests/dev only) so a misconfigured deployment fails loudly
    instead of silently returning fake audio to real users.
    """
    provider_name = (settings.tts_provider or "").strip().lower()

    if provider_name == "mock":
        return MockTTSProvider()

    if provider_name == "openai":
        from app.providers.tts.openai_provider import OpenAITTSProvider

        if not settings.tts_api_key:
            raise TTSProviderError("TTS_PROVIDER=openai but TTS_API_KEY is not set")
        return OpenAITTSProvider(
            api_key=settings.tts_api_key,
            model=settings.tts_model or "tts-1",
            default_voice=settings.tts_voice or "alloy",
            timeout_seconds=settings.provider_timeout_seconds,
        )

    if provider_name == "bhashini":
        from app.providers.bhashini.client import BhashiniClient
        from app.providers.tts.bhashini_provider import BhashiniTTSProvider

        if not settings.bhashini_inference_api_key:
            raise TTSProviderError("TTS_PROVIDER=bhashini but BHASHINI_INFERENCE_API_KEY is not set")
        return BhashiniTTSProvider(
            client=BhashiniClient(
                api_key=settings.bhashini_inference_api_key,
                inference_url=settings.bhashini_inference_url,
                timeout_seconds=settings.provider_timeout_seconds,
            ),
            service_id=settings.bhashini_tts_service_id,
            gender=settings.bhashini_tts_gender,
            language=settings.ai_language,
        )

    raise TTSProviderError(
        "No TTS provider configured. Set TTS_PROVIDER to 'bhashini' (with "
        "BHASHINI_INFERENCE_API_KEY) or 'openai' (with TTS_API_KEY) for real "
        "synthesis, or 'mock' for local development/testing only."
    )
