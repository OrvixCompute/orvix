"""Job execution: bridges orchestrator JobMessages to an inference engine via the
ModelManager, with concurrency limiting, metrics, and streaming/non-streaming
result delivery.

The executor no longer owns a single backend; for each job it asks the
ModelManager for the engine that serves the job's model (loading/swapping it into
VRAM if needed), then runs generation on it. Only chat jobs flow here today —
image dispatch arrives with the Session 3 protocol changes.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Awaitable, Callable

from orvix_node.binary import register_image
from orvix_node.inference.base import ChatEngine, GenerateRequest, ImageEngine, ImageRequest
from orvix_node.inference.manager import ModelManager
from orvix_node.logger import logger
from orvix_node.protocol import (
    ImageJobCompleteMessage,
    ImageJobDispatchMessage,
    ImageJobFailedMessage,
    JobChunkMessage,
    JobMessage,
    JobResultMessage,
)
from orvix_node.state import state

SendFn = Callable[[object], Awaitable[None]]


class JobExecutor:
    def __init__(
        self,
        manager: ModelManager,
        max_concurrent: int = 4,
        image_tmp_dir: str = "/tmp/node-images",
        binary_base_url: str = "",
    ) -> None:
        self.manager = manager
        self._sem = asyncio.Semaphore(max_concurrent)
        self.image_tmp_dir = image_tmp_dir
        # Base URL the orchestrator uses to fetch generated images from this node.
        self.binary_base_url = binary_base_url.rstrip("/")

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

            async with self.manager.serving(job.model) as engine:
                if job.stream:
                    await self._run_streaming(job, req, engine, send_chunk, started)
                else:
                    await self._run_blocking(job, req, engine, send_result, started)

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
        self,
        job: JobMessage,
        req: GenerateRequest,
        engine: ChatEngine,
        send_result: SendFn,
        started: float,
    ) -> None:
        resp = await engine.generate(req)
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
        self,
        job: JobMessage,
        req: GenerateRequest,
        engine: ChatEngine,
        send_chunk: SendFn,
        started: float,
    ) -> None:
        prompt_tokens = 0
        completion_tokens = 0
        async for chunk in engine.generate_stream(req):
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

    async def execute_image(
        self,
        dispatch: ImageJobDispatchMessage,
        send_complete: SendFn,
        send_failed: SendFn,
    ) -> None:
        """Run one image job. Never raises — failures are reported via send_failed."""
        await self._sem.acquire()
        await state.add_job(dispatch.job_id, {"model": dispatch.model, "kind": "image"})
        try:
            req = ImageRequest(
                prompt=dispatch.prompt,
                width=dispatch.width,
                height=dispatch.height,
                num_inference_steps=dispatch.num_inference_steps,
                seed=dispatch.seed,
            )
            async with self.manager.serving(dispatch.model) as engine:
                assert isinstance(engine, ImageEngine)
                result = await engine.infer(req)

            image_id = str(uuid.uuid4())
            os.makedirs(self.image_tmp_dir, exist_ok=True)
            path = os.path.join(self.image_tmp_dir, f"{image_id}.png")
            with open(path, "wb") as f:
                f.write(result.png_bytes)
            # Authorize the orchestrator's one-time fetch of this image.
            register_image(image_id, dispatch.binary_token, path)

            binary_url = f"{self.binary_base_url}/v1/binary/image/{image_id}"
            await state.record_completed(0)
            await send_complete(
                ImageJobCompleteMessage(
                    job_id=dispatch.job_id,
                    image_id=image_id,
                    binary_url=binary_url,
                    metadata=result.metadata,
                )
            )
        except Exception as exc:  # noqa: BLE001 — report, don't crash the agent
            logger.exception("Image job {} failed: {}", dispatch.job_id, exc)
            await state.record_failed()
            await send_failed(
                ImageJobFailedMessage(job_id=dispatch.job_id, error=str(exc))
            )
        finally:
            await state.remove_job(dispatch.job_id)
            self._sem.release()

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Wait for active jobs to drain, then unload whatever engine is resident."""
        deadline = time.monotonic() + timeout
        while state.current_jobs and time.monotonic() < deadline:
            logger.info("Waiting for {} active job(s) to finish...", len(state.current_jobs))
            await asyncio.sleep(0.5)
        await self.manager.shutdown()
