"""Bhashini (Dhruva) Hindi ASR provider.

Only instantiated when STT_PROVIDER=bhashini — see app.providers.stt.factory.
Caller audio (8kHz Asterisk recordings) is trimmed of leading/trailing
silence and resampled to 16kHz mono 16-bit PCM before upload,
base64-encoded, and sent as a single-task ASR pipeline.
"""

import asyncio
import base64

from app.providers.bhashini.client import BhashiniClient, BhashiniError
from app.providers.stt.base import STTProvider, STTProviderError, TranscriptionResult
from app.services.audio import AudioFormatError, prepare_for_asr, read_wav_info

SUPPORTED_LANGUAGE = "hi"


class BhashiniSTTProvider(STTProvider):
    def __init__(self, *, client: BhashiniClient, service_id: str, language: str = SUPPORTED_LANGUAGE) -> None:
        if not service_id:
            raise STTProviderError("BHASHINI_ASR_SERVICE_ID is not set")
        if language != SUPPORTED_LANGUAGE:
            raise STTProviderError(
                f"STT_PROVIDER=bhashini is configured for Hindi only; AI_LANGUAGE must be "
                f"'{SUPPORTED_LANGUAGE}' (got '{language}')"
            )
        self._client = client
        self._service_id = service_id
        self._language = language

    async def warm_up(self) -> None:
        await self._client.warm_up()

    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        if not audio_bytes:
            raise STTProviderError("Audio input is empty")
        if language not in (None, self._language):
            raise STTProviderError(f"Bhashini ASR is configured for '{self._language}' only (got '{language}')")

        try:
            # Trims the recording's leading pause and trailing end-of-speech
            # silence, then resamples to 16kHz — less audio to upload/transcribe.
            prepared = await asyncio.to_thread(prepare_for_asr, audio_bytes)
            duration = read_wav_info(prepared).duration_seconds
        except AudioFormatError as exc:
            raise STTProviderError(f"Caller audio is not usable PCM WAV: {exc}") from exc

        # Exactly the request shape verified live (HTTP 200). Sending
        # inputData.input with source=null is rejected with 422; the sample
        # rate travels in the WAV header of the resampled audio.
        task_config = {
            "taskType": "asr",
            "config": {
                "serviceId": self._service_id,
                "language": {"sourceLanguage": self._language},
            },
        }
        input_data = {"audio": [{"audioContent": base64.b64encode(prepared).decode("ascii")}]}

        try:
            result = await self._client.run_task(task_config=task_config, input_data=input_data)
        except BhashiniError as exc:
            raise STTProviderError(f"Bhashini ASR failed: {exc}") from None

        outputs = result.get("output")
        first = outputs[0] if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict) else {}
        text = first.get("source")
        if not isinstance(text, str):
            raise STTProviderError("Bhashini ASR response is missing output[0].source")
        text = text.strip()
        if not text:
            raise STTProviderError("Transcription produced no text (silent/empty audio)")

        return TranscriptionResult(text=text, language=self._language, confidence=None, duration_seconds=duration)
