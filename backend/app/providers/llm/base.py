from abc import ABC, abstractmethod
from dataclasses import dataclass


class LLMProviderError(Exception):
    """Raised when an LLM provider is misconfigured, times out, fails
    authentication, or returns an unusable response."""


@dataclass(frozen=True)
class LLMMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    finish_reason: str | None = None


class LLMProvider(ABC):
    """Abstraction over LLM vendors so the conversation orchestration layer
    isn't hard-coded to one provider."""

    @property
    def provider_name(self) -> str:
        """Human-readable provider identity for logs and A/B evidence
        (e.g. "Gemini", "Sarvam-M local"). Defaults to the class name so a
        provider that doesn't override this still logs something useful."""
        return type(self).__name__

    def for_max_tokens(self, max_tokens: int) -> "LLMProvider":
        """Immutable per-turn configuration view; mocks need no token limit."""
        return self

    @abstractmethod
    async def generate_response(
        self,
        *,
        system_prompt: str,
        history: list[LLMMessage],
        retrieved_context: str | None,
    ) -> LLMResponse:
        """Generates the assistant's next reply.

        `retrieved_context` is the already-bounded, already-formatted RAG
        context (or None if no relevant knowledge was retrieved) — this
        provider is not responsible for retrieval or context-size limiting,
        only for producing a response given what it was handed.
        """
