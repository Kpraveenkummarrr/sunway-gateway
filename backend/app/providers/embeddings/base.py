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

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input text in
        the same order."""

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]
