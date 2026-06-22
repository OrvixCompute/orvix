"""vLLM inference backend — SKELETON.

All methods raise NotImplementedError for now. The full implementation arrives in
Prompt 7, once a CUDA GPU is available. A reference implementation is included
below in a comment block so enabling real inference is mostly uncommenting.
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from orvix_node.inference.base import (
    GenerateChunk,
    GenerateRequest,
    GenerateResponse,
)

_TODO = "TODO: implement when GPU is available (Prompt 7)"


class VLLMBackend:
    def __init__(
        self,
        model: str,
        gpu_memory_utilization: float = 0.85,
        max_model_len: int = 4096,
        dtype: str = "auto",
        quantization: Optional[str] = None,
        trust_remote_code: bool = False,
    ) -> None:
        self.model = model
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len
        self.dtype = dtype
        self.quantization = quantization
        self.trust_remote_code = trust_remote_code
        self._engine = None

    async def initialize(self, model: str) -> None:
        raise NotImplementedError(_TODO)

    async def is_ready(self) -> bool:
        raise NotImplementedError(_TODO)

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        raise NotImplementedError(_TODO)

    def generate_stream(self, request: GenerateRequest) -> AsyncIterator[GenerateChunk]:
        raise NotImplementedError(_TODO)

    async def shutdown(self) -> None:
        raise NotImplementedError(_TODO)


# ============================================================================
# REFERENCE IMPLEMENTATION (Prompt 7) — uncomment & adapt once vLLM is installed.
# ============================================================================
#
# import time
# import uuid
# from vllm import AsyncLLMEngine, AsyncEngineArgs, SamplingParams
# from orvix_node.inference.base import GenerateUsage
# from orvix_node.logger import logger
# from orvix_node.state import state
#
# class VLLMBackend:
#     def __init__(self, model, gpu_memory_utilization=0.85, max_model_len=4096,
#                  dtype="auto", quantization=None, trust_remote_code=False):
#         self.model = model
#         self.gpu_memory_utilization = gpu_memory_utilization
#         self.max_model_len = max_model_len
#         self.dtype = dtype
#         self.quantization = quantization
#         self.trust_remote_code = trust_remote_code
#         self._engine = None
#
#     async def initialize(self, model):
#         start = time.perf_counter()
#         args = AsyncEngineArgs(
#             model=model,
#             gpu_memory_utilization=self.gpu_memory_utilization,
#             max_model_len=self.max_model_len,
#             dtype=self.dtype,
#             quantization=self.quantization,
#             trust_remote_code=self.trust_remote_code,
#         )
#         self._engine = AsyncLLMEngine.from_engine_args(args)
#         self.model = model
#         logger.info("vLLM engine initialized for {} in {:.1f}s",
#                     model, time.perf_counter() - start)
#
#     async def is_ready(self):
#         return self._engine is not None
#
#     def _build_prompt(self, messages):
#         tok = self._engine.engine.tokenizer
#         return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
#
#     async def generate(self, request):
#         prompt = self._build_prompt(request.messages)
#         params = SamplingParams(max_tokens=request.max_tokens, temperature=request.temperature)
#         final = None
#         async for out in self._engine.generate(prompt, params, request_id=str(uuid.uuid4())):
#             final = out
#         output = final.outputs[0]
#         return GenerateResponse(
#             content=output.text,
#             prompt_tokens=len(final.prompt_token_ids),
#             completion_tokens=len(output.token_ids),
#             finish_reason=output.finish_reason or "stop",
#         )
#
#     async def generate_stream(self, request):
#         prompt = self._build_prompt(request.messages)
#         params = SamplingParams(max_tokens=request.max_tokens, temperature=request.temperature)
#         prev = ""
#         final = None
#         async for out in self._engine.generate(prompt, params, request_id=str(uuid.uuid4())):
#             text = out.outputs[0].text
#             delta, prev = text[len(prev):], text
#             final = out
#             if delta:
#                 yield GenerateChunk(delta_content=delta, is_final=False)
#         output = final.outputs[0]
#         yield GenerateChunk(
#             delta_content="", is_final=True,
#             usage=GenerateUsage(prompt_tokens=len(final.prompt_token_ids),
#                                 completion_tokens=len(output.token_ids)),
#         )
#
#     async def shutdown(self):
#         self._engine = None
