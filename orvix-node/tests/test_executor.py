"""Tests for JobExecutor driving engines through the ModelManager."""

import asyncio
from typing import AsyncIterator

import pytest

from orvix_node import binary
from orvix_node.executor import JobExecutor
from orvix_node.inference.base import (
    ChatEngine,
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
    ImageEngine,
    ImageRequest,
    ImageResult,
    GenerateUsage,
    VideoEngine,
    VideoRequest,
    VideoResult,
)
from orvix_node.inference.manager import ModelManager
from orvix_node.inference.mock import MockBackend
from orvix_node.protocol import (
    ImageJobDispatchMessage,
    JobMessage,
    VideoJobDispatchMessage,
)
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


# --- image jobs ------------------------------------------------------------
class FakeImageEngine(ImageEngine):
    def __init__(self):
        self._loaded = False

    async def load(self, model_id): self._loaded = True
    async def unload(self): self._loaded = False
    async def is_loaded(self): return self._loaded

    async def infer(self, request: ImageRequest) -> ImageResult:
        return ImageResult(png_bytes=b"IMGDATA", metadata={"seed": request.seed})


def _image_dispatch(job_id="ij1"):
    return ImageJobDispatchMessage(
        job_id=job_id, model="flux-schnell", prompt="a cat", binary_token="tok"
    )


async def test_execute_image_success(tmp_path):
    binary._registry.clear()
    mgr = ModelManager({"image": FakeImageEngine()})
    ex = JobExecutor(mgr, image_tmp_dir=str(tmp_path), binary_base_url="http://node:9000")
    completes, fails = Collector(), Collector()

    await ex.execute_image(_image_dispatch(), send_complete=completes, send_failed=fails)

    assert len(fails.messages) == 0
    assert len(completes.messages) == 1
    msg = completes.messages[0]
    assert msg.type == "job.image.complete"
    assert msg.binary_url == f"http://node:9000/v1/binary/image/{msg.image_id}"
    # File written and registered under the dispatch token for the binary fetch.
    assert (tmp_path / f"{msg.image_id}.png").read_bytes() == b"IMGDATA"
    assert binary._registry[msg.image_id]["token"] == "tok"


class SlowImageEngine(ImageEngine):
    """Tracks max concurrency to verify the image-specific semaphore."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self._loaded = False

    async def load(self, model_id): self._loaded = True
    async def unload(self): self._loaded = False
    async def is_loaded(self): return self._loaded

    async def infer(self, request: ImageRequest) -> ImageResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.1)
        finally:
            self.active -= 1
        return ImageResult(png_bytes=b"IMGDATA", metadata={})


async def test_image_jobs_serialize_by_default(tmp_path):
    # One diffusion pass at a time: several GB of transient VRAM per job means
    # two concurrent generations OOM a card that handles one fine.
    engine = SlowImageEngine()
    ex = JobExecutor(
        ModelManager({"image": engine}), image_tmp_dir=str(tmp_path), binary_base_url="http://n:9000"
    )
    sink = Collector()
    await asyncio.gather(
        *[
            ex.execute_image(_image_dispatch(job_id=f"ij{i}"), send_complete=sink, send_failed=sink)
            for i in range(3)
        ]
    )
    assert engine.max_active == 1
    assert len(sink.messages) == 3
    assert all(m.type == "job.image.complete" for m in sink.messages)


async def test_image_limit_is_configurable(tmp_path):
    engine = SlowImageEngine()
    ex = JobExecutor(
        ModelManager({"image": engine}),
        image_tmp_dir=str(tmp_path),
        binary_base_url="http://n:9000",
        max_concurrent_image=2,
    )
    sink = Collector()
    await asyncio.gather(
        *[
            ex.execute_image(_image_dispatch(job_id=f"ij{i}"), send_complete=sink, send_failed=sink)
            for i in range(3)
        ]
    )
    assert engine.max_active == 2


async def test_image_job_does_not_consume_a_chat_slot(tmp_path):
    """A chat job must be able to start while an image job is mid-generation.

    Both engines wait on the other's start signal, so this only completes if the
    two limits are genuinely separate — a shared semaphore deadlocks here.
    """
    chat_started, image_started = asyncio.Event(), asyncio.Event()

    class HandshakeChat(ChatEngine):
        async def load(self, model_id): pass
        async def unload(self): pass
        async def is_loaded(self): return True

        async def generate(self, request: GenerateRequest) -> GenerateResponse:
            chat_started.set()
            await asyncio.wait_for(image_started.wait(), timeout=5)
            return GenerateResponse(content="x", prompt_tokens=1, completion_tokens=1)

        async def generate_stream(self, request) -> AsyncIterator[GenerateChunk]:
            yield GenerateChunk(is_final=True, usage=GenerateUsage(prompt_tokens=1, completion_tokens=1))

    class HandshakeImage(ImageEngine):
        async def load(self, model_id): pass
        async def unload(self): pass
        async def is_loaded(self): return True

        async def infer(self, request: ImageRequest) -> ImageResult:
            image_started.set()
            await asyncio.wait_for(chat_started.wait(), timeout=5)
            return ImageResult(png_bytes=b"IMGDATA", metadata={})

    # max_resident=2 mirrors concurrent_engines: both engines stay in VRAM.
    mgr = ModelManager({"chat": HandshakeChat(), "image": HandshakeImage()}, max_resident=2)
    # A single chat slot: if image jobs took one, the two could never overlap.
    ex = JobExecutor(
        mgr, max_concurrent=1, image_tmp_dir=str(tmp_path), binary_base_url="http://n:9000"
    )
    sink = Collector()

    await asyncio.gather(
        ex.execute(_job(), send_chunk=sink, send_result=sink),
        ex.execute_image(_image_dispatch(), send_complete=sink, send_failed=sink),
    )

    assert {m.type for m in sink.messages} == {"job_result", "job.image.complete"}
    assert all(getattr(m, "status", "completed") == "completed" for m in sink.messages)


async def test_execute_image_no_engine_fails(tmp_path):
    # Manager has no image engine -> acquire raises -> failure reported.
    mgr = ModelManager({"chat": MockBackend("p")})
    ex = JobExecutor(mgr, image_tmp_dir=str(tmp_path), binary_base_url="http://n:9000")
    completes, fails = Collector(), Collector()

    await ex.execute_image(_image_dispatch(), send_complete=completes, send_failed=fails)

    assert len(completes.messages) == 0
    assert len(fails.messages) == 1
    assert fails.messages[0].type == "job.image.failed"


# --- embeddings ------------------------------------------------------------
# The lint caught a broken import here that the suite did not, because nothing
# exercised this path. These tests close that gap.


class _FakeEmbedEngine:
    engine_type = "embedding"

    def __init__(self, fail=False):
        self.fail = fail
        self.seen = None

    async def embed(self, request):
        from orvix_node.inference.base import EmbeddingResult

        if self.fail:
            raise RuntimeError("engine exploded")
        self.seen = list(request.input)
        return EmbeddingResult(
            embeddings=[[0.5, 0.5] for _ in request.input],
            metadata={"dimensions": 2},
        )


class _EmbedManager:
    def __init__(self, engine):
        self.engine = engine

    def serving(self, model):
        engine = self.engine

        class _Ctx:
            async def __aenter__(self):
                return engine

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _embed_job(inputs):
    from orvix_node.protocol import EmbeddingJobDispatchMessage

    return EmbeddingJobDispatchMessage(
        job_id="job-e1", model="orvix-embed-1", input=inputs
    )


@pytest.mark.asyncio
async def test_execute_embedding_returns_vectors_in_a_job_result():
    """Answers with an ordinary job_result — the same shape non-streaming chat
    uses, so the orchestrator needs no second completion path."""
    from orvix_node.executor import JobExecutor

    engine = _FakeEmbedEngine()
    ex = JobExecutor(_EmbedManager(engine))
    sent = []

    async def _send(msg):
        sent.append(msg)

    await ex.execute_embedding(_embed_job(["a", "b"]), send_result=_send)

    assert len(sent) == 1
    msg = sent[0]
    assert msg.type == "job_result"
    assert msg.status == "completed"
    assert msg.result["embeddings"] == [[0.5, 0.5], [0.5, 0.5]]
    assert engine.seen == ["a", "b"], "input order is the contract"


@pytest.mark.asyncio
async def test_execute_embedding_reports_failure_without_raising():
    """A failing engine must come back as a failed result, not crash the agent."""
    from orvix_node.executor import JobExecutor

    ex = JobExecutor(_EmbedManager(_FakeEmbedEngine(fail=True)))
    sent = []

    async def _send(msg):
        sent.append(msg)

    await ex.execute_embedding(_embed_job(["a"]), send_result=_send)

    assert len(sent) == 1
    assert sent[0].status == "failed"
    assert "engine exploded" in sent[0].error


# --- video jobs ------------------------------------------------------------
class FakeVideoEngine(VideoEngine):
    def __init__(self):
        self._loaded = False

    async def load(self, model_id):
        self._loaded = True

    async def unload(self):
        self._loaded = False

    async def is_loaded(self):
        return self._loaded

    async def infer(self, request: VideoRequest) -> VideoResult:
        return VideoResult(
            mp4_bytes=b"MP4DATA",
            metadata={"num_frames": request.num_frames, "seed": request.seed},
        )


def _video_dispatch(job_id="vj1"):
    return VideoJobDispatchMessage(
        job_id=job_id,
        model="orvix-video-1",
        prompt="a cat walking",
        binary_token="tok",
    )


async def test_execute_video_success(tmp_path):
    binary._registry.clear()
    mgr = ModelManager({"video": FakeVideoEngine()})
    ex = JobExecutor(mgr, video_tmp_dir=str(tmp_path), binary_base_url="http://node:9000")
    completes, fails = Collector(), Collector()

    await ex.execute_video(_video_dispatch(), send_complete=completes, send_failed=fails)

    assert len(fails.messages) == 0
    assert len(completes.messages) == 1
    msg = completes.messages[0]
    assert msg.type == "job.video.complete"
    assert msg.binary_url == f"http://node:9000/v1/binary/video/{msg.video_id}"
    # File written and registered under the dispatch token for the binary fetch.
    assert (tmp_path / f"{msg.video_id}.mp4").read_bytes() == b"MP4DATA"
    assert binary._registry[msg.video_id]["token"] == "tok"


class SlowVideoEngine(VideoEngine):
    """Tracks max concurrency to verify the video-specific semaphore."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self._loaded = False

    async def load(self, model_id):
        self._loaded = True

    async def unload(self):
        self._loaded = False

    async def is_loaded(self):
        return self._loaded

    async def infer(self, request: VideoRequest) -> VideoResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.1)
        finally:
            self.active -= 1
        return VideoResult(mp4_bytes=b"MP4DATA", metadata={})


async def test_video_jobs_serialize_by_default(tmp_path):
    # A clip holds the GPU for minutes; max_concurrent_video_jobs defaults to 1.
    engine = SlowVideoEngine()
    ex = JobExecutor(
        ModelManager({"video": engine}), video_tmp_dir=str(tmp_path), binary_base_url="http://n:9000"
    )
    sink = Collector()
    await asyncio.gather(
        *[
            ex.execute_video(_video_dispatch(job_id=f"vj{i}"), send_complete=sink, send_failed=sink)
            for i in range(3)
        ]
    )
    assert engine.max_active == 1
    assert len(sink.messages) == 3
    assert all(m.type == "job.video.complete" for m in sink.messages)


async def test_execute_video_no_engine_fails(tmp_path):
    # Manager has no video engine -> acquire raises -> failure reported.
    mgr = ModelManager({"chat": MockBackend("p")})
    ex = JobExecutor(mgr, video_tmp_dir=str(tmp_path), binary_base_url="http://n:9000")
    completes, fails = Collector(), Collector()

    await ex.execute_video(_video_dispatch(), send_complete=completes, send_failed=fails)

    assert len(completes.messages) == 0
    assert len(fails.messages) == 1
    assert fails.messages[0].type == "job.video.failed"
