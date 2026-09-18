import numpy as np
import pytest

from app.core.config import Settings
from app.providers.embeddings.base import EmbeddingProviderError
from app.providers.embeddings.factory import get_embedding_provider
from app.providers.embeddings.local_provider import LocalEmbeddingProvider


@pytest.mark.asyncio
async def test_local_padding_preserves_cosine_and_order(tmp_path, monkeypatch):
    provider = LocalEmbeddingProvider(str(tmp_path), "a" * 64)
    source = np.random.default_rng(42).normal(size=(2, 384))

    class Model:
        def embed(self, texts, **kwargs):
            assert texts == ["लम्पी रोग", "lumpy disease"]
            return source

    monkeypatch.setattr(provider, "_load", lambda: Model())
    result = np.array(await provider.embed(["लम्पी रोग", "lumpy disease"]))
    expected = np.dot(source[0], source[1]) / np.linalg.norm(source[0]) / np.linalg.norm(source[1])
    assert result.shape == (2, 1536)
    assert np.all(result[:, 384:] == 0)
    assert np.dot(result[0], result[1]) == pytest.approx(expected)
    assert "a" * 64 in provider.embedding_space


def test_mock_embeddings_fail_closed_in_production():
    with pytest.raises(EmbeddingProviderError, match="not allowed"):
        get_embedding_provider(Settings(_env_file=None, app_env="production", rag_embedding_provider="mock"))


def test_local_requires_verified_artifact(tmp_path):
    with pytest.raises(EmbeddingProviderError, match="SHA256"):
        LocalEmbeddingProvider(str(tmp_path), "")
