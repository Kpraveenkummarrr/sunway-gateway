from abc import ABC, abstractmethod
from dataclasses import dataclass


class STTProviderError(Exception):
    """Raised when an STT provider is misconfigured, times out, fails
    authentication, or the audio can't be transcribed."""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None
    confidence: float | None
    duration_seconds: float | None


class STTProvider(ABC):
    """Abstraction over speech-to-text vendors."""

    async def warm_up(self) -> None:
        """Open connections ahead of the first request. Optional; must never raise."""

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        """Transcribes audio to text.

        Raises STTProviderError for empty/malformed audio, provider
        timeouts, auth failures, or an empty resulting transcription
        (callers should treat "recognized silence" as an error, not a
        successful empty answer).
        """
