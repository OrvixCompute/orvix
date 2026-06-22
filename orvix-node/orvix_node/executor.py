"""Job execution: bridges orchestrator JobMessages to an InferenceBackend, with
concurrency limiting, metrics, and streaming/non-streaming result delivery.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from orvix_node.inference.base import GenerateRequest, InferenceBackend
from orvix_node.logger import logger
from orvix_node.protocol import JobChunkMessage, JobMessage, JobResultMessage
from orvix_node.state import state

SendFn = Callable[[object], Awaitable[None]]


class JobExecutor:
    def __init__(self, backend: InferenceBackend, max_concurrent: int = 4) -> None:
        self.backend = backend
        self._sem = asyncio.Semaphore(max_concurrent)
        self._model: str | None = None

    async def initialize(self, model: str) -> None:
        await self.backend.initialize(model)
        self._model = model

    async def execute(
        self, job: JobMessage, send_chunk: SendFn, send_result: SendFn
    ) -> None:
        """Run one job. Never raises — failures are reported via send_result."""
        await self._sem.acquire()
        await state.add_job(job.job_id, {"model": job.model, "stream": job.stream})
        started = time.perf_counter()
        try:
            req = GenerateRequest(
                messages=job.messages,
                max_tokens=job.max_tokens,
                temperature=job.temperature,
            )

            if job.stream:
                await self._run_streaming(job, req, send_chunk, started)
            else:
                await self._run_blocking(job, req, send_result, started)

        except Exception as exc:  # noqa: BLE001 — report, don't crash the agent
            logger.exception("Job {} failed: {}", job.job_id, exc)
            latency_ms = int((time.perf_counter() - started) * 1000)
            await state.record_failed()
            await send_result(
                JobResultMessage(
                    job_id=job.job_id,
                    status="failed",
                    error=str(exc),
                    latency_ms=latency_ms,
                )
            )
        finally:
            await state.remove_job(job.job_id)
            self._sem.release()

    async def _run_blocking(
        self, job: JobMessage, req: GenerateRequest, send_result: SendFn, started: float
    ) -> None:
        resp = await self.backend.generate(req)
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = {
            "id": f"chatcmpl-{job.job_id}",
            "object": "chat.completion",
            "model": job.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": resp.content},
                    "finish_reason": resp.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.prompt_tokens + resp.completion_tokens,
            },
        }
        await state.record_completed(resp.prompt_tokens + resp.completion_tokens)
        await send_result(
            JobResultMessage(
                job_id=job.job_id,
                status="completed",
                result=result,
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                latency_ms=latency_ms,
            )
        )

    async def _run_streaming(
        self, job: JobMessage, req: GenerateRequest, send_chunk: SendFn, started: float
    ) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in self.backend.generate_stream(req):
            if chunk.usage is not None:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens
            sse = {
                "id": f"chatcmpl-{job.job_id}",
                "object": "chat.completion.chunk",
                "model": job.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {} if chunk.is_final else {"content": chunk.delta_content},
                        "finish_reason": "stop" if chunk.is_final else None,
                    }
                ],
            }
            # OpenAI-style usage on the final chunk so the orchestrator can bill
            # streamed jobs on real token counts.
            if chunk.is_final and chunk.usage is not None:
                sse["usage"] = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.prompt_tokens + chunk.usage.completion_tokens,
                }
            await send_chunk(
                JobChunkMessage(job_id=job.job_id, chunk=sse, is_final=chunk.is_final)
            )
        await state.record_completed(prompt_tokens + completion_tokens)
        logger.info(
            "Job {} streamed ({} prompt + {} completion tokens)",
            job.job_id,
            prompt_tokens,
            completion_tokens,
        )

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Wait for active jobs to drain, then shut the backend down."""
        deadline = time.monotonic() + timeout
        while state.current_jobs and time.monotonic() < deadline:
            logger.info("Waiting for {} active job(s) to finish...", len(state.current_jobs))
            await asyncio.sleep(0.5)
        await self.backend.shutdown()
