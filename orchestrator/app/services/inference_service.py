"""Token estimation and cost calculation for inference billing."""

from decimal import Decimal, ROUND_HALF_UP
from typing import List

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
        # content is null on an assistant turn that only calls tools, and on
        # those turns the tokens live in the call arguments instead.
        total += len(_encoder.encode(m.content or ""))
        if m.tool_calls:
            for call in m.tool_calls:
                total += len(_encoder.encode(call.function.name))
                total += len(_encoder.encode(call.function.arguments))
    # Small per-message overhead, mirroring OpenAI's accounting.
    total += 4 * len(messages)
    return total


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
