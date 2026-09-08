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

    raise STTProviderError(
        "No STT provider configured. Set STT_PROVIDER to 'openai' (with "
        "STT_API_KEY) for real transcription, or 'mock' for local "
        "development/testing only."
    )
