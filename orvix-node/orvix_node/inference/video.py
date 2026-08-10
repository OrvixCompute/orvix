"""Orvix's default video engine — Diffusers-backed text-to-video.

Defaults to LTX-Video, chosen for the same reason `orvix_image` picked a
distilled checkpoint: it is the fastest credible option per clip, and clip time
is the constraint that decides whether video is viable on a shared card at all.
Both the repo and the pipeline class are overridable, so swapping in a different
model is configuration rather than a code change.

Heavy GPU dependencies (torch, diffusers) are imported lazily inside
load()/infer(), mirroring the image engines, so importing this module never
requires a GPU or the `video` extra to be installed.

Config (env / constructor):
  - ORVIX_NODE_VIDEO_MODEL      pipeline repo (default Lightricks/LTX-Video)
  - ORVIX_NODE_VIDEO_PIPELINE   diffusers pipeline class (default LTXPipeline)
  - ORVIX_NODE_VIDEO_CACHE_DIR  local model cache (default ./models/orvix-video)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from orvix_node.inference.base import VideoEngine, VideoRequest, VideoResult
from orvix_node.logger import logger

_DEFAULT_MODEL = "Lightricks/LTX-Video"
_DEFAULT_PIPELINE = "LTXPipeline"
_DEFAULT_CACHE_DIR = "./models/orvix-video"


class OrvixVideoEngine(VideoEngine):
    # Measured numbers do not exist yet for this engine on the network's
    # hardware. This is a placeholder large enough that the ModelManager will
    # not try to hold it resident beside a chat model on a 24 GB card, which is
    # the failure this number exists to prevent. Re-measure before trusting it.
    required_vram_gb = 20.0
    supported_models = ["orvix-video-1"]

    def __init__(
        self,
        model: Optional[str] = None,
        pipeline_class: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: str = "cuda",
    ) -> None:
        self.model = model or os.environ.get("ORVIX_NODE_VIDEO_MODEL") or _DEFAULT_MODEL
        self.pipeline_class = (
            pipeline_class
            or os.environ.get("ORVIX_NODE_VIDEO_PIPELINE")
            or _DEFAULT_PIPELINE
        )
        self.cache_dir = (
            cache_dir or os.environ.get("ORVIX_NODE_VIDEO_CACHE_DIR") or _DEFAULT_CACHE_DIR
        )
        self.device = device
        self._pipe = None

    async def load(self, model_id: str = "orvix-video-1") -> None:
        # ``model_id`` is the orchestrator-facing catalog id; the upstream repo
        # is fixed per instance, so the argument is accepted for interface
        # uniformity but not otherwise used.
        if self._pipe is not None:
            return
        # Run the slow synchronous load in a thread. The client's heartbeat runs
        # on this same event loop, and starving it reads to the orchestrator as
        # a dead node even though the node is fine — the mistake image already
        # had to fix.
        loop = asyncio.get_event_loop()
        self._pipe = await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self):
        import diffusers
        import torch

        pipeline_cls = getattr(diffusers, self.pipeline_class, None)
        if pipeline_cls is None:
            raise RuntimeError(
                f"diffusers has no pipeline {self.pipeline_class!r}. Set "
                "ORVIX_NODE_VIDEO_PIPELINE to a class this diffusers version "
                "provides, or upgrade the `video` extra."
            )

        logger.info(
            "Loading video model {} ({}) into VRAM (bf16, cache={})...",
            self.model,
            self.pipeline_class,
            self.cache_dir,
        )
        pipe = pipeline_cls.from_pretrained(
            self.model, torch_dtype=torch.bfloat16, cache_dir=self.cache_dir
        )
        pipe.to(self.device)
        logger.info("Video model loaded.")
        return pipe

    async def unload(self) -> None:
        if self._pipe is None:
            return
        del self._pipe
        self._pipe = None
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — best-effort VRAM reclaim
            pass
        logger.info("Video model unloaded.")

    async def is_loaded(self) -> bool:
        return self._pipe is not None

    async def infer(self, request: VideoRequest) -> VideoResult:
        if self._pipe is None:
            raise RuntimeError("Video engine not loaded — call load() first")

        # Generation is minutes of blocking GPU work. Off the event loop for the
        # same reason load() is.
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._infer_sync, request)

    def _infer_sync(self, request: VideoRequest) -> VideoResult:
        import torch
        from diffusers.utils import export_to_video

        frames = self.normalize_frames(request.num_frames)
        generator = None
        if request.seed is not None:
            generator = torch.Generator(self.device).manual_seed(request.seed)

        start = time.time()
        out = self._pipe(
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            width=request.width,
            height=request.height,
            num_frames=frames,
            num_inference_steps=request.num_inference_steps,
            guidance_scale=request.guidance_scale,
            generator=generator,
        )
        elapsed = time.time() - start

        # export_to_video writes a container, so it needs a real path; the bytes
        # are read straight back and the file is discarded. Keeping the result
        # in memory is what lets the caller decide where a clip lives.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            export_to_video(out.frames[0], str(path), fps=request.fps)
            mp4_bytes = path.read_bytes()

        logger.info(
            "Generated {}x{} clip, {} frames @ {} fps in {:.1f}s ({:.1f} KB)",
            request.width,
            request.height,
            frames,
            request.fps,
            elapsed,
            len(mp4_bytes) / 1024,
        )
        return VideoResult(
            mp4_bytes=mp4_bytes,
            metadata={
                "model": self.model,
                "width": request.width,
                "height": request.height,
                # Report what was produced, not what was asked for: the frame
                # count is rounded to the pipeline's 8k+1 stride.
                "num_frames": frames,
                "requested_frames": request.num_frames,
                "fps": request.fps,
                "duration_seconds": round(frames / request.fps, 2),
                "num_inference_steps": request.num_inference_steps,
                "seed": request.seed,
                "generation_time_seconds": round(elapsed, 2),
            },
        )
