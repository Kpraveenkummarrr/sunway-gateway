"""OpenAI TTS provider.

Only instantiated (and only ever calls the API) when TTS_PROVIDER is
explicitly set to "openai" and TTS_API_KEY is configured — see
app.providers.tts.factory. No network call at import time; `openai` is
imported lazily so the rest of the app works without it installed.
"""

import asyncio

from app.providers.tts.base import SynthesisResult, TTSProvider, TTSProviderError

DEFAULT_MODEL = "tts-1"
DEFAULT_VOICE = "alloy"


class OpenAITTSProvider(TTSProvider):
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        default_voice: str = DEFAULT_VOICE,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise TTSProviderError("TTS_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._default_voice = default_voice
        self._timeout_seconds = timeout_seconds
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise TTSProviderError(
                    "TTS_PROVIDER=openai requires the 'openai' package "
                    "(pip install openai) — not installed by default."
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def synthesize(self, text: str, *, voice: str | None = None, language: str | None = None) -> SynthesisResult:
        if not text or not text.strip():
            raise TTSProviderError("Cannot synthesize empty text")

        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.audio.speech.create(
                    model=self._model,
                    voice=voice or self._default_voice,
                    input=text,
                    response_format="wav",
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise TTSProviderError(f"OpenAI TTS request timed out after {self._timeout_seconds}s") from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            raise TTSProviderError(f"OpenAI TTS request failed: {exc}") from exc

        audio_bytes = response.read() if hasattr(response, "read") else bytes(response.content)
        if not audio_bytes:
            raise TTSProviderError("OpenAI TTS returned empty audio")

        return SynthesisResult(audio_bytes=audio_bytes, audio_format="wav")
