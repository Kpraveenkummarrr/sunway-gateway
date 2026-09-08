from abc import ABC, abstractmethod
from dataclasses import dataclass


class TTSProviderError(Exception):
    """Raised when a TTS provider is misconfigured, times out, fails
    authentication, or the input text can't be synthesized."""


@dataclass(frozen=True)
class SynthesisResult:
    audio_bytes: bytes
    audio_format: str  # e.g. "wav", "mp3"


class TTSProvider(ABC):
    """Abstraction over text-to-speech vendors."""

    @abstractmethod
    async def synthesize(self, text: str, *, voice: str | None = None, language: str | None = None) -> SynthesisResult:
        """Synthesizes speech audio from text.

        Raises TTSProviderError for empty/invalid text, provider timeouts,
        or auth failures.
        """
