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
        if settings.app_env.lower() not in ("development", "test"):
            raise EmbeddingProviderError("Mock embeddings are not allowed outside development/test")
        return MockEmbeddingProvider(dimensions=settings.rag_embedding_dimensions)

    if provider_name == "local":
        from app.providers.embeddings.local_provider import get_local_provider
        return get_local_provider(settings.rag_local_model_path, settings.rag_local_model_sha256, settings.rag_local_threads)

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
            timeout_seconds=settings.provider_timeout_seconds,
        )

    raise EmbeddingProviderError(
        "No embedding provider configured. Set RAG_EMBEDDING_PROVIDER to "
        "'openai' (with RAG_EMBEDDING_API_KEY) for real ingestion, or "
        "'mock' for local development/testing only."
    )
