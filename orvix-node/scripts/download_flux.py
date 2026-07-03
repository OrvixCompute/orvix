#!/usr/bin/env python3
"""Pre-download Flux Schnell weights into the local cache (opt-in, one-time).

Flux Schnell is ~24 GB. Downloading it lazily on the first image request would
add 5–15 minutes of latency, so provider bootstrap should run this once ahead of
time. It is intentionally NOT wired into node startup — run it manually:

    pip install -e .[image]              # installs diffusers + huggingface_hub
    python scripts/download_flux.py      # ~24 GB download, one-time

Cache location and model id honour the same env vars the FluxEngine reads:
    ORVIX_NODE_FLUX_MODEL       (default black-forest-labs/FLUX.1-schnell)
    ORVIX_NODE_FLUX_CACHE_DIR   (default ./models/flux-schnell)
"""

from __future__ import annotations

import os
import sys

_DEFAULT_MODEL_ID = "black-forest-labs/FLUX.1-schnell"
_DEFAULT_CACHE_DIR = "./models/flux-schnell"


def main() -> int:
    model_id = os.environ.get("ORVIX_NODE_FLUX_MODEL") or _DEFAULT_MODEL_ID
    cache_dir = os.environ.get("ORVIX_NODE_FLUX_CACHE_DIR") or _DEFAULT_CACHE_DIR

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "huggingface_hub is not installed. Run `pip install -e .[image]` first.",
            file=sys.stderr,
        )
        return 1

    print(f"Downloading {model_id} (~24 GB, one-time) into {cache_dir} ...")
    snapshot_download(repo_id=model_id, cache_dir=cache_dir)
    print("Done. Flux Schnell is cached and ready for the image engine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
