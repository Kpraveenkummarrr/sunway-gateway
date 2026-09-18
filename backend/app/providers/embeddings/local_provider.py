"""Opt-in, CPU-only multilingual embeddings; never downloads during a call.

384 dimensions are zero-padded to the existing 1536 column. This preserves
cosine similarity exactly; it does NOT make this space compatible with
OpenAI/mock vectors. The artifact hash is part of the index identity.
"""
import asyncio
import hashlib
import re
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.providers.embeddings.base import EmbeddingProvider, EmbeddingProviderError

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class LocalEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_path: str, model_sha256: str, threads: int = 2):
        if not Path(model_path).is_dir() or not re.fullmatch(r"[a-f0-9]{64}", model_sha256):
            raise EmbeddingProviderError("Local embeddings require an existing model directory and its SHA256")
        if threads not in (1, 2, 3, 4):
            raise EmbeddingProviderError("Local embedding threads must be between 1 and 4")
        self._path, self._sha, self._threads = model_path, model_sha256, threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def dimensions(self) -> int:
        return 1536

    @property
    def embedding_space(self) -> str:
        return f"local:{MODEL}:{self._sha}:pad384to1536:v1"

    def _load(self):
        if self._model is None:
            from app.services.hardware_check import detect_hardware
            report = detect_hardware(path_for_disk=Path(self._path))
            if report.available_ram_mb is None or report.available_ram_mb < 1500:
                raise EmbeddingProviderError("Local embedding load requires at least 1500 MB available RAM")
            artifact = Path(self._path) / "model_optimized.onnx"
            digest = hashlib.sha256()
            with artifact.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != self._sha:
                raise EmbeddingProviderError("Local embedding artifact SHA256 mismatch; re-verify before indexing")
            from fastembed import TextEmbedding
            self._model = TextEmbedding(
                model_name=MODEL, specific_model_path=self._path,
                local_files_only=True, threads=self._threads,
                providers=["CPUExecutionProvider"],
            )
        return self._model

    def _embed(self, texts):
        # Thread lock remains held if a caller cancels its async wait. A second
        # request cannot start another ONNX inference/load over the same model.
        with self._lock:
            vectors = list(self._load().embed(texts, batch_size=8, parallel=None))
            if len(vectors) != len(texts):
                raise EmbeddingProviderError("Local model returned the wrong number of vectors")
            result = []
            for vector in vectors:
                value = np.asarray(vector, dtype=np.float64)
                if value.shape != (384,) or not np.isfinite(value).all() or np.linalg.norm(value) == 0:
                    raise EmbeddingProviderError("Local model returned an invalid embedding")
                value /= np.linalg.norm(value)
                result.append(np.pad(value, (0, 1152)).tolist())
            return result

    async def embed(self, texts):
        if not texts:
            return []
        try:
            return await asyncio.to_thread(self._embed, texts)
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            # Never leak filesystem paths/provider data in an API response.
            raise EmbeddingProviderError(f"Local embedding failed ({type(exc).__name__}); check installation/artifacts") from None


@lru_cache(maxsize=1)
def get_local_provider(model_path: str, model_sha256: str, threads: int):
    return LocalEmbeddingProvider(model_path, model_sha256, threads)
