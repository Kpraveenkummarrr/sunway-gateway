"""Deterministic mock TTS provider — dev/test only.

Symmetric with the mock STT provider's convention: "audio" is just the
input text UTF-8-encoded, so a test can round-trip text -> mock TTS ->
mock STT and get the original text back. Not a recreation of real speech
synthesis — it exists purely to exercise the text -> TTS -> audio
pipeline shape deterministically.
"""

from app.providers.tts.base import SynthesisResult, TTSProvider, TTSProviderError


class MockTTSProvider(TTSProvider):
    def __init__(self, *, simulate_failure: bool = False) -> None:
        self._simulate_failure = simulate_failure

    async def synthesize(self, text: str, *, voice: str | None = None, language: str | None = None) -> SynthesisResult:
        if self._simulate_failure:
            raise TTSProviderError("Simulated TTS provider failure")

        if not text or not text.strip():
            raise TTSProviderError("Cannot synthesize empty text")

        return SynthesisResult(audio_bytes=text.encode("utf-8"), audio_format="text/mock")
