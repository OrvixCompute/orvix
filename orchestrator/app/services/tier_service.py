"""Stake-based tier system.

Per the whitepaper, a user's tier is derived from how much ORVX they have STAKED
(``users.staked_orvx``), not from their wallet balance. Thresholds:

    bronze   0       <= staked < 10,000
    silver   10,000  <= staked < 50,000
    gold     50,000  <= staked < 250,000
    diamond  250,000 <= staked

This module is the single source of truth for those thresholds. The matching
discount fractions live in :mod:`app.services.inference_service` (TIER_DISCOUNTS),
which is wired to read from here.
"""

from decimal import Decimal

# Ordered low -> high: (tier_name, minimum_staked_orvx_inclusive).
TIER_THRESHOLDS: list[tuple[str, Decimal]] = [
    ("bronze", Decimal("0")),
    ("silver", Decimal("10000")),
    ("gold", Decimal("50000")),
    ("diamond", Decimal("250000")),
]


def _as_decimal(staked) -> Decimal:
    return Decimal(str(staked if staked is not None else 0))


def tier_for_stake(staked) -> str:
    """Return the tier name for a given staked-ORVX amount."""
    amount = _as_decimal(staked)
    name = TIER_THRESHOLDS[0][0]
    for tier_name, threshold in TIER_THRESHOLDS:
        if amount >= threshold:
            name = tier_name
    return name


def discount_pct_for_tier(tier: str) -> int:
    """Integer discount percent for a tier (mirrors inference_service.TIER_DISCOUNTS)."""
    # Imported lazily to avoid a circular import at module load.
    from app.services.inference_service import TIER_DISCOUNTS

    return int(TIER_DISCOUNTS.get(tier, Decimal("0")) * 100)


def next_tier_info(staked) -> dict | None:
    """Describe the next tier up, or ``None`` if already at the top (diamond)."""
    amount = _as_decimal(staked)
    for tier_name, threshold in TIER_THRESHOLDS:
        if amount < threshold:
            return {
                "name": tier_name,
                "required_stake": format(threshold, "f"),
                "additional_needed": format(threshold - amount, "f"),
            }
    return None
