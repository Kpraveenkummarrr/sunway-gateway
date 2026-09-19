from app.core.config import Settings
from app.providers.stt.base import STTProvider, STTProviderError
from app.providers.stt.mock import MockSTTProvider


def get_stt_provider(settings: Settings) -> STTProvider:
    """Selects the STT provider from STT_PROVIDER.

    No implicit fallback to the mock provider — "mock" must be selected
    explicitly (tests/dev only) so a misconfigured deployment fails loudly
    instead of silently pretending to transcribe audio.
    """
    provider_name = (settings.stt_provider or "").strip().lower()

    if provider_name == "mock":
        return MockSTTProvider()

    if provider_name == "openai":
        from app.providers.stt.openai_provider import OpenAISTTProvider

        if not settings.stt_api_key:
            raise STTProviderError("STT_PROVIDER=openai but STT_API_KEY is not set")
        return OpenAISTTProvider(
            api_key=settings.stt_api_key,
            model=settings.stt_model or "whisper-1",
            timeout_seconds=settings.provider_timeout_seconds,
        )

    if provider_name == "bhashini":
        from app.providers.bhashini.client import BhashiniClient
        from app.providers.stt.bhashini_provider import BhashiniSTTProvider

        if not settings.bhashini_inference_api_key:
            raise STTProviderError("STT_PROVIDER=bhashini but BHASHINI_INFERENCE_API_KEY is not set")
        return BhashiniSTTProvider(
            client=BhashiniClient(
                api_key=settings.bhashini_inference_api_key,
                inference_url=settings.bhashini_inference_url,
                timeout_seconds=min(settings.stt_timeout_seconds, settings.provider_timeout_seconds),
                keepalive_expiry_seconds=settings.http_keepalive_seconds,
            ),
            service_id=settings.bhashini_asr_service_id,
            language=settings.ai_language,
        )

    raise STTProviderError(
        "No STT provider configured. Set STT_PROVIDER to 'bhashini' (with "
        "BHASHINI_INFERENCE_API_KEY) or 'openai' (with STT_API_KEY) for real "
        "transcription, or 'mock' for local development/testing only."
    )
