from abc import ABC, abstractmethod


class EmbeddingProviderError(Exception):
    """Raised when an embedding provider is misconfigured or a call fails."""


class EmbeddingProvider(ABC):
    """Abstraction over embedding vendors so the ingestion/search pipeline
    isn't hard-coded to one provider."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector length this provider produces. Must match the
        knowledge_chunks.embedding column dimension (see
        app.models.knowledge.EMBEDDING_DIM) or ingestion will reject it."""

    @property
    def embedding_space(self) -> str:
        """Stable, non-secret identity for the vector space being used.

        Documents and queries must share an embedding space. Providers can
        override this when model identity matters; the class/dimension
        fallback keeps third-party test providers backwards-compatible.
        """
        return f"{type(self).__module__}.{type(self).__qualname__}:{self.dimensions}"

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input text in
        the same order."""

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]
