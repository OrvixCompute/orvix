"""Thin async wrappers around the Helius JSON-RPC endpoints we need."""

from typing import Any, Optional

import httpx

from app.config import settings
from app.logger import logger

# SPL Memo program (v2). Memo instructions are parsed under this program id.
MEMO_PROGRAM_IDS = {
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class SolanaService:
    """JSON-RPC client. One HTTP client is reused across calls."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._id = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        resp = await self._client.post(settings.helius_rpc_endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error for {method}: {data['error']}")
        return data.get("result")

    async def get_signatures_for_address(
        self, address: str, limit: int = 25, until: Optional[str] = None
    ) -> list[dict]:
        """Most-recent-first signatures touching `address`."""
        opts: dict[str, Any] = {"limit": limit}
        if until:
            opts["until"] = until
        return await self._rpc("getSignaturesForAddress", [address, opts]) or []

    async def get_parsed_transaction(self, signature: str) -> Optional[dict]:
        return await self._rpc(
            "getParsedTransaction",
            [signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
        )

    async def get_token_accounts_by_owner(self, owner: str, mint: str) -> list[dict]:
        return (
            await self._rpc(
                "getTokenAccountsByOwner",
                [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
            )
        ).get("value", []) or []

    # --- Parsing helpers ---------------------------------------------------
    @staticmethod
    def extract_memo(parsed_tx: dict) -> Optional[str]:
        """Pull the memo string out of a parsed transaction, if present."""
        try:
            instrs = parsed_tx["transaction"]["message"]["instructions"]
        except (KeyError, TypeError):
            return None
        for ix in instrs:
            if ix.get("program") == "spl-memo" or ix.get("programId") in MEMO_PROGRAM_IDS:
                parsed = ix.get("parsed")
                if isinstance(parsed, str):
                    return parsed
                # Some encodings nest the memo under "info".
                if isinstance(parsed, dict):
                    return parsed.get("memo") or parsed.get("info")
        return None

    @staticmethod
    def extract_spl_transfers(parsed_tx: dict, mint: str, recipient_owner: str) -> list[dict]:
        """Return SPL transfers of `mint` whose destination is owned by recipient.

        Each item: {"amount": Decimal-compatible str, "ui_amount": float, "source": str}.
        Uses pre/post token balances which reliably attribute amounts to the mint.
        """
        from decimal import Decimal

        results: list[dict] = []
        try:
            meta = parsed_tx["meta"]
            instrs = parsed_tx["transaction"]["message"]["instructions"]
        except (KeyError, TypeError):
            return results

        for ix in instrs:
            if ix.get("program") != "spl-token" and ix.get("programId") != TOKEN_PROGRAM_ID:
                continue
            parsed = ix.get("parsed") or {}
            if parsed.get("type") not in ("transfer", "transferChecked"):
                continue
            info = parsed.get("info", {})

            # transferChecked carries mint + tokenAmount directly.
            ix_mint = info.get("mint")
            if ix_mint and ix_mint != mint:
                continue

            token_amount = info.get("tokenAmount")
            if token_amount:
                ui_amount = token_amount.get("uiAmount")
                amount_str = token_amount.get("uiAmountString") or str(ui_amount)
            else:
                # Plain transfer: amount is in base units; resolve decimals from balances.
                raw = info.get("amount")
                decimals = _mint_decimals(meta, mint)
                if raw is None or decimals is None:
                    continue
                amount = Decimal(raw) / (Decimal(10) ** decimals)
                amount_str = format(amount, "f")
                ui_amount = float(amount)

            results.append(
                {
                    "amount": amount_str,
                    "ui_amount": ui_amount,
                    "source": info.get("source"),
                    "destination": info.get("destination"),
                    "authority": info.get("authority") or info.get("multisigAuthority"),
                }
            )
        # Note: destination is a token account, not the owner wallet. Recipient
        # ownership is confirmed by the caller against the treasury's token account.
        return results


def _mint_decimals(meta: dict, mint: str) -> Optional[int]:
    for bal in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if bal.get("mint") == mint:
            return bal.get("uiTokenAmount", {}).get("decimals")
    return None


# Singleton (created lazily so importing the module doesn't open a client).
_service: SolanaService | None = None


def get_solana_service() -> SolanaService:
    global _service
    if _service is None:
        _service = SolanaService()
    return _service
