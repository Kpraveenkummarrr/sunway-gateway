"""Fake objects shaped like the `openai` Python SDK's response types, for
structural-only testing of our OpenAI provider classes (app/providers/
{stt,llm,tts,embeddings}/openai_provider.py) — no network call, no cost.

These test OUR code: does it pass the right parameters, parse the
response correctly, wrap errors into our *ProviderError types, and time
out correctly? They do NOT test OpenAI's actual API behavior — that
requires a real, explicitly-approved live call (see
tests/test_real_provider_integration.py, opt-in only).
"""

import asyncio
from dataclasses import dataclass, field


class FakeAPIError(Exception):
    """Stands in for openai's exception types (AuthenticationError,
    APIError, etc.) — our provider code catches `Exception` broadly and
    wraps it, so any exception type here exercises the same path."""


@dataclass
class _FakeTranscription:
    text: str


@dataclass
class _FakeMessage:
    content: str | None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str | None = "stop"


@dataclass
class _FakeChatCompletion:
    choices: list[_FakeChoice] = field(default_factory=list)


@dataclass
class _FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


class _FakeSpeechResponse:
    def __init__(self, audio_bytes: bytes) -> None:
        self._audio_bytes = audio_bytes

    def read(self) -> bytes:
        return self._audio_bytes


class FakeAudioTranscriptions:
    def __init__(self, *, result_text: str = "", error: Exception | None = None, delay: float = 0.0) -> None:
        self.result_text = result_text
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return _FakeTranscription(text=self.result_text)


class FakeAudioSpeech:
    def __init__(self, *, audio_bytes: bytes = b"", error: Exception | None = None, delay: float = 0.0) -> None:
        self.audio_bytes = audio_bytes
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return _FakeSpeechResponse(self.audio_bytes)


class FakeChatCompletions:
    def __init__(
        self, *, result_text: str = "", finish_reason: str = "stop", error: Exception | None = None, delay: float = 0.0
    ) -> None:
        self.result_text = result_text
        self.finish_reason = finish_reason
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return _FakeChatCompletion(
            choices=[_FakeChoice(message=_FakeMessage(content=self.result_text), finish_reason=self.finish_reason)]
        )


class FakeEmbeddings:
    def __init__(self, *, vectors: list[list[float]] | None = None, error: Exception | None = None, delay: float = 0.0) -> None:
        self.vectors = vectors or []
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return _FakeEmbeddingResponse(data=[_FakeEmbeddingItem(embedding=v) for v in self.vectors])


class FakeAudio:
    def __init__(self, transcriptions: FakeAudioTranscriptions | None = None, speech: FakeAudioSpeech | None = None) -> None:
        self.transcriptions = transcriptions
        self.speech = speech


class FakeChat:
    def __init__(self, completions: FakeChatCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    """Drop-in stand-in for `openai.AsyncOpenAI` — assign to
    `provider._client` directly, bypassing the real SDK entirely."""

    def __init__(
        self,
        *,
        transcriptions: FakeAudioTranscriptions | None = None,
        speech: FakeAudioSpeech | None = None,
        completions: FakeChatCompletions | None = None,
        embeddings: FakeEmbeddings | None = None,
    ) -> None:
        self.audio = FakeAudio(transcriptions=transcriptions, speech=speech)
        if completions is not None:
            self.chat = FakeChat(completions=completions)
        self.embeddings = embeddings
