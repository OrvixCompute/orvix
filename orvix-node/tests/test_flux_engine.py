"""Unit tests for FluxEngine with diffusers/torch mocked (no GPU, no real deps).

FluxEngine imports torch/diffusers lazily inside load()/infer(), so we inject
fake modules into sys.modules before exercising those paths.
"""

from __future__ import annotations

import sys
import types

import pytest

from orvix_node.inference.base import ImageRequest, ImageResult
from orvix_node.inference.flux import FluxEngine


class _FakeImage:
    def save(self, buf, format="PNG"):  # noqa: A002 — mirror PIL's signature
        buf.write(b"PNGDATA")


class _FakePipe:
    def __init__(self):
        self.calls = []
        self.moved_to = None

    def to(self, device):
        self.moved_to = device
        return self

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(images=[_FakeImage()])


def _install_fakes(monkeypatch, pipe=None):
    """Register fake torch + diffusers modules; return (pipe, captured, counter)."""
    pipe = pipe or _FakePipe()
    captured: dict = {}
    counter = {"from_pretrained": 0}

    fake_torch = types.ModuleType("torch")
    fake_torch.bfloat16 = "bf16"

    class _Gen:
        def __init__(self, device):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    fake_torch.Generator = _Gen
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: False, empty_cache=lambda: None
    )

    def _from_pretrained(model_id, torch_dtype=None, cache_dir=None):
        counter["from_pretrained"] += 1
        captured.update(model_id=model_id, torch_dtype=torch_dtype, cache_dir=cache_dir)
        return pipe

    fake_diffusers = types.ModuleType("diffusers")
    fake_diffusers.FluxPipeline = types.SimpleNamespace(from_pretrained=_from_pretrained)

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers)
    return pipe, captured, counter


def test_engine_metadata():
    # Class-level metadata must not require importing torch/diffusers.
    assert FluxEngine.engine_type == "image"
    assert FluxEngine.required_vram_gb == 16.0
    assert FluxEngine.supported_models == ["flux-schnell"]


async def test_infer_before_load_raises():
    engine = FluxEngine()
    with pytest.raises(RuntimeError, match="not loaded"):
        await engine.infer(ImageRequest(prompt="hi"))


async def test_load_then_infer(monkeypatch):
    pipe, captured, counter = _install_fakes(monkeypatch)
    engine = FluxEngine(model_id="acme/flux", cache_dir="/tmp/flux", device="cuda")

    assert await engine.is_loaded() is False
    await engine.load()
    assert await engine.is_loaded() is True
    assert captured["model_id"] == "acme/flux"
    assert captured["cache_dir"] == "/tmp/flux"
    assert pipe.moved_to == "cuda"

    result = await engine.infer(
        ImageRequest(prompt="a cat", width=512, height=512, num_inference_steps=3, seed=7)
    )
    assert isinstance(result, ImageResult)
    assert result.png_bytes == b"PNGDATA"
    assert result.metadata["seed"] == 7
    assert result.metadata["steps"] == 3
    assert result.metadata["width"] == 512
    assert result.metadata["model"] == "acme/flux"
    assert isinstance(result.metadata["generation_time_seconds"], float)

    # The prompt + params were forwarded to the pipeline.
    call = pipe.calls[0]
    assert call["prompt"] == "a cat"
    assert call["width"] == 512 and call["height"] == 512
    assert call["num_inference_steps"] == 3
    assert call["guidance_scale"] == 0.0
    assert call["generator"] is not None  # seed provided


async def test_load_is_idempotent(monkeypatch):
    _, _, counter = _install_fakes(monkeypatch)
    engine = FluxEngine()
    await engine.load()
    await engine.load()
    assert counter["from_pretrained"] == 1  # second load is a no-op


async def test_unload_frees_and_is_idempotent(monkeypatch):
    _install_fakes(monkeypatch)
    engine = FluxEngine()
    await engine.load()
    assert await engine.is_loaded() is True
    await engine.unload()
    assert await engine.is_loaded() is False
    await engine.unload()  # no-op, must not raise
    assert await engine.is_loaded() is False


async def test_infer_without_seed_uses_no_generator(monkeypatch):
    pipe, _, _ = _install_fakes(monkeypatch)
    engine = FluxEngine()
    await engine.load()
    await engine.infer(ImageRequest(prompt="no seed"))
    assert pipe.calls[0]["generator"] is None
