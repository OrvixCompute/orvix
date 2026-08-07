"""What an image costs, and who is charged for it.

Chat prices per thousand tokens because tokens are what a chat request consumes.
The equivalent for an image is area: a measured 1024x1024 generation takes ~2.3 s
of GPU and peaks near 19.6 GiB of VRAM, and both scale with pixel count. So the
price is quoted per 1024x1024 image and scaled linearly from there, rather than
being a flat fee that would overcharge a 512x512 and undercharge a 1536x1536 by
the same factor of nine.

Tier discounts are the same ones chat applies, so staking buys the same thing on
both endpoints.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import settings
from app.services.inference_service import TIER_DISCOUNTS, quantize_usdc

# 1024 x 1024, the size the configured price is quoted for.
_BASELINE_PIXELS = Decimal(1024 * 1024)


def price_per_image(width: int, height: int, tier: str) -> Decimal:
    """Cost of one image of this size for a caller on this tier."""
    area_ratio = (Decimal(width) * Decimal(height)) / _BASELINE_PIXELS
    base = Decimal(str(settings.IMAGE_PRICE_USDC_PER_MEGAPIXEL)) * area_ratio
    discount = TIER_DISCOUNTS.get(tier, Decimal("0.0"))
    return quantize_usdc(base * (1 - discount))


def total_price(width: int, height: int, tier: str, units: int) -> Decimal:
    """Cost of `units` images of this size — what a single request may charge.

    Quantised per image and then multiplied, so the amount actually deducted for
    each image adds up to exactly what was quoted. Quantising only the total
    would leave a residue that the per-image deductions could never match.
    """
    return price_per_image(width, height, tier) * Decimal(units)
