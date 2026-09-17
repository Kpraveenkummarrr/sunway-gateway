"""Deterministic mock LLM provider — dev/test only.

Doesn't call any real model. Applies a simple, deterministic rule so the
orchestration layer's behavior (does it pass the right system prompt,
history, and retrieved context?) is testable: if no context was
retrieved, it returns a fixed "no supporting knowledge" answer (mirroring
the real policy an actual LLM is instructed to follow); otherwise it
echoes back a deterministic response that includes the last user message
and a snippet of the context, so tests can assert the right things reached
the provider.
"""

from app.providers.llm.base import LLMMessage, LLMProvider, LLMProviderError, LLMResponse

NO_CONTEXT_RESPONSE = (
    "I don't have that information in my available company information. "
    "I can connect you to a staff member if you'd like."
)


class MockLLMProvider(LLMProvider):
    def __init__(self, *, simulate_failure: bool = False) -> None:
        self._simulate_failure = simulate_failure
        # Recorded for test assertions.
        self.last_system_prompt: str | None = None
        self.last_history: list[LLMMessage] | None = None
        self.last_retrieved_context: str | None = None

    @property
    def provider_name(self) -> str:
        return "Mock"

    async def generate_response(
        self,
        *,
        system_prompt: str,
        history: list[LLMMessage],
        retrieved_context: str | None,
    ) -> LLMResponse:
        self.last_system_prompt = system_prompt
        self.last_history = history
        self.last_retrieved_context = retrieved_context

        if self._simulate_failure:
            raise LLMProviderError("Simulated LLM provider failure")

        if not history:
            raise LLMProviderError("Cannot generate a response with empty conversation history")

        last_user_message = next((m.content for m in reversed(history) if m.role == "user"), "")

        if not retrieved_context:
            return LLMResponse(text=NO_CONTEXT_RESPONSE, finish_reason="stop")

        snippet = retrieved_context[:120].strip()
        return LLMResponse(
            text=f"Based on the available information: {snippet}... (regarding: {last_user_message})",
            finish_reason="stop",
        )
