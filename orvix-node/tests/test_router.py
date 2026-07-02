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


def test_engine_type_for_unknown_raises():
    with pytest.raises(ValueError, match="Unknown model"):
        engine_type_for("does-not-exist")


def test_models_for_engine():
    assert models_for_engine("image") == ["flux-schnell"]
    assert "qwen-2.5-7b" in models_for_engine("chat")


def test_available_engine_types_default_chat_only():
    assert available_engine_types() == ["chat"]
    assert available_engine_types(enable_image=False) == ["chat"]


def test_available_engine_types_with_image():
    assert available_engine_types(enable_image=True) == ["chat", "image"]
