"""Unit tests for OrvixVideoEngine with torch/diffusers mocked (no GPU, no deps).

The engine imports its heavy deps lazily inside load()/infer(), so fake modules
are injected into sys.modules before those paths run — mirrors
test_orvix_image_engine.py.
"""

from __future__ import annotations

import sys
import types

import pytest

from orvix_node.inference.base import VideoEngine, VideoRequest, VideoResult
from orvix_node.inference.video import OrvixVideoEngine


class _FakeOutput:
    def __init__(self):
        # diffusers video pipelines return a batch of frame lists.
        self.frames = [["frame0", "frame1"]]


class _FakePipe:
    def __init__(self):
        self.moved_to = None
        self.call_kwargs = None

    def to(self, device):
        self.moved_to = device
        return self

    def __call__(self, **kwargs):
        self.call_kwargs = kwargs
        return _FakeOutput()


def _install_fakes(monkeypatch, pipe=None, has_pipeline=True):
    pipe = pipe or _FakePipe()

    torch = types.ModuleType("torch")
    torch.bfloat16 = "bfloat16"

    class _Gen:
        def __init__(self, device):
            self.device = device
            self.seed = None

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch.Generator = _Gen
    cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.cuda = cuda

    diffusers = types.ModuleType("diffusers")
    if has_pipeline:
        class _Pipeline:
            @staticmethod
            def from_pretrained(model, torch_dtype=None, cache_dir=None):
                pipe.from_pretrained_args = (model, torch_dtype, cache_dir)
                return pipe

        diffusers.LTXPipeline = _Pipeline

    written = {}

    utils = types.ModuleType("diffusers.utils")

    def _export_to_video(frames, path, fps=None):
        written["frames"] = frames
        written["fps"] = fps
        with open(path, "wb") as fh:
            fh.write(b"MP4DATA")

    utils.export_to_video = _export_to_video
    diffusers.utils = utils

    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "diffusers.utils", utils)
    return pipe, written


# --- frame normalization ---------------------------------------------------
# Latent video pipelines compress time by 8 and silently round the frame count.
# Rounding here is what keeps the reported duration true.
@pytest.mark.parametrize(
    "asked,expected",
    [(9, 9), (10, 17), (16, 17), (17, 17), (97, 97), (100, 105), (1, 9)],
)
def test_normalize_frames_rounds_up_to_8k_plus_1(asked, expected):
    assert VideoEngine.normalize_frames(asked) == expected
    assert (expected - 1) % 8 == 0


@pytest.mark.asyncio
async def test_load_moves_pipeline_to_device_and_is_idempotent(monkeypatch):
    pipe, _ = _install_fakes(monkeypatch)
    engine = OrvixVideoEngine(device="cuda")

    await engine.load()
    assert await engine.is_loaded() is True
    assert pipe.moved_to == "cuda"
    assert pipe.from_pretrained_args[0] == "Lightricks/LTX-Video"

    # Second load must not rebuild the pipeline — reloading a video model is
    # minutes of GPU time, so an accidental repeat is expensive, not harmless.
    pipe.from_pretrained_args = None
    await engine.load()
    assert pipe.from_pretrained_args is None


@pytest.mark.asyncio
async def test_missing_pipeline_class_names_the_setting(monkeypatch):
    """A diffusers too old for the pipeline must say which knob to turn."""
    _install_fakes(monkeypatch, has_pipeline=False)
    engine = OrvixVideoEngine()

    with pytest.raises(RuntimeError) as exc:
        await engine.load()
    assert "LTXPipeline" in str(exc.value)
    assert "ORVIX_NODE_VIDEO_PIPELINE" in str(exc.value)


@pytest.mark.asyncio
async def test_infer_returns_mp4_bytes_and_honest_metadata(monkeypatch):
    pipe, written = _install_fakes(monkeypatch)
    engine = OrvixVideoEngine(device="cuda")
    await engine.load()

    # 100 is not a valid frame count; the pipeline would quietly produce 105.
    result = await engine.infer(
        VideoRequest(prompt="a fox in snow", num_frames=100, fps=24, seed=7)
    )

    assert isinstance(result, VideoResult)
    assert result.mp4_bytes == b"MP4DATA"
    # The pipeline was asked for the rounded count, not the raw one.
    assert pipe.call_kwargs["num_frames"] == 105
    assert written["fps"] == 24

    md = result.metadata
    assert md["num_frames"] == 105, "metadata must report what was produced"
    assert md["requested_frames"] == 100, "and what was asked for"
    # Duration follows the produced frames; reporting 100/24 here would be a lie
    # the caller cannot detect.
    assert md["duration_seconds"] == pytest.approx(105 / 24, abs=0.01)
    assert md["seed"] == 7
    assert md["model"] == "Lightricks/LTX-Video"


@pytest.mark.asyncio
async def test_infer_before_load_raises(monkeypatch):
    _install_fakes(monkeypatch)
    engine = OrvixVideoEngine()
    with pytest.raises(RuntimeError, match="not loaded"):
        await engine.infer(VideoRequest(prompt="x"))


@pytest.mark.asyncio
async def test_unload_frees_and_is_idempotent(monkeypatch):
    _install_fakes(monkeypatch)
    engine = OrvixVideoEngine()
    await engine.load()

    await engine.unload()
    assert await engine.is_loaded() is False
    await engine.unload()  # must not raise on a second call
    assert await engine.is_loaded() is False


def test_engine_declares_its_type_and_model():
    assert OrvixVideoEngine.engine_type == "video"
    assert "orvix-video-1" in OrvixVideoEngine.supported_models


def test_module_imports_without_gpu_deps():
    """Importing the engine must never require torch or diffusers.

    The node imports its engine modules at start-up; if this one pulled GPU deps
    eagerly, every chat-only node would need the `video` extra installed.
    """
    import importlib

    for dep in ("torch", "diffusers"):
        assert dep not in sys.modules or sys.modules[dep] is not None
    mod = importlib.import_module("orvix_node.inference.video")
    assert mod.OrvixVideoEngine is OrvixVideoEngine
