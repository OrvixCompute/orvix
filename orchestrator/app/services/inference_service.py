"""Mock inference: token estimation, mock generation, and cost calculation.

Everything here is a stand-in until real GPU nodes are integrated. The billing
math, however, is the real thing — this is what lets the whole charging flow be
built and tested without any GPUs.
"""

import random
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterator, List

import tiktoken

from app.exceptions import ValidationError
from app.models.inference import SUPPORTED_MODELS, ChatMessage

# Price per 1K tokens, in USDC, keyed by model.
PRICING = {
    "qwen-2.5-7b": {"input": Decimal("0.0001"), "output": Decimal("0.0002")},
    "mistral-7b": {"input": Decimal("0.0001"), "output": Decimal("0.0002")},
    "llama-3.1-8b-quantized": {"input": Decimal("0.00008"), "output": Decimal("0.00016")},
}

# Tier -> discount fraction applied to total cost.
TIER_DISCOUNTS = {
    "bronze": Decimal("0.0"),
    "silver": Decimal("0.05"),
    "gold": Decimal("0.15"),
    "diamond": Decimal("0.25"),
}

_USDC_QUANT = Decimal("0.000001")  # 6 dp, matches numeric(20,6)

# tiktoken encoder is process-wide and threadsafe to read.
_encoder = tiktoken.get_encoding("cl100k_base")


def validate_model(model: str) -> None:
    if model not in SUPPORTED_MODELS:
        raise ValidationError(
            f"Model '{model}' is not supported. Choose one of: {', '.join(SUPPORTED_MODELS)}",
            error_code="model_not_found",
        )


def estimate_prompt_tokens(messages: List[ChatMessage]) -> int:
    """Approximate prompt tokens by encoding every message's content."""
    total = 0
    for m in messages:
        total += len(_encoder.encode(m.content))
    # Small per-message overhead, mirroring OpenAI's accounting.
    total += 4 * len(messages)
    return total


def _last_user_content(messages: List[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return messages[-1].content if messages else ""


def generate_mock(messages: List[ChatMessage], max_tokens: int) -> tuple[str, int]:
    """Produce mock content and a completion-token count."""
    # Clamp the range so it stays valid even when max_tokens < 50.
    low = min(50, max_tokens)
    high = min(max_tokens, 250)
    completion_tokens = random.randint(low, high)
    snippet = _last_user_content(messages)[:60]
    content = (
        "This is a mock response from Orvix. Real inference coming soon. "
        f"You asked about: {snippet}..."
    )
    return content, completion_tokens


def stream_mock_chunks(content: str) -> Iterator[str]:
    """Yield the mock content in ~5-token chunks (caller handles SSE framing + delay)."""
    tokens = _encoder.encode(content)
    for i in range(0, len(tokens), 5):
        yield _encoder.decode(tokens[i : i + 5])


def quantize_usdc(value: Decimal) -> Decimal:
    return value.quantize(_USDC_QUANT, rounding=ROUND_HALF_UP)


def calculate_cost(
    model: str, prompt_tokens: int, completion_tokens: int, tier: str
) -> Decimal:
    """Compute final USDC cost after applying the tier discount."""
    price = PRICING[model]
    input_cost = (Decimal(prompt_tokens) / 1000) * price["input"]
    output_cost = (Decimal(completion_tokens) / 1000) * price["output"]
    discount = TIER_DISCOUNTS.get(tier, Decimal("0.0"))
    total = (input_cost + output_cost) * (1 - discount)
    return quantize_usdc(total)


def estimate_max_cost(model: str, prompt_tokens: int, max_tokens: int, tier: str) -> Decimal:
    """Upper-bound cost used for the pre-generation balance check."""
    return calculate_cost(model, prompt_tokens, max_tokens, tier)
