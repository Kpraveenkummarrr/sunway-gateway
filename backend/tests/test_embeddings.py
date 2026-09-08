import math

import pytest

from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.factory import get_embedding_provider
from app.providers.embeddings.mock import MockEmbeddingProvider


@pytest.mark.asyncio
async def test_mock_provider_returns_correct_dimensions() -> None:
    provider = MockEmbeddingProvider(dimensions=1536)
    vectors = await provider.embed(["hello world"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 1536


@pytest.mark.asyncio
async def test_mock_provider_is_deterministic() -> None:
    provider = MockEmbeddingProvider(dimensions=64)
    v1 = await provider.embed_one("the quick brown fox")
    v2 = await provider.embed_one("the quick brown fox")
    assert v1 == v2


@pytest.mark.asyncio
async def test_mock_provider_differs_for_different_text() -> None:
    provider = MockEmbeddingProvider(dimensions=64)
    v1 = await provider.embed_one("apples")
    v2 = await provider.embed_one("oranges")
    assert v1 != v2


@pytest.mark.asyncio
async def test_mock_provider_returns_unit_vectors() -> None:
    provider = MockEmbeddingProvider(dimensions=64)
    vector = await provider.embed_one("normalize me")
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_factory_raises_clear_error_when_unconfigured() -> None:
    settings = Settings(rag_embedding_provider="")
    with pytest.raises(EmbeddingProviderError):
        get_embedding_provider(settings)


def test_factory_returns_mock_provider_when_explicitly_selected() -> None:
    settings = Settings(rag_embedding_provider="mock", rag_embedding_dimensions=128)
    provider = get_embedding_provider(settings)
    assert isinstance(provider, MockEmbeddingProvider)
    assert provider.dimensions == 128


def test_factory_raises_when_openai_selected_without_api_key() -> None:
    settings = Settings(rag_embedding_provider="openai", rag_embedding_api_key="")
    with pytest.raises(EmbeddingProviderError):
        get_embedding_provider(settings)


def test_factory_rejects_unknown_provider_name() -> None:
    settings = Settings(rag_embedding_provider="some-unsupported-vendor")
    with pytest.raises(EmbeddingProviderError):
        get_embedding_provider(settings)
