"""Deterministic mock embedding provider.

For automated tests and local development without a paid API key. Never
insert vectors from this provider into a real production knowledge base —
they encode no real semantic meaning, only a hash-derived pseudo-random
direction, so similarity search results are deterministic-but-meaningless.
"""

import hashlib
import math
import struct

from app.providers.embeddings.base import EmbeddingProvider


class MockEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def embedding_space(self) -> str:
        return f"mock:{self._dimensions}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        # Expand a SHA-256 digest of the text into `dimensions` floats via
        # repeated re-hashing, then normalize to a unit vector so cosine
        # similarity/distance behaves sensibly in tests.
        values: list[float] = []
        seed = text.encode("utf-8")
        counter = 0
        while len(values) < self._dimensions:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(digest) - 1, 2):
                if len(values) >= self._dimensions:
                    break
                raw = struct.unpack(">H", digest[i : i + 2])[0]
                values.append((raw / 65535.0) * 2 - 1)  # scale to [-1, 1]
            counter += 1

        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]
