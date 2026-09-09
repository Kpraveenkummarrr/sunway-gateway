"""OpenAI embedding provider.

Only instantiated (and only ever calls the API) when RAG_EMBEDDING_PROVIDER
is explicitly set to "openai" and RAG_EMBEDDING_API_KEY is configured — see
app.providers.embeddings.factory. This module makes no network call at
import time; the `openai` package is imported lazily so the rest of the
app works fine without it installed.
"""

import asyncio

from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError

DEFAULT_MODEL = "text-embedding-3-small"  # 1536 dimensions, matches the pgvector column


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        dimensions: int = 1536,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise EmbeddingProviderError("RAG_EMBEDDING_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout_seconds = timeout_seconds
        self._client = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise EmbeddingProviderError(
                    "RAG_EMBEDDING_PROVIDER=openai requires the 'openai' package "
                    "(pip install openai) — not installed by default to keep the "
                    "base install lightweight."
                ) from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        try:
            response = await asyncio.wait_for(
                client.embeddings.create(model=self._model, input=texts, dimensions=self._dimensions),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EmbeddingProviderError(
                f"OpenAI embedding request timed out after {self._timeout_seconds}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - surface any provider error uniformly
            raise EmbeddingProviderError(f"OpenAI embedding request failed: {exc}") from exc
        return [item.embedding for item in response.data]
