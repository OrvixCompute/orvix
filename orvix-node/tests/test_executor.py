"""Tests for JobExecutor driving engines through the ModelManager."""

import asyncio
from typing import AsyncIterator

import pytest

from orvix_node.executor import JobExecutor
from orvix_node.inference.base import (
    ChatEngine,
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
    GenerateUsage,
)
from orvix_node.inference.manager import ModelManager
from orvix_node.inference.mock import MockBackend
from orvix_node.protocol import JobMessage
from orvix_node.state import state


@pytest.fixture(autouse=True)
def reset_state():
    state.current_jobs.clear()
    state.jobs_completed = 0
    state.jobs_failed = 0
    state.total_tokens = 0
    yield


class Collector:
    def __init__(self):
        self.messages = []

    async def __call__(self, msg):
        self.messages.append(msg)


def _job(stream=False, job_id="j1"):
    return JobMessage(
        job_id=job_id,
        model="qwen-2.5-7b",
        messages=[{"role": "user", "content": "hi there"}],
        max_tokens=64,
        stream=stream,
    )


def _executor(engine, max_concurrent=2):
    """Wrap a single chat engine in a ModelManager and build an executor."""
    return JobExecutor(ModelManager({"chat": engine}), max_concurrent=max_concurrent)


async def test_mock_blocking_result_shape():
    ex = _executor(MockBackend("p"))
    out = Collector()
    await ex.execute(_job(stream=False), send_chunk=Collector(), send_result=out)

    assert len(out.messages) == 1
    res = out.messages[0]
    assert res.type == "job_result"
    assert res.status == "completed"
    assert res.result["choices"][0]["message"]["content"].startswith("This is a mock response")
    assert res.completion_tokens > 0
    assert state.jobs_completed == 1


async def test_mock_streaming_yields_multiple_chunks():
    ex = _executor(MockBackend("p"))
    chunks = Collector()
    await ex.execute(_job(stream=True), send_chunk=chunks, send_result=Collector())

    assert len(chunks.messages) >= 2
    assert all(c.type == "job_chunk" for c in chunks.messages)
    assert chunks.messages[-1].is_final is True
    assert chunks.messages[-1].chunk["choices"][0]["finish_reason"] == "stop"


class SlowBackend(ChatEngine):
    """Tracks max concurrency to verify the semaphore limit."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self._loaded = False

    async def load(self, model_id): self._loaded = True
    async def unload(self): self._loaded = False
    async def is_loaded(self): return self._loaded

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.1)
        finally:
            self.active -= 1
        return GenerateResponse(content="x", prompt_tokens=1, completion_tokens=1)

    async def generate_stream(self, request) -> AsyncIterator[GenerateChunk]:
        yield GenerateChunk(delta_content="x")
        yield GenerateChunk(is_final=True, usage=GenerateUsage(prompt_tokens=1, completion_tokens=1))


async def test_concurrency_limit_enforced():
    backend = SlowBackend()
    ex = _executor(backend, max_concurrent=2)
    sink = Collector()
    jobs = [
        ex.execute(_job(job_id=f"j{i}"), send_chunk=sink, send_result=sink)
        for i in range(5)
    ]
    await asyncio.gather(*jobs)
    assert backend.max_active <= 2


class BrokenBackend(ChatEngine):
    def __init__(self):
        self._loaded = False

    async def load(self, model_id): self._loaded = True
    async def unload(self): self._loaded = False
    async def is_loaded(self): return self._loaded

    async def generate(self, request):
        raise RuntimeError("backend boom")

    async def generate_stream(self, request):
        raise RuntimeError("stream boom")
        yield  # pragma: no cover


async def test_errors_reported_as_failed_result():
    ex = _executor(BrokenBackend(), max_concurrent=1)
    out = Collector()
    await ex.execute(_job(stream=False), send_chunk=Collector(), send_result=out)
    assert len(out.messages) == 1
    assert out.messages[0].status == "failed"
    assert "boom" in out.messages[0].error
    assert state.jobs_failed == 1


async def test_shutdown_waits_for_active_jobs():
    backend = SlowBackend()
    ex = _executor(backend, max_concurrent=4)
    sink = Collector()
    job_task = asyncio.create_task(
        ex.execute(_job(), send_chunk=sink, send_result=sink)
    )
    await asyncio.sleep(0.02)  # let the job start
    assert len(state.current_jobs) == 1
    await ex.shutdown(timeout=5)
    assert len(state.current_jobs) == 0
    await job_task
