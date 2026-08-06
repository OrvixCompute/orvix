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
    await b.unload()


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
    await b.unload()


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
    await b.unload()


def test_startup_timeout_defaults_to_180():
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    assert b._startup_timeout == 180.0


def test_startup_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ORVIX_NODE_VLLM_STARTUP_TIMEOUT", "420")
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    assert b._startup_timeout == 420.0


def test_startup_timeout_constructor_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("ORVIX_NODE_VLLM_STARTUP_TIMEOUT", "420")
    b = VLLMBackend(
        model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL, startup_timeout=5
    )
    assert b._startup_timeout == 5.0


async def test_load_checks_local_vllm(patch_async_client):
    # load() probes the local vLLM server's model list (GET /models).
    patch_async_client(_default_handler)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    await b.load(CATALOG)
    assert await b.is_loaded() is True
    assert b.vllm_model == VLLM_MODEL  # configured model is served -> kept
    await b.unload()


async def test_load_auto_corrects_served_model(patch_async_client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "some/other-model"}]})
        return httpx.Response(404)

    patch_async_client(handler)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    await b.load(CATALOG)
    assert b.vllm_model == "some/other-model"  # falls back to what's actually served
    await b.unload()


async def test_load_fails_when_vllm_down(patch_async_client):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    patch_async_client(down)
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    with pytest.raises(RuntimeError, match="not reachable"):
        await b.load(CATALOG)


async def test_is_loaded_after_load(make_backend):
    b = VLLMBackend(model=CATALOG, inference_endpoint=ENDPOINT, vllm_model=VLLM_MODEL)
    assert await b.is_loaded() is False  # no client yet
    ready = make_backend()
    assert await ready.is_loaded() is True
    await ready.unload()


async def test_unload_cleans_up(make_backend):
    b = make_backend()
    assert b._client is not None
    await b.unload()
    assert b._client is None
    assert await b.is_loaded() is False


# --- managed subprocess mode ----------------------------------------------
class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_managed_load_spawns_and_unload_stops(patch_async_client, monkeypatch):
    patch_async_client(_default_handler)  # /models -> 200 immediately (ready)
    proc = _FakeProc()
    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return proc

    monkeypatch.setattr(vllm_mod.asyncio, "create_subprocess_exec", fake_exec)

    b = VLLMBackend(
        model=CATALOG,
        inference_endpoint=ENDPOINT,
        vllm_model=VLLM_MODEL,
        managed=True,
        startup_timeout=5,
    )
    await b.load(CATALOG)
    assert await b.is_loaded() is True
    # Default launch command + port parsed from the endpoint.
    assert captured["cmd"][0] == "vllm"
    assert "8000" in captured["cmd"]

    await b.unload()
    assert proc.terminated is True  # killing the process is what frees VRAM
    assert await b.is_loaded() is False


async def test_managed_load_raises_if_server_exits(patch_async_client, monkeypatch):
    patch_async_client(_default_handler)
    dead = _FakeProc()
    dead.returncode = 1  # already exited

    async def fake_exec(*cmd, **kwargs):
        return dead

    monkeypatch.setattr(vllm_mod.asyncio, "create_subprocess_exec", fake_exec)
    b = VLLMBackend(
        model=CATALOG,
        inference_endpoint=ENDPOINT,
        vllm_model=VLLM_MODEL,
        managed=True,
        startup_timeout=5,
    )
    with pytest.raises(RuntimeError, match="exited during startup"):
        await b.load(CATALOG)


# --- tool calling ----------------------------------------------------------
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

_TOOL_CALL_JSON = {
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "Jakarta"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 42, "completion_tokens": 17},
}


async def test_tools_are_omitted_from_the_payload_when_absent(make_backend):
    """vLLM rejects a null/empty `tools` key, so ordinary chat must not send it."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": VLLM_MODEL}]})
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_completion_json())

    backend = make_backend(handler)
    await backend.generate(GenerateRequest(messages=[{"role": "user", "content": "hi"}]))

    assert "tools" not in seen
    assert "tool_choice" not in seen


async def test_tools_are_forwarded_verbatim(make_backend):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": VLLM_MODEL}]})
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_TOOL_CALL_JSON)

    backend = make_backend(handler)
    await backend.generate(
        GenerateRequest(
            messages=[{"role": "user", "content": "weather in Jakarta?"}],
            tools=_TOOLS,
            tool_choice="auto",
        )
    )

    assert seen["tools"] == _TOOLS
    assert seen["tool_choice"] == "auto"


async def test_tool_calls_come_back_on_the_response(make_backend):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": VLLM_MODEL}]})
        return httpx.Response(200, json=_TOOL_CALL_JSON)

    backend = make_backend(handler)
    resp = await backend.generate(
        GenerateRequest(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)
    )

    assert resp.finish_reason == "tool_calls"
    assert resp.content == ""
    assert resp.tool_calls is not None and len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call["function"]["name"] == "get_weather"
    # Arguments stay a JSON string, as OpenAI sends them.
    assert json.loads(call["function"]["arguments"]) == {"city": "Jakarta"}


async def test_tool_calls_win_over_a_stop_finish_reason(make_backend):
    """Some engines label a tool-call turn "stop"; the calls are the truth."""
    payload = json.loads(json.dumps(_TOOL_CALL_JSON))
    payload["choices"][0]["finish_reason"] = "stop"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": VLLM_MODEL}]})
        return httpx.Response(200, json=payload)

    backend = make_backend(handler)
    resp = await backend.generate(
        GenerateRequest(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)
    )
    assert resp.finish_reason == "tool_calls"
