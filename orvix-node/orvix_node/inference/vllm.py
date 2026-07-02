"""vLLM inference backend — HTTP proxy to a local vLLM OpenAI-compatible server.

Instead of embedding an in-process ``AsyncLLMEngine`` (which would load a second
copy of the model and fight the standalone vLLM server for VRAM), this backend
forwards jobs over HTTP to an already-running vLLM server
(``vllm serve ... --port 8000``) on ``inference_endpoint``.

Config (env / constructor):
  - ORVIX_NODE_INFERENCE_ENDPOINT  (default http://localhost:8000/v1)
  - ORVIX_NODE_VLLM_MODEL          the model id vLLM actually serves
                                   (default Qwen/Qwen2.5-7B-Instruct)

The orchestrator-facing catalog id (e.g. ``qwen-2.5-7b``) is what the node
advertises; ``vllm_model`` is the upstream name sent to the vLLM server.
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator, Optional

import httpx

from orvix_node.inference.base import (
    ChatEngine,
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
    GenerateUsage,
)
from orvix_node.logger import logger

_DEFAULT_ENDPOINT = "http://localhost:8000/v1"
_DEFAULT_VLLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"


class VLLMBackend(ChatEngine):
    # Capability metadata (informational; the served model comes from config).
    required_vram_gb = 18.0
    supported_models = ["qwen-2.5-7b"]

    def __init__(
        self,
        model: str,
        inference_endpoint: Optional[str] = None,
        vllm_model: Optional[str] = None,
        request_timeout: float = 300.0,
    ) -> None:
        self.model = model  # orchestrator-facing catalog id
        self.inference_endpoint = (
            inference_endpoint
            or os.environ.get("ORVIX_NODE_INFERENCE_ENDPOINT")
            or _DEFAULT_ENDPOINT
        ).rstrip("/")
        self.vllm_model = (
            vllm_model
            or os.environ.get("ORVIX_NODE_VLLM_MODEL")
            or _DEFAULT_VLLM_MODEL
        )
        self.request_timeout = request_timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self, model: str) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=self.inference_endpoint,
            timeout=httpx.Timeout(self.request_timeout, connect=10.0),
        )
        # Probe the server and confirm (or auto-correct) the served model id.
        try:
            r = await self._client.get("/models")
            r.raise_for_status()
            served = [m.get("id") for m in r.json().get("data", [])]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"vLLM endpoint {self.inference_endpoint} not reachable: {exc}"
            ) from exc
        if served and self.vllm_model not in served:
            logger.warning(
                "Configured vLLM model '{}' not served (served={}); using '{}'",
                self.vllm_model,
                served,
                served[0],
            )
            self.vllm_model = served[0]
        logger.info(
            "vLLM HTTP backend ready at {} (catalog='{}' -> vllm='{}')",
            self.inference_endpoint,
            self.model,
            self.vllm_model,
        )

    async def is_ready(self) -> bool:
        return self._client is not None

    def _payload(self, request: GenerateRequest, stream: bool) -> dict:
        return {
            "model": self.vllm_model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }

    @staticmethod
    def _finish(reason: Optional[str]) -> str:
        return reason if reason in ("stop", "length") else "stop"

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        assert self._client is not None, "backend not initialized"
        r = await self._client.post(
            "/chat/completions", json=self._payload(request, False)
        )
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return GenerateResponse(
            content=(choice["message"].get("content") or ""),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=self._finish(choice.get("finish_reason")),
        )

    async def generate_stream(
        self, request: GenerateRequest
    ) -> AsyncIterator[GenerateChunk]:
        assert self._client is not None, "backend not initialized"
        payload = self._payload(request, True)
        payload["stream_options"] = {"include_usage": True}
        prompt_tokens = 0
        completion_tokens = 0
        async with self._client.stream(
            "POST", "/chat/completions", json=payload
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    prompt_tokens = obj["usage"].get("prompt_tokens", prompt_tokens)
                    completion_tokens = obj["usage"].get(
                        "completion_tokens", completion_tokens
                    )
                for ch in obj.get("choices", []):
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        yield GenerateChunk(delta_content=delta, is_final=False)
        yield GenerateChunk(
            delta_content="",
            is_final=True,
            usage=GenerateUsage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
