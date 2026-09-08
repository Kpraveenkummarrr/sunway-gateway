"""OpenAI (Whisper) STT provider.

Only instantiated (and only ever calls the API) when STT_PROVIDER is
explicitly set to "openai" and STT_API_KEY is configured — see
app.providers.stt.factory. No network call at import time; `openai` is
imported lazily so the rest of the app works without it installed.
"""

import asyncio
import io

from app.providers.stt.base import STTProvider, STTProviderError, TranscriptionResult

DEFAULT_MODEL = "whisper-1"


class OpenAISTTProvider(STTProvider):
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout_seconds: float = 30.0) -> None:
        if not api_key:
            raise STTProviderError("STT_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise STTProviderError(
                    "STT_PROVIDER=openai requires the 'openai' package "
                    "(pip install openai) — not installed by default."
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        if not audio_bytes:
            raise STTProviderError("Audio input is empty")

        client = self._get_client()
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.wav"

        try:
            response = await asyncio.wait_for(
                client.audio.transcriptions.create(
                    model=self._model,
                    file=audio_file,
                    language=language,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise STTProviderError(f"OpenAI STT request timed out after {self._timeout_seconds}s") from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            raise STTProviderError(f"OpenAI STT request failed: {exc}") from exc

        text = (response.text or "").strip()
        if not text:
            raise STTProviderError("Transcription produced no text (silent/empty audio)")

        return TranscriptionResult(text=text, language=language, confidence=None, duration_seconds=None)
