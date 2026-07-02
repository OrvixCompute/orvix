"""Flux Schnell image engine — Diffusers-backed text-to-image.

Flux Schnell (Apache 2.0) is a distilled model that produces good images in only
1–4 steps and ignores guidance. It runs in bfloat16 to fit ~16 GB of VRAM.

Heavy GPU dependencies (``torch``, ``diffusers``) are imported lazily inside
:meth:`load` / :meth:`infer`, so importing this module — and unit-testing the
engine with those libraries mocked — never requires a GPU or the ``image`` extra
to be installed.

Config (env / constructor):
  - ORVIX_NODE_FLUX_MODEL       upstream model id (default black-forest-labs/FLUX.1-schnell)
  - ORVIX_NODE_FLUX_CACHE_DIR   local model cache (default ./models/flux-schnell)
"""

from __future__ import annotations

import io
import os
import time
from typing import Optional

from orvix_node.inference.base import ImageEngine, ImageRequest, ImageResult
from orvix_node.logger import logger

_DEFAULT_MODEL_ID = "black-forest-labs/FLUX.1-schnell"
_DEFAULT_CACHE_DIR = "./models/flux-schnell"


class FluxEngine(ImageEngine):
    required_vram_gb = 16.0
    supported_models = ["flux-schnell"]

    def __init__(
        self,
        model_id: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: str = "cuda",
    ) -> None:
        self.model_id = (
            model_id or os.environ.get("ORVIX_NODE_FLUX_MODEL") or _DEFAULT_MODEL_ID
        )
        self.cache_dir = (
            cache_dir
            or os.environ.get("ORVIX_NODE_FLUX_CACHE_DIR")
            or _DEFAULT_CACHE_DIR
        )
        self.device = device
        self._pipe = None  # diffusers FluxPipeline once loaded

    async def load(self) -> None:
        if self._pipe is not None:
            return
        # Lazy heavy imports — only needed when actually serving on a GPU.
        import torch
        from diffusers import FluxPipeline

        logger.info(
            "Loading Flux Schnell ({}) into VRAM (bf16, cache={})...",
            self.model_id,
            self.cache_dir,
        )
        pipe = FluxPipeline.from_pretrained(
            self.model_id,
            torch_dtype=torch.bfloat16,
            cache_dir=self.cache_dir,
        )
        pipe.to(self.device)
        self._pipe = pipe
        logger.info("Flux Schnell loaded.")

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
        logger.info("Flux Schnell unloaded.")

    async def is_loaded(self) -> bool:
        return self._pipe is not None

    async def infer(self, request: ImageRequest) -> ImageResult:
        if self._pipe is None:
            raise RuntimeError("Flux engine not loaded — call load() first")

        import torch

        generator = None
        if request.seed is not None:
            generator = torch.Generator(self.device).manual_seed(request.seed)

        start = time.time()
        result = self._pipe(
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            num_inference_steps=request.num_inference_steps,
            guidance_scale=request.guidance_scale,
            generator=generator,
        )
        elapsed = time.time() - start

        image = result.images[0]  # PIL.Image
        buf = io.BytesIO()
        image.save(buf, format="PNG")

        return ImageResult(
            png_bytes=buf.getvalue(),
            metadata={
                "seed": request.seed,
                "steps": request.num_inference_steps,
                "width": request.width,
                "height": request.height,
                "generation_time_seconds": round(elapsed, 2),
                "model": self.model_id,
            },
        )
