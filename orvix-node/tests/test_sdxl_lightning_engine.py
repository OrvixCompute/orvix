"""Unit tests for SDXLLightningEngine with torch/diffusers/hub deps mocked (no
GPU, no real deps).

SDXLLightningEngine imports its heavy deps lazily inside load()/infer(), so we
inject fake modules into sys.modules before exercising those paths — mirrors
test_flux_engine.py.
"""

from __future__ import annotations

import sys
import types

import pytest

from orvix_node.inference.base import ImageRequest, ImageResult
from orvix_node.inference.sdxl_lightning import SDXLLightningEngine


class _FakeImage:
    def save(self, buf, format="PNG"):  # noqa: A002 — mirror PIL's signature
        buf.write(b"PNGDATA")


class _FakeUnet:
    def __init__(self):
        self.moved_to = None
        self.moved_dtype = None
        self.loaded_state = None

    def to(self, device, dtype=None):
        self.moved_to = device
        self.moved_dtype = dtype
        return self

    def load_state_dict(self, state_dict):
        self.loaded_state = state_dict


class _FakeScheduler:
    def __init__(self, config=None, timestep_spacing=None):
        self.config = config
        self.timestep_spacing = timestep_spacing


class _FakePipe:
    def __init__(self):
        self.calls = []
        self.moved_to = None
        self.scheduler = types.SimpleNamespace(config={"orig": True})
        self.unet = None

    def to(self, device):
        self.moved_to = device
        return self

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(images=[_FakeImage()])


def _install_fakes(monkeypatch, pipe=None, unet=None):
    """Register fake torch/diffusers/huggingface_hub/safetensors modules."""
    pipe = pipe or _FakePipe()
    unet = unet or _FakeUnet()
    captured: dict = {}
    counter = {"from_pretrained": 0}

    fake_torch = types.ModuleType("torch")
    fake_torch.float16 = "fp16"

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

    def _unet_from_config(base_model, subfolder=None, cache_dir=None):
        captured.update(unet_base_model=base_model, unet_subfolder=subfolder, unet_cache_dir=cache_dir)
        return unet

    def _pipe_from_pretrained(base_model, unet=None, torch_dtype=None, variant=None, cache_dir=None):
        counter["from_pretrained"] += 1
        captured.update(
            base_model=base_model,
            unet=unet,
            torch_dtype=torch_dtype,
            variant=variant,
            cache_dir=cache_dir,
        )
        pipe.unet = unet
        return pipe

    def _scheduler_from_config(config, timestep_spacing=None):
        captured.update(scheduler_config=config, timestep_spacing=timestep_spacing)
        return _FakeScheduler(config, timestep_spacing)

    fake_diffusers = types.ModuleType("diffusers")
    fake_diffusers.UNet2DConditionModel = types.SimpleNamespace(from_config=_unet_from_config)
    fake_diffusers.StableDiffusionXLPipeline = types.SimpleNamespace(
        from_pretrained=_pipe_from_pretrained
    )
    fake_diffusers.EulerDiscreteScheduler = types.SimpleNamespace(
        from_config=_scheduler_from_config
    )

    def _hf_hub_download(repo_id, filename, cache_dir=None):
        captured.update(hf_repo_id=repo_id, hf_filename=filename, hf_cache_dir=cache_dir)
        return "/fake/cache/sdxl_lightning_4step_unet.safetensors"

    fake_hfhub = types.ModuleType("huggingface_hub")
    fake_hfhub.hf_hub_download = _hf_hub_download

    fake_state_dict = {"fake": "weights"}

    def _load_file(path, device=None):
        captured.update(loaded_path=path, loaded_device=device)
        return fake_state_dict

    fake_safetensors = types.ModuleType("safetensors")
    fake_safetensors_torch = types.ModuleType("safetensors.torch")
    fake_safetensors_torch.load_file = _load_file
    fake_safetensors.torch = fake_safetensors_torch

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hfhub)
    monkeypatch.setitem(sys.modules, "safetensors", fake_safetensors)
    monkeypatch.setitem(sys.modules, "safetensors.torch", fake_safetensors_torch)
    return pipe, unet, captured, counter, fake_state_dict


def test_engine_metadata():
    # Class-level metadata must not require importing torch/diffusers.
    assert SDXLLightningEngine.engine_type == "image"
    assert SDXLLightningEngine.required_vram_gb == 10.0
    assert SDXLLightningEngine.supported_models == ["sdxl-lightning"]


async def test_infer_before_load_raises():
    engine = SDXLLightningEngine()
    with pytest.raises(RuntimeError, match="not loaded"):
        await engine.infer(ImageRequest(prompt="hi"))


async def test_load_then_infer(monkeypatch):
    pipe, unet, captured, counter, fake_state_dict = _install_fakes(monkeypatch)
    engine = SDXLLightningEngine(
        base_model="acme/sdxl-base", cache_dir="/tmp/sdxl", device="cuda"
    )

    assert await engine.is_loaded() is False
    await engine.load()
    assert await engine.is_loaded() is True

    assert captured["unet_base_model"] == "acme/sdxl-base"
    assert captured["unet_subfolder"] == "unet"
    assert captured["unet_cache_dir"] == "/tmp/sdxl"
    assert captured["hf_repo_id"] == "ByteDance/SDXL-Lightning"
    assert captured["hf_filename"] == "sdxl_lightning_4step_unet.safetensors"
    assert captured["hf_cache_dir"] == "/tmp/sdxl"
    assert unet.loaded_state == fake_state_dict
    assert captured["base_model"] == "acme/sdxl-base"
    assert captured["cache_dir"] == "/tmp/sdxl"
    assert captured["timestep_spacing"] == "trailing"
    assert pipe.moved_to == "cuda"

    result = await engine.infer(
        ImageRequest(prompt="a cat", width=512, height=512, num_inference_steps=1, seed=7)
    )
    assert isinstance(result, ImageResult)
    assert result.png_bytes == b"PNGDATA"
    assert result.metadata["seed"] == 7
    # Steps + guidance are fixed for the Lightning 4-step checkpoint, regardless
    # of what the request asked for.
    assert result.metadata["steps"] == 4
    assert result.metadata["guidance_scale"] == 0.0
    assert result.metadata["width"] == 512
    assert result.metadata["model"] == "sdxl-lightning"
    assert isinstance(result.metadata["generation_time_seconds"], float)

    call = pipe.calls[0]
    assert call["prompt"] == "a cat"
    assert call["width"] == 512 and call["height"] == 512
    assert call["num_inference_steps"] == 4
    assert call["guidance_scale"] == 0.0
    assert call["generator"] is not None  # seed provided


async def test_load_is_idempotent(monkeypatch):
    _, _, _, counter, _ = _install_fakes(monkeypatch)
    engine = SDXLLightningEngine()
    await engine.load()
    await engine.load()
    assert counter["from_pretrained"] == 1  # second load is a no-op


async def test_unload_frees_and_is_idempotent(monkeypatch):
    _install_fakes(monkeypatch)
    engine = SDXLLightningEngine()
    await engine.load()
    assert await engine.is_loaded() is True
    await engine.unload()
    assert await engine.is_loaded() is False
    await engine.unload()  # no-op, must not raise
    assert await engine.is_loaded() is False


async def test_infer_without_seed_uses_no_generator(monkeypatch):
    pipe, _, _, _, _ = _install_fakes(monkeypatch)
    engine = SDXLLightningEngine()
    await engine.load()
    await engine.infer(ImageRequest(prompt="no seed"))
    assert pipe.calls[0]["generator"] is None
