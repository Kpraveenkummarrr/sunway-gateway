import pytest

from app.core.config import Settings
from app.providers.tts.base import TTSProviderError
from app.providers.tts.factory import get_tts_provider
from app.providers.tts.mock import MockTTSProvider


@pytest.mark.asyncio
async def test_mock_tts_synthesizes_text_to_bytes() -> None:
    provider = MockTTSProvider()
    result = await provider.synthesize("hello there")
    assert result.audio_bytes == b"hello there"
    assert result.audio_format == "text/mock"


@pytest.mark.asyncio
async def test_mock_tts_rejects_empty_text() -> None:
    provider = MockTTSProvider()
    with pytest.raises(TTSProviderError):
        await provider.synthesize("")


@pytest.mark.asyncio
async def test_mock_tts_rejects_whitespace_only_text() -> None:
    provider = MockTTSProvider()
    with pytest.raises(TTSProviderError):
        await provider.synthesize("   ")


@pytest.mark.asyncio
async def test_mock_tts_simulated_provider_failure() -> None:
    provider = MockTTSProvider(simulate_failure=True)
    with pytest.raises(TTSProviderError):
        await provider.synthesize("hello")


@pytest.mark.asyncio
async def test_mock_stt_tts_roundtrip() -> None:
    """Symmetric mock convention: text -> TTS -> audio -> STT -> same text."""
    from app.providers.stt.mock import MockSTTProvider

    tts = MockTTSProvider()
    stt = MockSTTProvider()

    original = "what are your business hours?"
    audio = await tts.synthesize(original)
    transcription = await stt.transcribe(audio.audio_bytes)
    assert transcription.text == original


def test_factory_raises_clear_error_when_unconfigured() -> None:
    settings = Settings(tts_provider="")
    with pytest.raises(TTSProviderError):
        get_tts_provider(settings)


def test_factory_returns_mock_when_explicitly_selected() -> None:
    settings = Settings(tts_provider="mock")
    provider = get_tts_provider(settings)
    assert isinstance(provider, MockTTSProvider)


def test_factory_raises_when_openai_selected_without_api_key() -> None:
    settings = Settings(tts_provider="openai", tts_api_key="")
    with pytest.raises(TTSProviderError):
        get_tts_provider(settings)


def test_factory_rejects_unknown_provider_name() -> None:
    settings = Settings(tts_provider="some-unsupported-vendor")
    with pytest.raises(TTSProviderError):
        get_tts_provider(settings)
