"""Bhashini (Dhruva) Hindi TTS provider.

Only instantiated when TTS_PROVIDER=bhashini — see app.providers.tts.factory.
Returns the decoded WAV at Bhashini's native sample rate (48kHz in live
testing); the call controller normalizes it to 8kHz for Asterisk playback.
"""

import base64
import binascii

from app.providers.bhashini.client import BhashiniClient, BhashiniError
from app.providers.tts.base import SynthesisResult, TTSProvider, TTSProviderError
from app.services.audio import AudioFormatError, read_wav_info

SUPPORTED_LANGUAGE = "hi"
SUPPORTED_GENDERS = ("female", "male")


class BhashiniTTSProvider(TTSProvider):
    def __init__(
        self,
        *,
        client: BhashiniClient,
        service_id: str,
        gender: str = "female",
        language: str = SUPPORTED_LANGUAGE,
    ) -> None:
        if not service_id:
            raise TTSProviderError("BHASHINI_TTS_SERVICE_ID is not set")
        if gender not in SUPPORTED_GENDERS:
            raise TTSProviderError(f"BHASHINI_TTS_GENDER must be one of {SUPPORTED_GENDERS} (got '{gender}')")
        if language != SUPPORTED_LANGUAGE:
            raise TTSProviderError(
                f"TTS_PROVIDER=bhashini is configured for Hindi only; AI_LANGUAGE must be "
                f"'{SUPPORTED_LANGUAGE}' (got '{language}')"
            )
        self._client = client
        self._service_id = service_id
        self._gender = gender
        self._language = language

    async def synthesize(self, text: str, *, voice: str | None = None, language: str | None = None) -> SynthesisResult:
        if not text or not text.strip():
            raise TTSProviderError("Cannot synthesize empty text")
        if language not in (None, self._language):
            raise TTSProviderError(f"Bhashini TTS is configured for '{self._language}' only (got '{language}')")

        task_config = {
            "taskType": "tts",
            "config": {
                "language": {"sourceLanguage": self._language},
                "serviceId": self._service_id,
                "gender": self._gender,
            },
        }
        input_data = {"input": [{"source": text.strip()}], "audio": [{"audioContent": None}]}

        try:
            result = await self._client.run_task(task_config=task_config, input_data=input_data)
        except BhashiniError as exc:
            raise TTSProviderError(f"Bhashini TTS failed: {exc}") from None

        audio_items = result.get("audio")
        first = audio_items[0] if isinstance(audio_items, list) and audio_items and isinstance(audio_items[0], dict) else {}
        encoded = first.get("audioContent")
        if not isinstance(encoded, str) or not encoded:
            raise TTSProviderError("Bhashini TTS response is missing audio[0].audioContent")

        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise TTSProviderError("Bhashini TTS audioContent is not valid base64") from None

        try:
            read_wav_info(audio_bytes)
        except AudioFormatError as exc:
            raise TTSProviderError(f"Bhashini TTS did not return a readable PCM WAV: {exc}") from None

        return SynthesisResult(audio_bytes=audio_bytes, audio_format="wav")
