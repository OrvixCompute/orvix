"""Unit tests for OrvixEmbeddingEngine with sentence-transformers mocked."""

from __future__ import annotations

import sys
import types

import pytest

from orvix_node.inference.base import EmbeddingRequest, EmbeddingResult
from orvix_node.inference.embedding import OrvixEmbeddingEngine


class _FakeModel:
    def __init__(self, name, device=None, cache_folder=None, dim=768):
        self.name = name
        self.device = device
        self.cache_folder = cache_folder
        self._dim = dim
        self.encode_kwargs = None

    def get_sentence_embedding_dimension(self):
        return self._dim

    def encode(self, inputs, **kwargs):
        self.encode_kwargs = kwargs
        return [[0.1] * self._dim for _ in inputs]


def _install(monkeypatch, dim=768, model_cls=None):
    holder = {}

    def _factory(name, device=None, cache_folder=None):
        m = (model_cls or _FakeModel)(name, device, cache_folder, dim)
        holder["model"] = m
        return m

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = _factory
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    return holder


@pytest.mark.asyncio
async def test_load_uses_cpu_by_default_and_is_idempotent(monkeypatch):
    """CPU by default is the whole point — the GPU is the contended resource."""
    holder = _install(monkeypatch)
    engine = OrvixEmbeddingEngine()

    await engine.load()
    assert await engine.is_loaded() is True
    assert holder["model"].device == "cpu"
    assert holder["model"].name == "BAAI/bge-base-en-v1.5"

    first = holder["model"]
    await engine.load()
    assert holder["model"] is first, "second load must not rebuild the model"


@pytest.mark.asyncio
async def test_dimensions_follow_the_loaded_model(monkeypatch):
    """A configured override may be a different width than the class default.

    Advertising the default anyway would have callers size a vector store that
    rejects the first insert.
    """
    _install(monkeypatch, dim=1024)
    engine = OrvixEmbeddingEngine(model="some/other-model")
    await engine.load()
    assert engine.dimensions == 1024


@pytest.mark.asyncio
async def test_embed_returns_one_vector_per_input_in_order(monkeypatch):
    _install(monkeypatch, dim=4)
    engine = OrvixEmbeddingEngine()
    await engine.load()

    result = await engine.embed(EmbeddingRequest(input=["a", "b", "c"]))

    assert isinstance(result, EmbeddingResult)
    assert len(result.embeddings) == 3
    assert all(len(v) == 4 for v in result.embeddings)
    assert result.metadata["count"] == 3
    assert result.metadata["dimensions"] == 4


@pytest.mark.asyncio
async def test_embed_normalizes(monkeypatch):
    """Callers compare with cosine similarity; unnormalized vectors give
    subtly wrong neighbours in every store that assumes unit length."""
    holder = _install(monkeypatch, dim=4)
    engine = OrvixEmbeddingEngine()
    await engine.load()
    await engine.embed(EmbeddingRequest(input=["x"]))
    assert holder["model"].encode_kwargs["normalize_embeddings"] is True


@pytest.mark.asyncio
async def test_count_mismatch_raises_rather_than_misaligning(monkeypatch):
    """A short result would silently pair vectors with the wrong inputs."""

    class _ShortModel(_FakeModel):
        def encode(self, inputs, **kwargs):
            return [[0.1] * self._dim]  # one vector regardless of input count

    _install(monkeypatch, dim=4, model_cls=_ShortModel)
    engine = OrvixEmbeddingEngine()
    await engine.load()

    with pytest.raises(RuntimeError, match="count mismatch"):
        await engine.embed(EmbeddingRequest(input=["a", "b", "c"]))


@pytest.mark.asyncio
async def test_embed_before_load_raises(monkeypatch):
    _install(monkeypatch)
    engine = OrvixEmbeddingEngine()
    with pytest.raises(RuntimeError, match="not loaded"):
        await engine.embed(EmbeddingRequest(input=["a"]))


@pytest.mark.asyncio
async def test_unload_is_idempotent(monkeypatch):
    _install(monkeypatch)
    engine = OrvixEmbeddingEngine()
    await engine.load()
    await engine.unload()
    assert await engine.is_loaded() is False
    await engine.unload()


def test_engine_metadata():
    assert OrvixEmbeddingEngine.engine_type == "embedding"
    assert "orvix-embed-1" in OrvixEmbeddingEngine.supported_models
    # Small enough to co-reside with a chat model — that is why it may stay on.
    assert OrvixEmbeddingEngine.required_vram_gb < 1
