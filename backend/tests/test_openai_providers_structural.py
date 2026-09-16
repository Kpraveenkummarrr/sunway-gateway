"""Structural tests for the OpenAI provider implementations — verify our
own code (parameter passing, timeout handling, error wrapping, response
parsing) using a fake SDK client (tests/fake_openai.py). No network call,
no API key needed, no cost. These are NOT a substitute for a real live
call (see tests/test_real_provider_integration.py, opt-in only, requires
explicit approval per the Phase 7 report).
"""

import pytest

from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.providers.llm.base import LLMMessage, LLMProviderError
from app.providers.llm.openai_provider import OpenAILLMProvider
from app.providers.stt.base import STTProviderError
from app.providers.stt.openai_provider import OpenAISTTProvider
from app.providers.tts.base import TTSProviderError
from app.providers.tts.openai_provider import OpenAITTSProvider
from tests.fake_openai import (
    FakeAPIError,
    FakeAudioSpeech,
    FakeAudioTranscriptions,
    FakeChatCompletions,
    FakeEmbeddings,
    FakeOpenAIClient,
)

# ---- STT (Whisper) ----


@pytest.mark.asyncio
async def test_openai_stt_valid_audio_returns_text() -> None:
    provider = OpenAISTTProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(transcriptions=FakeAudioTranscriptions(result_text="hello there"))

    result = await provider.transcribe(b"fake-audio-bytes", language="en")

    assert result.text == "hello there"
    call = provider._client.audio.transcriptions.calls[0]
    assert call["model"] == "whisper-1"
    assert call["language"] == "en"


@pytest.mark.asyncio
async def test_openai_stt_rejects_empty_audio_without_calling_api() -> None:
    provider = OpenAISTTProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(transcriptions=FakeAudioTranscriptions(result_text="should not be reached"))

    with pytest.raises(STTProviderError):
        await provider.transcribe(b"", language="en")

    assert provider._client.audio.transcriptions.calls == []


@pytest.mark.asyncio
async def test_openai_stt_empty_transcription_result_is_an_error() -> None:
    provider = OpenAISTTProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(transcriptions=FakeAudioTranscriptions(result_text="   "))

    with pytest.raises(STTProviderError, match="no text"):
        await provider.transcribe(b"fake-audio-bytes")


@pytest.mark.asyncio
async def test_openai_stt_times_out() -> None:
    provider = OpenAISTTProvider(api_key="test-key", timeout_seconds=0.05)
    provider._client = FakeOpenAIClient(transcriptions=FakeAudioTranscriptions(result_text="late", delay=1.0))

    with pytest.raises(STTProviderError, match="timed out"):
        await provider.transcribe(b"fake-audio-bytes")


@pytest.mark.asyncio
async def test_openai_stt_authentication_failure_wrapped() -> None:
    provider = OpenAISTTProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(
        transcriptions=FakeAudioTranscriptions(error=FakeAPIError("401 Unauthorized: invalid API key"))
    )

    with pytest.raises(STTProviderError, match="request failed"):
        await provider.transcribe(b"fake-audio-bytes")


@pytest.mark.asyncio
async def test_openai_stt_generic_provider_failure_wrapped() -> None:
    provider = OpenAISTTProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(transcriptions=FakeAudioTranscriptions(error=FakeAPIError("503 Service Unavailable")))

    with pytest.raises(STTProviderError):
        await provider.transcribe(b"fake-audio-bytes")


def test_openai_stt_requires_api_key() -> None:
    with pytest.raises(STTProviderError):
        OpenAISTTProvider(api_key="")


# ---- LLM (chat completions) ----


@pytest.mark.asyncio
async def test_openai_llm_valid_request_returns_text() -> None:
    provider = OpenAILLMProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="Our hours are 9-5."))

    response = await provider.generate_response(
        system_prompt="You are a helpful assistant.",
        history=[LLMMessage(role="user", content="What are your hours?")],
        retrieved_context="[Source: hours.pdf]\n9-5 Mon-Fri.",
    )

    assert response.text == "Our hours are 9-5."
    call = provider._client.chat.completions.calls[0]
    assert call["messages"][0]["role"] == "system"
    assert "9-5 Mon-Fri" in call["messages"][0]["content"]
    assert "KNOWLEDGE CONTEXT" in call["messages"][0]["content"]
    assert "ANSWER RULES" in call["messages"][0]["content"]
    assert call["messages"][1] == {"role": "user", "content": "What are your hours?"}


@pytest.mark.asyncio
async def test_openai_llm_without_context_omits_it_from_system_prompt() -> None:
    provider = OpenAILLMProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="I don't have that info."))

    await provider.generate_response(
        system_prompt="Base prompt.", history=[LLMMessage(role="user", content="hi")], retrieved_context=None
    )

    call = provider._client.chat.completions.calls[0]
    assert call["messages"][0]["content"] == "Base prompt."


@pytest.mark.asyncio
async def test_openai_llm_rejects_empty_history_without_calling_api() -> None:
    provider = OpenAILLMProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="unreachable"))

    with pytest.raises(LLMProviderError):
        await provider.generate_response(system_prompt="p", history=[], retrieved_context=None)

    assert provider._client.chat.completions.calls == []


@pytest.mark.asyncio
async def test_openai_llm_empty_response_is_an_error() -> None:
    provider = OpenAILLMProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text=""))

    with pytest.raises(LLMProviderError, match="empty"):
        await provider.generate_response(system_prompt="p", history=[LLMMessage(role="user", content="hi")], retrieved_context=None)


@pytest.mark.asyncio
async def test_openai_llm_times_out() -> None:
    provider = OpenAILLMProvider(api_key="test-key", timeout_seconds=0.05)
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(result_text="late", delay=1.0))

    with pytest.raises(LLMProviderError, match="timed out"):
        await provider.generate_response(system_prompt="p", history=[LLMMessage(role="user", content="hi")], retrieved_context=None)


@pytest.mark.asyncio
async def test_openai_llm_provider_failure_wrapped() -> None:
    provider = OpenAILLMProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(completions=FakeChatCompletions(error=FakeAPIError("rate limited")))

    with pytest.raises(LLMProviderError):
        await provider.generate_response(system_prompt="p", history=[LLMMessage(role="user", content="hi")], retrieved_context=None)


@pytest.mark.asyncio
async def test_openai_llm_unexpected_response_shape_handled() -> None:
    """No choices at all — a malformed/unexpected response shape."""
    provider = OpenAILLMProvider(api_key="test-key")
    fake_completions = FakeChatCompletions(result_text="x")
    provider._client = FakeOpenAIClient(completions=fake_completions)

    # Monkeypatch the create() to return zero choices this one time.
    from tests.fake_openai import _FakeChatCompletion

    async def _empty_choices(**kwargs):
        return _FakeChatCompletion(choices=[])

    fake_completions.create = _empty_choices

    with pytest.raises(LLMProviderError, match="empty"):
        await provider.generate_response(system_prompt="p", history=[LLMMessage(role="user", content="hi")], retrieved_context=None)


def test_openai_llm_requires_api_key() -> None:
    with pytest.raises(LLMProviderError):
        OpenAILLMProvider(api_key="")


# ---- TTS ----


@pytest.mark.asyncio
async def test_openai_tts_valid_text_returns_audio_bytes() -> None:
    provider = OpenAITTSProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(speech=FakeAudioSpeech(audio_bytes=b"RIFF....fake-wav-bytes"))

    result = await provider.synthesize("Hello there", voice="alloy", language="en")

    assert result.audio_bytes == b"RIFF....fake-wav-bytes"
    assert result.audio_format == "wav"
    call = provider._client.audio.speech.calls[0]
    assert call["voice"] == "alloy"
    assert call["input"] == "Hello there"


@pytest.mark.asyncio
async def test_openai_tts_rejects_empty_text_without_calling_api() -> None:
    provider = OpenAITTSProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(speech=FakeAudioSpeech(audio_bytes=b"unreachable"))

    with pytest.raises(TTSProviderError):
        await provider.synthesize("   ")

    assert provider._client.audio.speech.calls == []


@pytest.mark.asyncio
async def test_openai_tts_empty_audio_response_is_an_error() -> None:
    provider = OpenAITTSProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(speech=FakeAudioSpeech(audio_bytes=b""))

    with pytest.raises(TTSProviderError, match="empty audio"):
        await provider.synthesize("Hello")


@pytest.mark.asyncio
async def test_openai_tts_times_out() -> None:
    provider = OpenAITTSProvider(api_key="test-key", timeout_seconds=0.05)
    provider._client = FakeOpenAIClient(speech=FakeAudioSpeech(audio_bytes=b"late", delay=1.0))

    with pytest.raises(TTSProviderError, match="timed out"):
        await provider.synthesize("Hello")


@pytest.mark.asyncio
async def test_openai_tts_provider_failure_wrapped() -> None:
    provider = OpenAITTSProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(speech=FakeAudioSpeech(error=FakeAPIError("insufficient_quota")))

    with pytest.raises(TTSProviderError):
        await provider.synthesize("Hello")


def test_openai_tts_requires_api_key() -> None:
    with pytest.raises(TTSProviderError):
        OpenAITTSProvider(api_key="")


# ---- Embeddings ----


@pytest.mark.asyncio
async def test_openai_embeddings_valid_request_returns_vectors() -> None:
    provider = OpenAIEmbeddingProvider(api_key="test-key", dimensions=3)
    provider._client = FakeOpenAIClient(embeddings=FakeEmbeddings(vectors=[[0.1, 0.2, 0.3]]))

    vectors = await provider.embed(["hello"])

    assert vectors == [[0.1, 0.2, 0.3]]
    call = provider._client.embeddings.calls[0]
    assert call["input"] == ["hello"]
    assert call["dimensions"] == 3


@pytest.mark.asyncio
async def test_openai_embeddings_times_out() -> None:
    provider = OpenAIEmbeddingProvider(api_key="test-key", timeout_seconds=0.05)
    provider._client = FakeOpenAIClient(embeddings=FakeEmbeddings(vectors=[[0.1]], delay=1.0))

    with pytest.raises(EmbeddingProviderError, match="timed out"):
        await provider.embed(["hello"])


@pytest.mark.asyncio
async def test_openai_embeddings_provider_failure_wrapped() -> None:
    provider = OpenAIEmbeddingProvider(api_key="test-key")
    provider._client = FakeOpenAIClient(embeddings=FakeEmbeddings(error=FakeAPIError("rate limited")))

    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["hello"])


def test_openai_embeddings_requires_api_key() -> None:
    with pytest.raises(EmbeddingProviderError):
        OpenAIEmbeddingProvider(api_key="")
