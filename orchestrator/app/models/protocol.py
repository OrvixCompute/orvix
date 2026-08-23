"""Wire protocol between node and orchestrator.

Re-exports from the shared ``orvix_protocol`` package. The canonical source
lives at ``packages/protocol/orvix_protocol/protocol.py``.
"""

from orvix_protocol.protocol import (
    AnyMessage,
    BaseMessage,
    EmbeddingJobDispatchMessage,
    GPUMetrics,
    GPUInfo,
    HeartbeatMessage,
    ImageJobCompleteMessage,
    ImageJobDispatchMessage,
    ImageJobFailedMessage,
    JobChunkMessage,
    JobMessage,
    JobResultMessage,
    PingMessage,
    RegisterAckMessage,
    RegisterMessage,
    ShutdownMessage,
    VideoJobCompleteMessage,
    VideoJobDispatchMessage,
    VideoJobFailedMessage,
    parse_message,
    serialize,
)

__all__ = [
    "AnyMessage",
    "BaseMessage",
    "EmbeddingJobDispatchMessage",
    "GPUMetrics",
    "GPUInfo",
    "HeartbeatMessage",
    "ImageJobCompleteMessage",
    "ImageJobDispatchMessage",
    "ImageJobFailedMessage",
    "JobChunkMessage",
    "JobMessage",
    "JobResultMessage",
    "PingMessage",
    "RegisterAckMessage",
    "RegisterMessage",
    "ShutdownMessage",
    "VideoJobCompleteMessage",
    "VideoJobDispatchMessage",
    "VideoJobFailedMessage",
    "parse_message",
    "serialize",
]
