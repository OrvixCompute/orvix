"""AI analysis of token scans, dispatched to the GPU network.

This is what turns raw on-chain data into *intelligence*: the scan results
(metadata, supply, price, liquidity, accumulation metrics) are handed to a chat
model running on an ORVX GPU node, which produces a narrative/risk summary.

Design:
- The job is dispatched over the normal node path (JobMessage -> node_manager),
  so monitoring agents create real GPU workload — the flywheel.
- Fail-soft: when no node serves the model, the job times out, or the node
  errors, the analysis is null. The scan endpoints still return everything
  else; intelligence is an additive layer.
- Results are cached in intel_scans (scan_type "intelligence") so repeated
  scans do not re-spend GPU time on every call.
- No billing is attached to these internal analysis jobs: they are product
  workload, not user inference. The jobs row is still written (is_mock=false)
  so network stats reflect the real compute the network served.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

from app.config import settings
from app.logger import logger
from app.models.protocol import JobMessage
from app.services import social_intel, token_intel
from app.services.node_manager import node_manager


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_prompt(mint: str, scan: dict, accumulation: Optional[dict], social: Optional[dict] = None) -> str:
    """Structured prompt: raw data in, intelligence out."""
    metadata = scan.get("metadata") or {}
    supply = scan.get("supply") or {}
    liquidity = scan.get("liquidity") or {}
    risk = scan.get("risk") or {}
    acc = (accumulation or {}).get("metrics") or {}

    prompt = (
        "You are ORVIX, a crypto market intelligence agent. Analyze this Solana "
        "token using ONLY the on-chain data below. Return a concise JSON object "
        "with keys: narrative (2-4 sentences describing the current market "
        "picture and emerging narrative), risk_flags (array of short strings), "
        "watch_next (1-2 sentences on what to watch). Do not invent data.\n\n"
        f"Token: {metadata.get('name') or mint} ({metadata.get('symbol') or 'unknown symbol'})\n"
        f"URI: {metadata.get('uri') or 'none'}\n"
        f"Supply: {supply.get('uiAmountString') or 'unknown'} (decimals {supply.get('decimals')})\n"
        f"Price USDC: {scan.get('price_usdc') or 'unknown'}\n"
        f"Liquidity (pools): {liquidity.get('pool_count')} pools, "
        f"~{liquidity.get('estimated_usdc') or 'unknown'} USDC\n"
        f"Top-10 holder share: {((scan.get('holders') or {}).get('top10_share')) if scan.get('holders') else 'unknown'}\n"
        f"Accumulation score: {accumulation.get('score') if accumulation else 'unknown'} "
        f"({accumulation.get('label') if accumulation else ''})\n"
        f"  - net inflow 7d: {acc.get('inflow_7d')}\n"
        f"  - buy txs 7d: {acc.get('buy_tx_7d')}\n"
        f"Risk warnings: {risk.get('warnings') or 'none'}\n"
    )

    if social:
        metrics = social.get("metrics") or {}
        prompt += (
            f"\nSocial signals:\n"
            f"  - Social score: {social.get('social_score', 0)}/100\n"
            f"  - DexScreener trending: {metrics.get('dex_trending', False)}\n"
            f"  - 24h volume (USD): {metrics.get('dex_volume_24h') or 'unknown'}\n"
            f"  - 24h price change: {metrics.get('dex_price_change_24h') or 'unknown'}%\n"
            f"  - Twitter followers: {metrics.get('twitter_followers') or 'unknown'}\n"
            f"  - Social sentiment: {metrics.get('social_sentiment') or 'unknown'}\n"
        )

    return prompt


def _extract_content(result) -> Optional[str]:
    """Pull assistant text out of a JobResultMessage's OpenAI-format payload."""
    payload = result.result or {}
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


async def generate_token_intelligence(
    db: Client, mint: str, *, bypass_cache: bool = False
) -> Optional[dict]:
    """Analyze a token via a GPU node. Returns analysis dict or None (fail-soft)."""
    if not bypass_cache:
        cached = token_intel._cache_get("intelligence", mint)
        if cached is not None:
            return cached
        cached = token_intel._db_cache_get(db, "intelligence", mint)
        if cached is not None:
            token_intel._cache_put("intelligence", mint, cached)
            return cached

    # Gather the raw inputs (cached scans; cheap).
    try:
        scan = await token_intel.scan_token(db, mint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intelligence: scan failed for {}: {}", mint, exc)
        return None
    try:
        accumulation = await token_intel.compute_accumulation(db, mint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intelligence: accumulation failed for {}: {}", mint, exc)
        accumulation = None

    # Social data is optional — failure yields None, prompt still works.
    try:
        social = await social_intel.analyze_social(db, mint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Intelligence: social analysis failed for {}: {}", mint, exc)
        social = None

    model = settings.INTEL_AI_MODEL
    # Bronze tier selection is fine for internal jobs; priority tiers only change
    # *which* least-loaded node is picked, not whether one is available.
    node = node_manager.select_node(model, "bronze")
    if node is None:
        logger.info(
            "Intelligence: no node serving {} for {} — analysis null",
            model,
            mint,
        )
        return None

    prompt = _build_prompt(mint, scan, accumulation, social=social)
    job = JobMessage(
        job_id=str(uuid.uuid4()),
        model=model,
        messages=[
            {"role": "system", "content": "You return only JSON."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=settings.INTEL_AI_MAX_TOKENS,
        temperature=settings.INTEL_AI_TEMPERATURE,
        stream=False,
        user_tier="bronze",
    )

    started = time.perf_counter()
    try:
        result = await node_manager.dispatch_job(node, job)
    except Exception as exc:  # noqa: BLE001 — fail-soft
        logger.warning("Intelligence: dispatch failed for {}: {}", mint, exc)
        return None

    if result.status == "failed":
        logger.warning("Intelligence: node reported failure for {}: {}", mint, result.error)
        return None

    content = _extract_content(result)
    if not content:
        logger.warning("Intelligence: empty content from node for {}", mint)
        return None

    parsed = _parse_json(content)
    if parsed is None:
        # Fall back to the raw text — still useful, still intelligence.
        parsed = {"narrative": content.strip(), "risk_flags": [], "watch_next": ""}

    payload = {
        "mint": mint,
        "model": model,
        "analysis": parsed,
        "generated_at": _now_iso(),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "node_id": node.node_id,
    }

    # Record the job row (is_mock=false) so network stats show real compute served.
    try:
        db.table("jobs").insert(
            {
                "status": "completed",
                "model": model,
                "is_mock": False,
                "node_id": node.node_id,
                "provider_id": node.provider_id,
                "prompt_tokens": result.prompt_tokens or 0,
                "completion_tokens": result.completion_tokens or 0,
                "cost_usdc": 0.0,
                "latency_ms": payload["latency_ms"],
                "metadata": {"kind": "token_intelligence", "mint": mint},
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
        logger.warning("Intelligence: job row write failed for {}: {}", mint, exc)

    token_intel._cache_put("intelligence", mint, payload)
    token_intel._db_cache_put(db, "intelligence", mint, payload)
    logger.info("Intelligence generated for {} (model={}, latency={}ms)", mint, model, payload["latency_ms"])
    return payload


def _parse_json(content: str) -> Optional[dict]:
    """Parse a JSON object out of a model reply (handles ```json fences)."""
    text = content.strip()
    if text.startswith("```"):
        # Strip code fences: ```json\n{...}\n```
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
