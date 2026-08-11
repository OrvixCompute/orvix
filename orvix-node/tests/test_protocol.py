"""Protocol round-trip tests for the video job messages.

The protocol file is mirrored between node and orchestrator and must stay
identical; these tests pin the wire shapes that both sides serialize against.
"""

import pytest

from orvix_node.protocol import (
    VideoJobCompleteMessage,
    VideoJobDispatchMessage,
    VideoJobFailedMessage,
    parse_message,
    serialize,
)


def test_video_dispatch_round_trip():
    msg = VideoJobDispatchMessage(
        job_id="j1",
        model="orvix-video-1",
        prompt="a cat walking",
        negative_prompt="blurry",
        width=704,
        height=480,
        num_frames=97,
        fps=24,
        num_inference_steps=30,
        guidance_scale=3.0,
        seed=42,
        binary_token="tok123",
    )
    parsed = parse_message(serialize(msg))
    assert isinstance(parsed, VideoJobDispatchMessage)
    assert parsed.type == "job.video.dispatch"
    assert parsed.job_id == "j1"
    assert parsed.negative_prompt == "blurry"
    assert parsed.width == 704 and parsed.height == 480
    assert parsed.num_frames == 97 and parsed.fps == 24
    assert parsed.num_inference_steps == 30 and parsed.guidance_scale == 3.0
    assert parsed.seed == 42 and parsed.binary_token == "tok123"


def test_video_dispatch_defaults():
    msg = VideoJobDispatchMessage(
        job_id="j1", model="orvix-video-1", prompt="x", binary_token="tok"
    )
    assert msg.width == 704 and msg.height == 480
    assert msg.num_frames == 97 and msg.fps == 24
    assert msg.num_inference_steps == 30 and msg.guidance_scale == 3.0
    assert msg.negative_prompt is None and msg.seed is None


def test_video_complete_round_trip():
    msg = VideoJobCompleteMessage(
        job_id="j1",
        video_id="vid-1",
        binary_url="http://node:9000/v1/binary/video/vid-1",
        metadata={"num_frames": 97, "duration_seconds": 4.04},
    )
    parsed = parse_message(serialize(msg))
    assert isinstance(parsed, VideoJobCompleteMessage)
    assert parsed.type == "job.video.complete"
    assert parsed.video_id == "vid-1"
    assert parsed.binary_url.endswith("/v1/binary/video/vid-1")
    assert parsed.metadata["num_frames"] == 97


def test_video_failed_round_trip():
    msg = VideoJobFailedMessage(job_id="j1", error="OOM during generation")
    parsed = parse_message(serialize(msg))
    assert isinstance(parsed, VideoJobFailedMessage)
    assert parsed.type == "job.video.failed"
    assert parsed.error == "OOM during generation"


def test_unknown_type_raises():
    with pytest.raises(Exception):
        parse_message(
            '{"type": "job.video.unknown", "id": "x", "timestamp": "2026-01-01T00:00:00Z"}'
        )
