"""Unit tests for the vLLM HTTP-proxy backend.

No real network / vLLM / GPU: httpx is intercepted with httpx.MockTransport
(for generate/stream/state tests) and via monkeypatching httpx.AsyncClient (for
initialize, which constructs its own client internally).
"""

from __future__ import annotations

import json

import httpx
import pytest

from orvix_node.inference import vllm as vllm_mod
from orvix_node.inference.base import GenerateRequest
from orvix_node.inference.vllm import VLLMBackend

ENDPOINT = "http://localhost:8000/v1"
CATALOG = "qwen-2.5-7b"
VLLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"


def _completion_json() -> dict:
    return {
        "choices": [{"message": {"content": "Hello there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


# vLLM streaming chunks (with stream_options include_usage -> a trailing usage frame).
_SSE = (
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


def _default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": VLLM_MODEL}]})
    if path.endswith("/chat/completions"):
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(
                200, content=_SSE.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_completion_json())
    return httpx.Response(404)


@pytest.fixture
def make_backend():
    """Build a VLLMBackend whose _client is wired to a MockTransport handler."""

    def _make(handler=_default_handler) -> VLLMBackend:
        b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
        b._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=ENDPOINT
        )
        return b

    return _make


@pytest.fixture
def patch_async_client(monkeypatch):
    """Make initialize()'s internally-created AsyncClient use a MockTransport."""
    real_cls = httpx.AsyncClient

    def _patch(handler=_default_handler):
        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_cls(*args, **kwargs)

        monkeypatch.setattr(vllm_mod.httpx, "AsyncClient", factory)

    return _patch


def _req() -> GenerateRequest:
    return GenerateRequest(messages=[{"role": "user", "content": "hi"}], max_tokens=64)


async def test_generate_non_stream_success(make_backend):
    b = make_backend()
    resp = await b.generate(_req())
    assert resp.content == "Hello there"
    assert resp.prompt_tokens == 11
    assert resp.completion_tokens == 3
    assert resp.finish_reason == "stop"
    await b.shutdown()


async def test_generate_stream_chunks(make_backend):
    b = make_backend()
    chunks = [c async for c in b.generate_stream(_req())]
    content_chunks = [c for c in chunks if not c.is_final]
    assert "".join(c.delta_content for c in content_chunks) == "Hello world"
    final = chunks[-1]
    assert final.is_final is True
    assert final.usage is not None
    assert final.usage.prompt_tokens == 7
    assert final.usage.completion_tokens == 2
    await b.shutdown()


async def test_model_mapping(make_backend):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=_completion_json())
        return httpx.Response(404)

    b = make_backend(handler)
    await b.generate(_req())
    # The orchestrator catalog id (qwen-2.5-7b) must be mapped to the upstream id.
    assert b.model == CATALOG
    assert captured["model"] == VLLM_MODEL
    await b.shutdown()


async def test_initialize_checks_local_vllm(patch_async_client):
    # initialize() probes the local vLLM server's model list (GET /models).
    patch_async_client(_default_handler)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    await b.initialize(CATALOG)
    assert await b.is_ready() is True
    assert b.vllm_model == VLLM_MODEL  # configured model is served -> kept
    await b.shutdown()


async def test_initialize_auto_corrects_served_model(patch_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "some/other-model"}]})
        return httpx.Response(404)

    patch_async_client(handler)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    await b.initialize(CATALOG)
    assert b.vllm_model == "some/other-model"  # falls back to what's actually served
    await b.shutdown()


async def test_initialize_fails_when_vllm_down(patch_async_client):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    patch_async_client(down)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    with pytest.raises(RuntimeError, match="not reachable"):
        await b.initialize(CATALOG)


async def test_is_ready_after_initialize(make_backend):
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    assert await b.is_ready() is False  # no client yet
    ready = make_backend()
    assert await ready.is_ready() is True
    await ready.shutdown()


async def test_shutdown_cleans_up(make_backend):
    b = make_backend()
    assert b._client is not None
    await b.shutdown()
    assert b._client is None
    assert await b.is_ready() is False
