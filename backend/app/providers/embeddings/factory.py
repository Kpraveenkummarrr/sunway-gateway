from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError
from app.providers.embeddings.mock import MockEmbeddingProvider


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Selects the embedding provider from RAG_EMBEDDING_PROVIDER.

    Deliberately has no implicit fallback to the mock provider for real
    ingestion — "mock" must be selected explicitly (tests/dev only) so a
    misconfigured deployment fails loudly instead of silently writing
    meaningless vectors into the real knowledge base.
    """
    provider_name = (settings.rag_embedding_provider or "").strip().lower()

    if provider_name == "mock":
        return MockEmbeddingProvider(dimensions=settings.rag_embedding_dimensions)

    if provider_name == "openai":
        from app.providers.embeddings.openai_provider import OpenAIEmbeddingProvider

        if not settings.rag_embedding_api_key:
            raise EmbeddingProviderError(
                "RAG_EMBEDDING_PROVIDER=openai but RAG_EMBEDDING_API_KEY is not set"
            )
        return OpenAIEmbeddingProvider(
            api_key=settings.rag_embedding_api_key,
            model=settings.rag_embedding_model or "text-embedding-3-small",
            dimensions=settings.rag_embedding_dimensions,
        )

    raise EmbeddingProviderError(
        "No embedding provider configured. Set RAG_EMBEDDING_PROVIDER to "
        "'openai' (with RAG_EMBEDDING_API_KEY) for real ingestion, or "
        "'mock' for local development/testing only."
    )
