"""SDXL Lightning image engine — Diffusers-backed text-to-image, 4-step distilled.

SDXL Lightning (ByteDance) swaps a distilled Lightning UNet checkpoint into a
stock SDXL base pipeline for fast, guidance-free generation. Chosen over Flux
Schnell for pod activation: no HuggingFace access gate, a smaller on-disk
footprint, and it fits comfortably in less VRAM (fp16).

Heavy GPU dependencies (torch, diffusers, safetensors) are imported lazily
inside load()/infer(), mirroring FluxEngine, so importing this module never
requires a GPU or the `image` extra to be installed.

Config (env / constructor):
  - ORVIX_NODE_SDXL_BASE_MODEL   base pipeline repo (default stabilityai/stable-diffusion-xl-base-1.0)
  - ORVIX_NODE_SDXL_CACHE_DIR    local model cache (default ./models/sdxl-lightning)
"""

from __future__ import annotations

import asyncio
import io
import os
import time
from typing import Optional

from orvix_node.inference.base import ImageEngine, ImageRequest, ImageResult
from orvix_node.logger import logger

_DEFAULT_BASE_MODEL = "stabilityai/stable-diffusion-xl-base-1.0"
_DEFAULT_CACHE_DIR = "./models/sdxl-lightning"
_LIGHTNING_REPO = "ByteDance/SDXL-Lightning"
_LIGHTNING_CKPT = "sdxl_lightning_4step_unet.safetensors"
_LIGHTNING_STEPS = 4  # fixed: the 4-step UNet checkpoint requires exactly this


class SDXLLightningEngine(ImageEngine):
    required_vram_gb = 10.0
    supported_models = ["sdxl-lightning"]

    def __init__(
        self,
        base_model: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: str = "cuda",
    ) -> None:
        self.base_model = (
            base_model
            or os.environ.get("ORVIX_NODE_SDXL_BASE_MODEL")
            or _DEFAULT_BASE_MODEL
        )
        self.cache_dir = (
            cache_dir or os.environ.get("ORVIX_NODE_SDXL_CACHE_DIR") or _DEFAULT_CACHE_DIR
        )
        self.device = device
        self._pipe = None  # diffusers StableDiffusionXLPipeline once loaded

    async def load(self, model_id: str = "sdxl-lightning") -> None:
        # ``model_id`` is the orchestrator-facing catalog id; the upstream repos
        # are fixed on this engine, so the argument is accepted for interface
        # uniformity but not otherwise used.
        if self._pipe is not None:
            return
        # Run the (synchronous, ~minute-long) load in a thread so it doesn't
        # block the event loop — the client's heartbeat/WS traffic runs on the
        # same loop, and starving it this long reads to the orchestrator as a
        # dead connection even though the node is fine.
        loop = asyncio.get_event_loop()
        self._pipe = await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self):
        import torch
        from diffusers import (
            EulerDiscreteScheduler,
            StableDiffusionXLPipeline,
            UNet2DConditionModel,
        )
        from huggingface_hub import hf_hub_download
        from safetensors.torch import load_file

        logger.info(
            "Loading SDXL Lightning ({} + {}) into VRAM (fp16, cache={})...",
            self.base_model,
            _LIGHTNING_CKPT,
            self.cache_dir,
        )
        unet = UNet2DConditionModel.from_config(
            self.base_model, subfolder="unet", cache_dir=self.cache_dir
        ).to(self.device, torch.float16)
        ckpt_path = hf_hub_download(
            _LIGHTNING_REPO, _LIGHTNING_CKPT, cache_dir=self.cache_dir
        )
        unet.load_state_dict(load_file(ckpt_path, device=self.device))

        pipe = StableDiffusionXLPipeline.from_pretrained(
            self.base_model,
            unet=unet,
            torch_dtype=torch.float16,
            variant="fp16",
            cache_dir=self.cache_dir,
        )
        pipe.to(self.device)
        pipe.scheduler = EulerDiscreteScheduler.from_config(
            pipe.scheduler.config, timestep_spacing="trailing"
        )
        logger.info("SDXL Lightning loaded.")
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
        logger.info("SDXL Lightning unloaded.")

    async def is_loaded(self) -> bool:
        return self._pipe is not None

    async def infer(self, request: ImageRequest) -> ImageResult:
        if self._pipe is None:
            raise RuntimeError("SDXL Lightning engine not loaded — call load() first")

        import torch

        generator = None
        if request.seed is not None:
            generator = torch.Generator(self.device).manual_seed(request.seed)

        start = time.time()
        result = self._pipe(
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            num_inference_steps=_LIGHTNING_STEPS,
            guidance_scale=0.0,
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
                "steps": _LIGHTNING_STEPS,
                "width": request.width,
                "height": request.height,
                "generation_time_seconds": round(elapsed, 2),
                "model": "sdxl-lightning",
                "guidance_scale": 0.0,
            },
        )
