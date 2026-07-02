"""Node HTTP binary endpoint — serves generated image bytes to the orchestrator.

After an image job completes, the node registers the file under its image_id with
the per-job token the orchestrator sent in the dispatch. The orchestrator then
GETs ``/v1/binary/image/<image_id>`` with that token in the ``X-Node-Secret``
header. The temp file (and registry entry) is deleted after a successful
transfer, so each image is served exactly once.
"""

from __future__ import annotations

import os
import secrets
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from orvix_node.logger import logger

# image_id -> {"token": str, "path": str}
_registry: dict[str, dict] = {}


def register_image(image_id: str, token: str, path: str) -> None:
    _registry[image_id] = {"token": token, "path": path}


def sweep_temp_dir(tmp_dir: str, max_age_seconds: float = 3600, now: float | None = None) -> dict:
    """Delete leftover image temp files older than max_age_seconds.

    Handles two leak cases: an image that completed but was never fetched (still
    registered → drop the registry entry too), and files left by an earlier crash
    (not registered → remove the file). Returns {"removed": n}.
    """
    now = now if now is not None else time.time()
    result = {"removed": 0}
    if not os.path.isdir(tmp_dir):
        return result
    for name in os.listdir(tmp_dir):
        fpath = os.path.join(tmp_dir, name)
        try:
            if not os.path.isfile(fpath):
                continue
            if now - os.path.getmtime(fpath) < max_age_seconds:
                continue
            image_id = name[:-4] if name.endswith(".png") else name
            _registry.pop(image_id, None)
            os.remove(fpath)
            result["removed"] += 1
        except OSError as exc:  # noqa: BLE001
            logger.warning("Temp sweep failed to remove {}: {}", fpath, exc)
    if result["removed"]:
        logger.info("Temp sweep removed {} stale image file(s)", result["removed"])
    return result


def _cleanup(image_id: str, path: str) -> None:
    _registry.pop(image_id, None)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:  # noqa: BLE001
        logger.warning("Failed to delete served image {}: {}", path, exc)


def create_binary_router() -> APIRouter:
    router = APIRouter(tags=["binary"])

    @router.get("/v1/binary/image/{image_id}")
    async def get_image(image_id: str, x_node_secret: str | None = Header(default=None)):
        entry = _registry.get(image_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="image not found")
        if not x_node_secret or not secrets.compare_digest(x_node_secret, entry["token"]):
            raise HTTPException(status_code=401, detail="invalid node secret")
        path = entry["path"]
        if not os.path.exists(path):
            _registry.pop(image_id, None)
            raise HTTPException(status_code=404, detail="image file missing")
        # Stream the PNG, then delete the temp file + registry entry.
        return FileResponse(
            path,
            media_type="image/png",
            background=BackgroundTask(_cleanup, image_id, path),
        )

    return router
