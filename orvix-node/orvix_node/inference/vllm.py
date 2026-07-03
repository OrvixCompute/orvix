"""vLLM inference backend — HTTP proxy to a local vLLM OpenAI-compatible server.

Instead of embedding an in-process ``AsyncLLMEngine`` (which would load a second
copy of the model and fight the standalone vLLM server for VRAM), this backend
forwards jobs over HTTP to a vLLM server (``vllm serve ... --port 8000``) on
``inference_endpoint``.

VRAM ownership — two modes:
  - Unmanaged (default): the vLLM server is started/stopped out of band; the node
    just connects to it. ``unload()`` closes the HTTP client but does NOT free the
    server's VRAM (the process keeps running).
  - Managed (``managed=True``): the node OWNS the vLLM server as a subprocess.
    ``load()`` spawns ``vllm serve`` and waits until it answers; ``unload()``
    terminates the process, which is what actually frees VRAM. This is required
    for the ModelManager to swap the GPU between chat (vLLM) and image (Flux).
    Restarting the server on the next ``load()`` costs ~10-15s.

Config (env / constructor):
  - ORVIX_NODE_INFERENCE_ENDPOINT  (default http://localhost:8000/v1)
  - ORVIX_NODE_VLLM_MODEL          upstream model id vLLM serves
  - ORVIX_NODE_VLLM_MANAGED        "true" to let the node control the subprocess
  - ORVIX_NODE_VLLM_SERVE_CMD      override the launch command; supports
                                   {model} and {port} placeholders

The orchestrator-facing catalog id (e.g. ``qwen-2.5-7b``) is what the node
advertises; ``vllm_model`` is the upstream name sent to the vLLM server.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from typing import AsyncIterator, List, Optional
from urllib.parse import urlparse

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


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


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
        managed: Optional[bool] = None,
        serve_command: Optional[str] = None,
        startup_timeout: float = 180.0,
        stop_timeout: float = 30.0,
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
        self._managed = managed if managed is not None else _env_true("ORVIX_NODE_VLLM_MANAGED")
        self._serve_command = serve_command or os.environ.get("ORVIX_NODE_VLLM_SERVE_CMD") or ""
        self._startup_timeout = startup_timeout
        self._stop_timeout = stop_timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._process: Optional[asyncio.subprocess.Process] = None

    # --- lifecycle ---------------------------------------------------------
    async def load(self, model_id: str) -> None:
        self.model = model_id
        self._client = httpx.AsyncClient(
            base_url=self.inference_endpoint,
            timeout=httpx.Timeout(self.request_timeout, connect=10.0),
        )
        # In managed mode we own the server process — spawn it and wait until it
        # answers before probing. In unmanaged mode the server is already up.
        if self._managed:
            await self._start_server()
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
            "vLLM HTTP backend ready at {} (catalog='{}' -> vllm='{}', managed={})",
            self.inference_endpoint,
            self.model,
            self.vllm_model,
            self._managed,
        )

    async def is_loaded(self) -> bool:
        return self._client is not None

    async def unload(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        # In managed mode, killing the process is what actually frees VRAM.
        if self._managed and self._process is not None:
            await self._stop_server()

    # --- managed-subprocess control ---------------------------------------
    def _server_port(self) -> int:
        return urlparse(self.inference_endpoint).port or 8000

    def _serve_cmd(self) -> List[str]:
        port = self._server_port()
        if self._serve_command:
            return shlex.split(self._serve_command.format(model=self.vllm_model, port=port))
        return ["vllm", "serve", self.vllm_model, "--port", str(port)]

    async def _start_server(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return  # already running
        cmd = self._serve_cmd()
        logger.info("Starting managed vLLM server: {}", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(*cmd)
        await self._wait_until_ready()

    async def _wait_until_ready(self) -> None:
        assert self._client is not None
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError(
                    f"vLLM server exited during startup (code {self._process.returncode})"
                )
            try:
                r = await self._client.get("/models")
                if r.status_code == 200:
                    logger.info("Managed vLLM server is ready")
                    return
            except Exception:  # noqa: BLE001 — not up yet, keep polling
                pass
            await asyncio.sleep(1.0)
        raise RuntimeError(
            f"vLLM server did not become ready within {self._startup_timeout:.0f}s"
        )

    async def _stop_server(self) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._stop_timeout)
        except asyncio.TimeoutError:
            logger.warning("vLLM server did not stop in time; killing")
            proc.kill()
            await proc.wait()
        logger.info("Managed vLLM server stopped (VRAM freed)")

    # --- inference ---------------------------------------------------------
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
        assert self._client is not None, "backend not loaded"
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
        assert self._client is not None, "backend not loaded"
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
