"""OpenAI chat completion LLM provider — also serves any OpenAI-compatible
endpoint via `base_url` (used for Google Gemini, LLM_PROVIDER=gemini).

Only instantiated (and only ever calls the API) when LLM_PROVIDER is
explicitly set to "openai" or "gemini" with its API key configured — see
app.providers.llm.factory. No network call at import time; `openai` is
imported lazily so the rest of the app works without it installed.
"""

import asyncio

from app.providers.llm.base import LLMMessage, LLMProvider, LLMProviderError, LLMResponse
from app.providers.llm.prompt_context import augment_system_prompt_with_context

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAILLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
        max_tokens: int = 400,
        *,
        base_url: str | None = None,
        provider_name: str = "OpenAI",
        reasoning_effort: str | None = None,
    ) -> None:
        if not api_key:
            raise LLMProviderError(f"{provider_name} LLM API key is not set")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._base_url = base_url
        self._provider_name = provider_name
        # Only sent when configured: OpenAI's non-reasoning models (e.g.
        # gpt-4o-mini) reject the parameter.
        self._reasoning_effort = reasoning_effort
        self._client = None

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise LLMProviderError(
                    f"The {self._provider_name} LLM provider requires the 'openai' package "
                    "(pip install openai)."
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
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

        full_system_prompt = augment_system_prompt_with_context(system_prompt, retrieved_context)

        messages = [{"role": "system", "content": full_system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in history]

        request = {"model": self._model, "messages": messages, "max_tokens": self._max_tokens}
        if self._reasoning_effort:
            request["reasoning_effort"] = self._reasoning_effort

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(**request),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise LLMProviderError(
                f"{self._provider_name} LLM request timed out after {self._timeout_seconds}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            detail = str(exc).replace(self._api_key, "<redacted>")
            raise LLMProviderError(f"{self._provider_name} LLM request failed: {detail}") from exc

        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "").strip() if choice else ""
        if not text:
            finish_reason = choice.finish_reason if choice else None
            raise LLMProviderError(
                f"{self._provider_name} LLM returned an empty response (finish_reason={finish_reason})"
            )

        return LLMResponse(text=text, finish_reason=choice.finish_reason if choice else None)
