"""Serialize/deserialize round-trips for the image protocol messages + the
backward-compatible RegisterMessage capability fields."""

from app.models.protocol import (
    GPUInfo,
    ImageJobCompleteMessage,
    ImageJobDispatchMessage,
    ImageJobFailedMessage,
    RegisterMessage,
    parse_message,
    serialize,
)


def test_dispatch_roundtrip():
    m = ImageJobDispatchMessage(
        job_id="j1", model="flux-schnell", prompt="a cat", seed=7, binary_token="tok"
    )
    parsed = parse_message(serialize(m))
    assert isinstance(parsed, ImageJobDispatchMessage)
    assert parsed.prompt == "a cat"
    assert parsed.width == 1024 and parsed.height == 1024
    assert parsed.num_inference_steps == 4
    assert parsed.seed == 7
    assert parsed.binary_token == "tok"


def test_complete_roundtrip():
    m = ImageJobCompleteMessage(
        job_id="j1", image_id="i1", binary_url="http://n/v1/binary/image/i1", metadata={"seed": 1}
    )
    parsed = parse_message(serialize(m))
    assert isinstance(parsed, ImageJobCompleteMessage)
    assert parsed.image_id == "i1"
    assert parsed.metadata["seed"] == 1


def test_failed_roundtrip():
    parsed = parse_message(serialize(ImageJobFailedMessage(job_id="j1", error="boom")))
    assert isinstance(parsed, ImageJobFailedMessage)
    assert parsed.error == "boom"


def _register(**kw) -> RegisterMessage:
    return RegisterMessage(
        provider_id="p",
        node_secret="s",
        version="1.0",
        gpu_info=GPUInfo(model="x", vram_total_mb=24000),
        models_supported=["qwen-2.5-7b"],
        max_concurrent_jobs=2,
        **kw,
    )


def test_register_capabilities_backward_compatible():
    # Old node: no engines/vram_gb → sensible defaults.
    parsed = parse_message(serialize(_register()))
    assert isinstance(parsed, RegisterMessage)
    assert parsed.engines == []
    assert parsed.vram_gb == 0.0


def test_register_with_capabilities():
    parsed = parse_message(serialize(_register(engines=["chat", "image"], vram_gb=24.0)))
    assert parsed.engines == ["chat", "image"]
    assert parsed.vram_gb == 24.0
