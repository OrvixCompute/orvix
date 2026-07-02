"""Engine routing: map an orchestrator-facing model id to an engine type.

Session 1 keeps this deliberately thin — a pure ``model_id -> engine_type``
resolver plus the node's advertised engine list. Actually loading/swapping
engines (the ModelManager) arrives in Session 2, which will build on this map.
"""

from __future__ import annotations

from typing import Dict, List

# Orchestrator-facing catalog id -> engine type. Extend this when a new model or
# engine is added; the ModelManager (Session 2) uses it to pick an engine.
MODEL_TO_ENGINE: Dict[str, str] = {
    "qwen-2.5-7b": "chat",
    "flux-schnell": "image",
}

ENGINE_TYPES = ("chat", "image")


def engine_type_for(model_id: str) -> str:
    """Return the engine type that serves ``model_id``.

    Raises ValueError for an unknown model so callers fail loudly rather than
    silently mis-routing.
    """
    engine_type = MODEL_TO_ENGINE.get(model_id)
    if engine_type is None:
        raise ValueError(f"Unknown model: {model_id!r}")
    return engine_type


def models_for_engine(engine_type: str) -> List[str]:
    """All catalog model ids handled by ``engine_type``."""
    return [m for m, et in MODEL_TO_ENGINE.items() if et == engine_type]


def available_engine_types(enable_image: bool = False) -> List[str]:
    """Engine types this node advertises.

    Chat is always available. Image is opt-in (``enable_image``) because serving
    it safely alongside chat needs the ModelManager's VRAM swap (Session 2); we
    do not advertise a capability the node cannot yet fulfil.
    """
    types = ["chat"]
    if enable_image:
        types.append("image")
    return types
