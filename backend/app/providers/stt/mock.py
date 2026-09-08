"""Deterministic mock STT provider — dev/test only.

Convention: since standing up real audio codec handling is out of scope
for this phase, the mock provider treats the "audio" bytes as UTF-8 text
that IS the transcript (so a test can pass `b"what are your hours?"` as
if it were an audio file and get that exact string back). This is not a
recreation of real STT behavior — it exists purely to exercise the
audio -> STT -> text pipeline shape deterministically.
"""

from app.providers.stt.base import STTProvider, STTProviderError, TranscriptionResult


class MockSTTProvider(STTProvider):
    def __init__(self, *, simulate_failure: bool = False) -> None:
        self._simulate_failure = simulate_failure

    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        if self._simulate_failure:
            raise STTProviderError("Simulated STT provider failure")

        if not audio_bytes:
            raise STTProviderError("Audio input is empty")

        try:
            text = audio_bytes.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise STTProviderError("Malformed audio input (mock provider expects UTF-8 text bytes)") from exc

        if not text:
            raise STTProviderError("Transcription produced no text (silent/empty audio)")

        return TranscriptionResult(
            text=text,
            language=language or "en",
            confidence=1.0,
            duration_seconds=len(audio_bytes) / 16000,  # arbitrary deterministic placeholder
        )
