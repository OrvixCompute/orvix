"""Client for the OpenCovenant trust MCP (Covenant Guard).

Covenant exposes on-chain trust facts for Solana wallets and agent assets over
a remote MCP endpoint (https://mcp.opencovenant.org/mcp). All tools are
read-only and take no credentials; the endpoint is an HTTP stream
(text/event-stream), one SSE ``event: message`` per JSON-RPC response.

Orvix uses this to attest a provider before trusting its node: an opt-in
registration-time reputation check whose verdict is stored on the ``nodes``
row (see NodeManager.register_node). The integration is fail-soft — a network
error, timeout, or malformed response never blocks node registration, it just
records that no attestation was obtained.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import settings
from app.logger import logger

# JSON-RPC protocol version spoken by the Covenant Guard server.
_PROTOCOL_VERSION = "2025-06-18"

# Hard upper bound on a single SSE data line, matching the daemon's 8 MB frame
# cap and the probe's observed response sizes (a few KB).
_MAX_DATA_LINE = 8 * 1024 * 1024


class CovenantError(Exception):
    """Raised when an MCP call fails or the server returns an error."""


@dataclass
class CovenantReputation:
    """Parsed result of ``covenant_reputation``.

    ``score`` is 0-1000. ``tier`` is one of bronze/silver/gold/diamond.
    ``source_fee_payer`` is the settlement fee-payer that grounded the score.
    """

    wallet: str
    score: int
    tier: str
    settled_jobs: int
    distinct_counterparties: int
    volume_micro_usdc: int
    source_fee_payer: Optional[str] = None

    @property
    def has_history(self) -> bool:
        return self.settled_jobs > 0 or self.distinct_counterparties > 0


@dataclass
class CovenantCheckResult:
    """Outcome of a single MCP tool call.

    ``ok`` is False when the server returned an error (isError) or the call
    failed locally (timeout/HTTP/parse). ``reputation`` is set only for
    successful ``covenant_reputation`` calls.
    """

    ok: bool
    tool: str
    error: Optional[str] = None
    raw: Optional[Any] = None
    reputation: Optional[CovenantReputation] = None


class CovenantService:
    """Minimal MCP client for the Covenant Guard endpoint.

    JSON-RPC over HTTP stream: POST a JSON-RPC request, read SSE frames until
    the response with a matching id arrives. One httpx client is reused across
    calls; the connection is closed with ``close()`` (the orchestrator does
    this on shutdown, see main.py).
    """

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = endpoint or settings.COVENANT_MCP_URL
        self._client = httpx.AsyncClient(timeout=settings.COVENANT_MCP_TIMEOUT_S)
        self._id = 0
        self._initialized = False

    async def close(self) -> None:
        await self._client.aclose()

    # --- transport ---------------------------------------------------------
    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request, return the ``result`` object.

        Raises CovenantError on HTTP errors, non-JSON-RPC responses, or an
        explicit JSON-RPC error object. Timeouts are re-raised as
        CovenantError so callers can fail-soft uniformly.
        """
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params,
        }
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            ) as resp:
                resp.raise_for_status()
                lines = [
                    line.strip()
                    async for line in resp.aiter_lines()
                    if line.strip() and not line.startswith(b":".decode())
                ]
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise CovenantError(f"MCP transport error: {exc}") from exc

        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if len(data) > _MAX_DATA_LINE:
                raise CovenantError("MCP response frame exceeds size limit")
            try:
                msg = json.loads(data)
            except json.JSONDecodeError as exc:
                raise CovenantError("MCP response is not valid JSON") from exc
            if msg.get("id") != self._id:
                continue
            if "error" in msg:
                err = msg["error"]
                raise CovenantError(
                    f"MCP error for {method}: {err.get('message', err)}"
                )
            return msg.get("result")

        raise CovenantError(f"No response frame for {method}")

    async def _initialize(self) -> None:
        """Perform the MCP handshake once per connection.

        The server answers initialize with its capabilities and instructions;
        we only need to prove the endpoint is a working MCP server. A failure
        here is not fatal — callers fail-soft — but it is logged.
        """
        if self._initialized:
            return
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "orvix", "version": "0.1.0"},
                },
            )
            self._initialized = True
        except CovenantError as exc:
            logger.warning("Covenant MCP initialize failed: {}", exc)

    # --- tools -------------------------------------------------------------
    async def check_reputation(self, wallet: str) -> CovenantCheckResult:
        """``covenant_reputation``: a wallet's 0-1000 on-chain settlement score."""
        await self._initialize()
        try:
            result = await self._request(
                "tools/call",
                {
                    "name": "covenant_reputation",
                    "arguments": {"wallet": wallet},
                },
            )
        except CovenantError as exc:
            return CovenantCheckResult(ok=False, tool="covenant_reputation", error=str(exc))

        if result.get("isError"):
            return CovenantCheckResult(
                ok=False,
                tool="covenant_reputation",
                error=_text_content(result),
                raw=result,
            )

        structured = result.get("structuredContent") or {}
        try:
            reputation = CovenantReputation(
                wallet=structured.get("wallet") or wallet,
                score=int(structured.get("score", 0)),
                tier=structured.get("tier", "bronze"),
                settled_jobs=int(structured.get("settled_jobs", 0)),
                distinct_counterparties=int(structured.get("distinct_counterparties", 0)),
                volume_micro_usdc=int(structured.get("volume_micro_usdc", 0)),
                source_fee_payer=structured.get("source_fee_payer"),
            )
        except (TypeError, ValueError) as exc:
            return CovenantCheckResult(
                ok=False,
                tool="covenant_reputation",
                error=f"Unexpected reputation payload: {exc}",
                raw=structured,
            )

        return CovenantCheckResult(
            ok=True, tool="covenant_reputation", raw=result, reputation=reputation
        )

    async def check_agent_passport(self, asset: str) -> CovenantCheckResult:
        """``covenant_agent_passport``: an MPL Core agent asset's on-chain identity."""
        await self._initialize()
        try:
            result = await self._request(
                "tools/call",
                {
                    "name": "covenant_agent_passport",
                    "arguments": {"asset": asset},
                },
            )
        except CovenantError as exc:
            return CovenantCheckResult(ok=False, tool="covenant_agent_passport", error=str(exc))

        if result.get("isError"):
            return CovenantCheckResult(
                ok=False,
                tool="covenant_agent_passport",
                error=_text_content(result),
                raw=result,
            )

        return CovenantCheckResult(ok=True, tool="covenant_agent_passport", raw=result)

    async def verify_attestation(self, attestation: Any) -> CovenantCheckResult:
        """``covenant_verify``: confirm a Covenant-signed attestation is genuine.

        ``attestation`` may be a JSON object or its string form.
        """
        await self._initialize()
        try:
            result = await self._request(
                "tools/call",
                {
                    "name": "covenant_verify",
                    "arguments": {"attestation": attestation},
                },
            )
        except CovenantError as exc:
            return CovenantCheckResult(ok=False, tool="covenant_verify", error=str(exc))

        if result.get("isError"):
            return CovenantCheckResult(
                ok=False,
                tool="covenant_verify",
                error=_text_content(result),
                raw=result,
            )

        return CovenantCheckResult(ok=True, tool="covenant_verify", raw=result)


def _text_content(result: dict) -> str:
    """Join the text blocks of an MCP content array, for error messages."""
    parts: list[str] = []
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if text:
            parts.append(text)
    return " | ".join(parts) if parts else "server reported an error"


# Singleton (created lazily so importing the module doesn't open a client).
_service: CovenantService | None = None


def get_covenant_service() -> CovenantService:
    global _service
    if _service is None:
        _service = CovenantService()
    return _service
