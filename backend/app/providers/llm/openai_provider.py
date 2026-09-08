"""OpenAI chat completion LLM provider.

Only instantiated (and only ever calls the API) when LLM_PROVIDER is
explicitly set to "openai" and LLM_API_KEY is configured — see
app.providers.llm.factory. No network call at import time; `openai` is
imported lazily so the rest of the app works without it installed.
"""

import asyncio

from app.providers.llm.base import LLMMessage, LLMProvider, LLMProviderError, LLMResponse

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAILLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
        max_tokens: int = 400,
    ) -> None:
        if not api_key:
            raise LLMProviderError("LLM_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise LLMProviderError(
                    "LLM_PROVIDER=openai requires the 'openai' package "
                    "(pip install openai) — not installed by default."
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def generate_response(
        self,
        *,
        system_prompt: str,
        history: list[LLMMessage],
        retrieved_context: str | None,
    ) -> LLMResponse:
        if not history:
            raise LLMProviderError("Cannot generate a response with empty conversation history")

        client = self._get_client()

        full_system_prompt = system_prompt
        if retrieved_context:
            full_system_prompt = f"{system_prompt}\n\nRelevant knowledge base excerpts:\n{retrieved_context}"

        messages = [{"role": "system", "content": full_system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in history]

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    max_tokens=self._max_tokens,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise LLMProviderError(f"OpenAI LLM request timed out after {self._timeout_seconds}s") from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            raise LLMProviderError(f"OpenAI LLM request failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "").strip() if choice else ""
        if not text:
            raise LLMProviderError("OpenAI LLM returned an empty response")

        return LLMResponse(text=text, finish_reason=choice.finish_reason if choice else None)
