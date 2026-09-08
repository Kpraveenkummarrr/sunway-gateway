import pytest

from app.core.config import Settings
from app.providers.stt.base import STTProviderError
from app.providers.stt.factory import get_stt_provider
from app.providers.stt.mock import MockSTTProvider


@pytest.mark.asyncio
async def test_mock_stt_transcribes_text_bytes() -> None:
    provider = MockSTTProvider()
    result = await provider.transcribe(b"what are your business hours?")
    assert result.text == "what are your business hours?"
    assert result.language == "en"
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_mock_stt_respects_language_hint() -> None:
    provider = MockSTTProvider()
    result = await provider.transcribe(b"hola", language="es")
    assert result.language == "es"


@pytest.mark.asyncio
async def test_mock_stt_rejects_empty_audio() -> None:
    provider = MockSTTProvider()
    with pytest.raises(STTProviderError):
        await provider.transcribe(b"")


@pytest.mark.asyncio
async def test_mock_stt_rejects_malformed_audio() -> None:
    provider = MockSTTProvider()
    with pytest.raises(STTProviderError):
        await provider.transcribe(b"\xff\xfe\x00\x01invalid-utf8")


@pytest.mark.asyncio
async def test_mock_stt_rejects_whitespace_only_audio() -> None:
    provider = MockSTTProvider()
    with pytest.raises(STTProviderError):
        await provider.transcribe(b"   \n\t  ")


@pytest.mark.asyncio
async def test_mock_stt_simulated_provider_failure() -> None:
    provider = MockSTTProvider(simulate_failure=True)
    with pytest.raises(STTProviderError):
        await provider.transcribe(b"hello")


def test_factory_raises_clear_error_when_unconfigured() -> None:
    settings = Settings(stt_provider="")
    with pytest.raises(STTProviderError):
        get_stt_provider(settings)


def test_factory_returns_mock_when_explicitly_selected() -> None:
    settings = Settings(stt_provider="mock")
    provider = get_stt_provider(settings)
    assert isinstance(provider, MockSTTProvider)


def test_factory_raises_when_openai_selected_without_api_key() -> None:
    settings = Settings(stt_provider="openai", stt_api_key="")
    with pytest.raises(STTProviderError):
        get_stt_provider(settings)


def test_factory_rejects_unknown_provider_name() -> None:
    settings = Settings(stt_provider="some-unsupported-vendor")
    with pytest.raises(STTProviderError):
        get_stt_provider(settings)
