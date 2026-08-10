"""Tests for the engine router (model_id -> engine_type mapping)."""

from __future__ import annotations

import pytest

from orvix_node.inference.router import (
    available_engine_types,
    engine_type_for,
    models_for_engine,
)


def test_engine_type_for_known_models():
    assert engine_type_for("qwen-2.5-7b") == "chat"
    assert engine_type_for("flux-schnell") == "image"
    assert engine_type_for("orvix-image-1") == "image"


def test_engine_type_for_unknown_raises():
    with pytest.raises(ValueError, match="Unknown model"):
        engine_type_for("does-not-exist")


def test_models_for_engine():
    assert models_for_engine("image") == ["flux-schnell", "orvix-image-1"]
    assert "qwen-2.5-7b" in models_for_engine("chat")


def test_available_engine_types_default_chat_only():
    assert available_engine_types() == ["chat"]
    assert available_engine_types(enable_image=False) == ["chat"]


def test_available_engine_types_with_image():
    assert available_engine_types(enable_image=True) == ["chat", "image"]


def test_video_model_routes_to_the_video_engine():
    from orvix_node.inference.router import engine_type_for

    assert engine_type_for("orvix-video-1") == "video"


def test_available_engine_types_with_video():
    # Video is opt-in independently of image: a machine can be dedicated to
    # clips without also advertising image.
    assert available_engine_types(enable_video=True) == ["chat", "video"]
    assert available_engine_types(enable_image=True, enable_video=True) == [
        "chat",
        "image",
        "video",
    ]
    # And stays off by default — a clip parks the node for minutes.
    assert "video" not in available_engine_types()


def test_embedding_model_routes_to_the_embedding_engine():
    from orvix_node.inference.router import engine_type_for

    assert engine_type_for("orvix-embed-1") == "embedding"


def test_available_engine_types_with_embedding():
    # Independent of the other two: a node can serve embeddings while its GPU
    # is busy with chat, which is the reason this engine exists.
    assert available_engine_types(enable_embedding=True) == ["chat", "embedding"]
    assert available_engine_types(
        enable_image=True, enable_video=True, enable_embedding=True
    ) == ["chat", "image", "video", "embedding"]
    assert "embedding" not in available_engine_types()
