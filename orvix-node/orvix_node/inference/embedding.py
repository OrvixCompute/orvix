"""Orvix's default embedding engine — sentence-transformers.

Defaults to `BAAI/bge-base-en-v1.5` (768 dims): small, well-understood, and
strong enough that a vector store built on it is not a compromise. It runs on
CPU in tens of milliseconds per input, which is the point — a node whose GPU is
busy with chat can still serve embeddings, so this capability does not compete
for the resource everything else is queuing for.

`sentence_transformers` is imported lazily inside load()/embed(), mirroring the
image and video engines, so importing this module never requires the `embed`
extra to be installed.

Config (env / constructor):
  - ORVIX_NODE_EMBED_MODEL      model repo (default BAAI/bge-base-en-v1.5)
  - ORVIX_NODE_EMBED_DEVICE     "cpu" (default) or "cuda"
  - ORVIX_NODE_EMBED_CACHE_DIR  local model cache (default ./models/orvix-embed)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from orvix_node.inference.base import EmbeddingEngine, EmbeddingRequest, EmbeddingResult
from orvix_node.logger import logger

_DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
_DEFAULT_CACHE_DIR = "./models/orvix-embed"
_DEFAULT_DIMENSIONS = 768


class OrvixEmbeddingEngine(EmbeddingEngine):
    # Small enough that co-residence with a chat model is the normal case, not
    # a risk — which is why this one is safe to leave resident.
    required_vram_gb = 0.5
    supported_models = ["orvix-embed-1"]
    dimensions = _DEFAULT_DIMENSIONS

    def __init__(
        self,
        model: Optional[str] = None,
        device: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.model = model or os.environ.get("ORVIX_NODE_EMBED_MODEL") or _DEFAULT_MODEL
        # CPU by default: the GPU is the contended resource and this model does
        # not need it. An operator with headroom can override.
        self.device = device or os.environ.get("ORVIX_NODE_EMBED_DEVICE") or "cpu"
        self.cache_dir = (
            cache_dir or os.environ.get("ORVIX_NODE_EMBED_CACHE_DIR") or _DEFAULT_CACHE_DIR
        )
        self._model = None

    async def load(self, model_id: str = "orvix-embed-1") -> None:
        if self._model is not None:
            return
        loop = asyncio.get_event_loop()
        self._model = await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self):
        from sentence_transformers import SentenceTransformer

        logger.info(
            "Loading embedding model {} on {} (cache={})...",
            self.model,
            self.device,
            self.cache_dir,
        )
        model = SentenceTransformer(
            self.model, device=self.device, cache_folder=self.cache_dir
        )
        # Trust the loaded model over the class default: a configured override
        # may have a different width, and advertising the wrong one would have
        # callers build vector stores that reject the first insert.
        try:
            dim = model.get_sentence_embedding_dimension()
            if dim:
                self.dimensions = int(dim)
        except Exception:  # noqa: BLE001 — advertisement only, never fatal
            logger.warning("Could not read embedding dimension; keeping {}", self.dimensions)
        logger.info("Embedding model loaded ({} dims).", self.dimensions)
        return model

    async def unload(self) -> None:
        if self._model is None:
            return
        del self._model
        self._model = None
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — best-effort, and torch may be absent
            pass
        logger.info("Embedding model unloaded.")

    async def is_loaded(self) -> bool:
        return self._model is not None

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        if self._model is None:
            raise RuntimeError("Embedding engine not loaded — call load() first")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_sync, request)

    def _embed_sync(self, request: EmbeddingRequest) -> EmbeddingResult:
        start = time.time()
        # normalize_embeddings: callers compare with cosine similarity, and unit
        # vectors make that a dot product. Returning unnormalized vectors would
        # leave every downstream store to do it — or, more often, not do it and
        # get subtly wrong neighbours.
        raw = self._model.encode(
            request.input, normalize_embeddings=True, convert_to_numpy=True
        )
        vectors = [[float(x) for x in row] for row in raw]
        elapsed = time.time() - start

        # Order and count are the contract; a mismatch here would misalign every
        # vector with its input, silently, in the caller's database.
        if len(vectors) != len(request.input):
            raise RuntimeError(
                f"Embedding count mismatch: {len(vectors)} vectors for "
                f"{len(request.input)} inputs"
            )

        logger.info(
            "Embedded {} input(s) in {:.0f} ms ({} dims)",
            len(vectors),
            elapsed * 1000,
            len(vectors[0]) if vectors else 0,
        )
        return EmbeddingResult(
            embeddings=vectors,
            metadata={
                "model": self.model,
                "dimensions": len(vectors[0]) if vectors else self.dimensions,
                "device": self.device,
                "normalized": True,
                "count": len(vectors),
                "generation_time_ms": round(elapsed * 1000, 1),
            },
        )
