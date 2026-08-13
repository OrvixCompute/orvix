"""Devnet end-to-end harness for the payment flow (Session 4).

Drives the REAL code paths against a live Solana devnet: it broadcasts real
devnet transactions and then runs the actual listener / payout / sweeper code
against them, verifying DB + on-chain state. Scenarios:

  A  top-up detection   — send USDC+memo to hot, run the listener, assert credit
  B  payout send+confirm — queue a withdrawal, run the worker (PAYOUT_STUB=false),
                           assert the provider wallet receives USDC + tx confirms
  D  hot sweep          — fund hot over threshold, sweep hot->main (SWEEP_STUB=false)
  E  failure -> refund  — force a pre-broadcast payout failure, assert refund

SAFETY
  * Refuses to run unless the RPC endpoint looks like devnet (override: --allow-mainnet).
  * Broadcasts/mutates only with --yes (otherwise it prints the plan and exits).
  * Uses a dedicated test user keyed on the source wallet address — it does not
    touch real user rows.

SETUP (see docs/operations/payment-flow.md)
  Point .env at devnet: HELIUS_RPC_URL=https://devnet.helius-rpc.com, devnet
  USDC_MINT_ADDRESS, treasury/payout keypair paths, USDC-funded wallets. Then set
  these harness env vars:
    E2E_SOURCE_KEYPAIR_PATH   funded devnet wallet (acts as the depositing user)
    E2E_PROVIDER_WALLET       payout destination owner (default: source owner)
    E2E_DEPOSIT_USDC          scenario A amount   (default 1.0)
    E2E_WITHDRAW_USDC         scenario B/E amount (default 0.5; keep < auto-approve
                              and >= MIN_WITHDRAW_AMOUNT_USDC in the devnet .env)
    E2E_SWEEP_TOPUP_USDC      scenario D top-up to push hot over threshold (default:
                              enough to exceed HOT_SWEEP_THRESHOLD_USDC)

RUN (from /opt/orvix/orchestrator or a devnet checkout, .env in CWD):
  python scripts/devnet_e2e.py --scenario a,b,d,e            # dry plan
  python scripts/devnet_e2e.py --scenario a,b,d,e --yes      # execute
  python scripts/devnet_e2e.py --all --yes
"""

import argparse
import asyncio
import base64
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

USDC_DECIMALS = 6
MEMO_PROGRAM_ID_STR = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
CONFIRM_ATTEMPTS = 30  # ~60s at 2s/poll


# --- low-level helpers ------------------------------------------------------

def _memo_ix(ctx, memo: str, signer):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        Pubkey.from_string(MEMO_PROGRAM_ID_STR),
        memo.encode("utf-8"),
        [AccountMeta(pubkey=signer, is_signer=True, is_writable=False)],
    )


async def _confirm(ctx, sig: str) -> bool:
    for _ in range(CONFIRM_ATTEMPTS):
        await asyncio.sleep(2)
        try:
            status = await ctx.sol.get_signature_status(sig)
        except Exception:  # noqa: BLE001 — transient RPC error, keep polling
            continue
        if status in ("confirmed", "finalized"):
            return True
    return False


async def _send_usdc_with_memo(ctx, source_kp, dest_owner_str: str, amount: Decimal, memo: str | None):
    """Broadcast a USDC transfer (source -> dest owner), creating the dest ATA and
    attaching an optional memo. Returns the signature."""
    from solders.hash import Hash
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from app.config import settings
    from app.services import spl

    mint = Pubkey.from_string(settings.USDC_MINT_ADDRESS)
    dest_owner = Pubkey.from_string(dest_owner_str)
    source_ata = spl.associated_token_address(source_kp.pubkey(), mint)
    dest_ata = spl.associated_token_address(dest_owner, mint)
    amount_raw = int((amount * (Decimal(10) ** USDC_DECIMALS)).to_integral_value())

    ixs = [
        spl.create_idempotent_ata_ix(source_kp.pubkey(), dest_owner, mint),
        spl.transfer_checked_ix(
            source_ata=source_ata,
            mint=mint,
            dest_ata=dest_ata,
            owner=source_kp.pubkey(),
            amount_raw=amount_raw,
            decimals=USDC_DECIMALS,
        ),
    ]
    if memo:
        ixs.append(_memo_ix(ctx, memo, source_kp.pubkey()))

    blockhash = Hash.from_string(await ctx.sol.get_latest_blockhash())
    tx = Transaction.new_signed_with_payer(ixs, source_kp.pubkey(), [source_kp], blockhash)
    return await ctx.sol.send_raw_transaction(base64.b64encode(bytes(tx)).decode())


def _ensure_test_user(ctx) -> dict:
    """Look up (or create) the e2e test user, keyed on the source wallet address."""
    wallet = ctx.source_owner
    res = ctx.db.table("users").select("*").eq("wallet_address", wallet).limit(1).execute()
    if res.data:
        return res.data[0]
    row = {"wallet_address": wallet, "email": "e2e-devnet@orvix.local", "is_provider": True}
    return ctx.db.table("users").insert(row).execute().data[0]


def _user_row(ctx, user_id: str) -> dict:
    return ctx.db.table("users").select("*").eq("id", user_id).limit(1).execute().data[0]


def _set_available(ctx, user_id: str, amount: Decimal) -> None:
    """Ensure available_usdc >= amount (top up the test user's provider balance)."""
    row = _user_row(ctx, user_id)
    cur = Decimal(str(row.get("available_usdc") or 0))
    if cur < amount:
        ctx.db.table("users").update({"available_usdc": float(amount)}).eq("id", user_id).execute()


# --- scenarios --------------------------------------------------------------

async def scenario_a(ctx) -> dict:
    """Top-up detection: deposit USDC+memo, run the listener, assert the credit."""
    from app.services.payment_listener import payment_listener

    sigs: list[str] = []
    user = _ensure_test_user(ctx)
    memo = f"orvx_{uuid.uuid4().hex[:12]}"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    ctx.db.table("topup_intents").insert(
        {
            "user_id": user["id"],
            "memo": memo,
            "expected_amount_usdc": float(ctx.deposit_usdc),
            "status": "pending",
            "expires_at": expires,
        }
    ).execute()

    before = Decimal(str(_user_row(ctx, user["id"])["balance_usdc"]))
    sig = await _send_usdc_with_memo(ctx, ctx.source_kp, ctx.hot_owner, ctx.deposit_usdc, memo)
    sigs.append(sig)
    if not await _confirm(ctx, sig):
        return {"name": "A top-up", "status": "FAIL", "notes": f"deposit {sig} never confirmed", "sigs": sigs}

    # Drive the real listener parse+match+credit path against the on-chain tx.
    await payment_listener._resolve_treasury_token_accounts()
    await payment_listener._process_signature(sig)

    after = Decimal(str(_user_row(ctx, user["id"])["balance_usdc"]))
    intent = (
        ctx.db.table("topup_intents").select("status").eq("memo", memo).limit(1).execute().data[0]
    )
    credited = after - before
    ok = credited == ctx.deposit_usdc and intent["status"] in ("fulfilled", "partial")
    return {
        "name": "A top-up",
        "status": "PASS" if ok else "FAIL",
        "notes": f"credited {credited} USDC, intent={intent['status']}",
        "sigs": sigs,
    }


async def scenario_b(ctx) -> dict:
    """Real payout: queue a withdrawal, run the worker, assert the provider is paid."""
    from app.config import settings
    from app.services.payout_service import payout_service

    if settings.PAYOUT_STUB:
        return {"name": "B payout", "status": "SKIP", "notes": "PAYOUT_STUB=true (set false for real send)", "sigs": []}

    sigs: list[str] = []
    user = _ensure_test_user(ctx)
    _set_available(ctx, user["id"], ctx.withdraw_usdc)

    provider_before = await ctx.sol.get_token_balance(ctx.provider_wallet, settings.USDC_MINT_ADDRESS)
    payout_service.queue_withdrawal(user["id"], ctx.withdraw_usdc, ctx.provider_wallet)
    await payout_service.process_pending_withdrawals()

    w = (
        ctx.db.table("withdrawals")
        .select("*")
        .eq("user_id", user["id"])
        .order("queued_at", desc=True)
        .limit(1)
        .execute()
        .data[0]
    )
    if w["status"] != "completed":
        return {
            "name": "B payout",
            "status": "FAIL",
            "notes": f"withdrawal status={w['status']} err={w.get('error_message')}",
            "sigs": [w.get("solana_signature")] if w.get("solana_signature") else [],
        }
    sigs.append(w["solana_signature"])
    provider_after = await ctx.sol.get_token_balance(ctx.provider_wallet, settings.USDC_MINT_ADDRESS)
    delta = provider_after - provider_before
    ok = delta >= ctx.withdraw_usdc
    return {
        "name": "B payout",
        "status": "PASS" if ok else "FAIL",
        "notes": f"provider +{delta} USDC (expected {ctx.withdraw_usdc}), sig confirmed",
        "sigs": sigs,
    }


async def scenario_d(ctx) -> dict:
    """Hot sweep: push hot over threshold, sweep hot->main, assert main grows."""
    from solders.hash import Hash
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from app.config import settings
    from app.services import spl
    from app.services.hot_sweeper import hot_sweeper

    if settings.TREASURY_SWEEP_STUB:
        return {"name": "D hot sweep", "status": "SKIP", "notes": "TREASURY_SWEEP_STUB=true", "sigs": []}
    main_pub = ctx.wallet_service.public_key("main")
    if not main_pub:
        return {"name": "D hot sweep", "status": "SKIP", "notes": "TREASURY_MAIN_PUBLIC unset", "sigs": []}

    sigs: list[str] = []
    threshold = Decimal(str(settings.HOT_SWEEP_THRESHOLD_USDC))

    # Ensure main has a USDC ATA (cold wallet can't self-create; source pays rent).
    mint = Pubkey.from_string(settings.USDC_MINT_ADDRESS)
    ata_ix = spl.create_idempotent_ata_ix(
        ctx.source_kp.pubkey(), Pubkey.from_string(main_pub), mint
    )
    blockhash = Hash.from_string(await ctx.sol.get_latest_blockhash())
    ata_tx = Transaction.new_signed_with_payer(
        [ata_ix], ctx.source_kp.pubkey(), [ctx.source_kp], blockhash
    )
    await ctx.sol.send_raw_transaction(base64.b64encode(bytes(ata_tx)).decode())

    hot_before = await ctx.wallet_service.get_usdc_balance("hot")
    if hot_before <= threshold:
        topup = ctx.sweep_topup_usdc or (threshold - hot_before + Decimal("1"))
        fund_sig = await _send_usdc_with_memo(ctx, ctx.source_kp, ctx.hot_owner, topup, None)
        sigs.append(fund_sig)
        if not await _confirm(ctx, fund_sig):
            return {"name": "D hot sweep", "status": "FAIL", "notes": "hot funding tx unconfirmed", "sigs": sigs}

    main_before = await ctx.wallet_service.get_usdc_balance("main")
    result = await hot_sweeper.run_once(ctx.db)
    if not result.get("swept"):
        return {"name": "D hot sweep", "status": "FAIL", "notes": f"not swept: {result}", "sigs": sigs}
    sigs.append(result["signature"])
    await _confirm(ctx, result["signature"])
    main_after = await ctx.wallet_service.get_usdc_balance("main")
    ok = main_after > main_before
    return {
        "name": "D hot sweep",
        "status": "PASS" if ok else "FAIL",
        "notes": f"swept {result.get('amount_usdc')} USDC, main {main_before}->{main_after}",
        "sigs": sigs,
    }


async def scenario_e(ctx) -> dict:
    """Failure handling: force a pre-broadcast payout failure, assert full refund."""
    from app.services import payout_service as payout_mod
    from app.services.payout_service import payout_service
    from app.services.wallet import wallet_service

    user = _ensure_test_user(ctx)
    _set_available(ctx, user["id"], ctx.withdraw_usdc)
    avail_before = Decimal(str(_user_row(ctx, user["id"])["available_usdc"]))

    w = payout_service.queue_withdrawal(user["id"], ctx.withdraw_usdc, ctx.provider_wallet)

    async def _boom(*a, **k):  # simulate an RPC reject BEFORE broadcast
        raise RuntimeError("simulated pre-broadcast failure (e2e scenario E)")

    original = wallet_service.send_usdc
    wallet_service.send_usdc = _boom
    try:
        await payout_service._process_one(payout_mod.get_supabase(), w)
    finally:
        wallet_service.send_usdc = original

    row = ctx.db.table("withdrawals").select("*").eq("id", w["id"]).limit(1).execute().data[0]
    avail_after = Decimal(str(_user_row(ctx, user["id"])["available_usdc"]))
    ok = row["status"] == "failed" and avail_after == avail_before
    return {
        "name": "E fail->refund",
        "status": "PASS" if ok else "FAIL",
        "notes": f"status={row['status']}, available {avail_before}->{avail_after} (refunded)",
        "sigs": [],
    }


SCENARIOS = {"a": scenario_a, "b": scenario_b, "d": scenario_d, "e": scenario_e}


# --- driver -----------------------------------------------------------------

def _build_ctx():
    from app.config import settings
    from app.database import get_supabase
    from app.services.solana_service import get_solana_service
    from app.services.wallet import load_keypair, wallet_service

    src_path = os.environ.get("E2E_SOURCE_KEYPAIR_PATH", "")
    if not src_path:
        raise SystemExit("E2E_SOURCE_KEYPAIR_PATH is required")
    source_kp = load_keypair(src_path)
    source_owner = str(source_kp.pubkey())

    return SimpleNamespace(
        db=get_supabase(),
        sol=get_solana_service(),
        wallet_service=wallet_service,
        source_kp=source_kp,
        source_owner=source_owner,
        hot_owner=settings.TREASURY_WALLET_ADDRESS,
        provider_wallet=os.environ.get("E2E_PROVIDER_WALLET") or source_owner,
        deposit_usdc=Decimal(os.environ.get("E2E_DEPOSIT_USDC", "1.0")),
        withdraw_usdc=Decimal(os.environ.get("E2E_WITHDRAW_USDC", "0.5")),
        sweep_topup_usdc=Decimal(os.environ["E2E_SWEEP_TOPUP_USDC"])
        if os.environ.get("E2E_SWEEP_TOPUP_USDC")
        else None,
    )


def _preflight(ctx, allow_mainnet: bool) -> None:
    from app.config import settings

    endpoint = settings.solana_rpc_endpoint
    if "devnet" not in endpoint and not allow_mainnet:
        raise SystemExit(
            f"RPC endpoint does not look like devnet ({endpoint!r}). "
            "Point SOLANA_RPC_URL (or HELIUS_RPC_URL) at devnet, or pass --allow-mainnet to override."
        )
    if not settings.USDC_MINT_ADDRESS:
        raise SystemExit("USDC_MINT_ADDRESS not configured")
    if not ctx.hot_owner:
        raise SystemExit("TREASURY_WALLET_ADDRESS (hot) not configured")


def _print_plan(ctx, selected: list[str]) -> None:
    from app.config import settings

    print("Devnet E2E plan (DRY — pass --yes to execute):")
    print(f"  RPC          : {settings.solana_rpc_endpoint}")
    print(f"  USDC mint    : {settings.USDC_MINT_ADDRESS}")
    print(f"  hot wallet   : {ctx.hot_owner}")
    print(f"  payout stub  : {settings.PAYOUT_STUB}   sweep stub: {settings.TREASURY_SWEEP_STUB}")
    print(f"  source wallet: {ctx.source_owner}")
    print(f"  provider dest: {ctx.provider_wallet}")
    print(f"  amounts      : deposit={ctx.deposit_usdc} withdraw={ctx.withdraw_usdc} USDC")
    print(f"  scenarios    : {', '.join(selected)}")


def _print_report(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print(f"{'Scenario':<16}{'Status':<8}Notes")
    print("-" * 72)
    for r in results:
        print(f"{r['name']:<16}{r['status']:<8}{r['notes']}")
        for s in r.get("sigs") or []:
            if s:
                print(f"{'':<24}sig: {s}")
    print("=" * 72)


async def _run(selected: list[str], ctx) -> int:
    results = []
    for key in selected:
        try:
            results.append(await SCENARIOS[key](ctx))
        except Exception as exc:  # noqa: BLE001 — one scenario failing must not abort the rest
            results.append({"name": f"{key} error", "status": "ERROR", "notes": str(exc), "sigs": []})
    _print_report(results)
    await ctx.sol.close()
    return 0 if all(r["status"] in ("PASS", "SKIP") for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Devnet e2e harness for the payment flow")
    parser.add_argument("--scenario", default="", help="comma list of a,b,d,e")
    parser.add_argument("--all", action="store_true", help="run all scenarios")
    parser.add_argument("--yes", action="store_true", help="actually broadcast/mutate")
    parser.add_argument("--allow-mainnet", action="store_true", help="skip the devnet guard")
    args = parser.parse_args()

    selected = ["a", "b", "d", "e"] if args.all else [
        s.strip().lower() for s in args.scenario.split(",") if s.strip()
    ]
    bad = [s for s in selected if s not in SCENARIOS]
    if not selected or bad:
        raise SystemExit(f"Select scenarios from a,b,d,e (got {selected}, invalid {bad})")

    ctx = _build_ctx()
    _preflight(ctx, args.allow_mainnet)

    if not args.yes:
        _print_plan(ctx, selected)
        print("\nDry run — no transactions sent. Re-run with --yes to execute.")
        return 0

    return asyncio.run(_run(selected, ctx))


if __name__ == "__main__":
    sys.exit(main())
