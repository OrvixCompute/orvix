# orvix-staking — non-custodial user staking program

Anchor (v2) program that locks ORVX for a user-chosen lock period. The program
owns the vault; no operator key can move staked tokens.

## Instructions

| Instruction | Args | Behaviour |
|---|---|---|
| `stake` | `amount: u64`, `lock_days: i64` | Transfers ORVX from the user's ATA into the program vault; records/updates the user's `StakeAccount` PDA. `lock_days` must be 3, 7, or 14. The deadline only ever extends. |
| `unstake` | `amount: u64` | Refuses before `stake_locked_until`; otherwise transfers ORVX back to the user's ATA. Partial unstakes allowed. |

## Accounts

- `StakeAccount` PDA — seeds `["stake", owner]`; zero-copy Pod layout:
  `owner: Address`, `amount: PodU64`, `stake_locked_until: PodI64`,
  `created_at: PodI64`.
- Vault — token account PDA at seeds `["vault"]`; its authority is a
  **separate** PDA at seeds `["vault_authority"]` (distinct addresses, so the
  account list has no duplicate mutable accounts).

## Build

The bundled `anchor` CLI ships a Rust toolchain too old for the current
dependency tree (solana-program 2.x needs Rust 1.85+). Use `build.sh`, which
invokes the official `cargo-build-sbf` from the solana 3.1.10 release
(platform-tools rustc 1.89) and stages the artifact where `anchor deploy`
expects it:

```bash
./build.sh
# -> target/deploy/orvix_staking.so  (SBPF ELF, devnet-verifier-compatible)
```

The on-chain program ID is `CS4CWHL4DeSvbqZaUzT9AgK47VWweg94Ta2FZokvJZSg`
(keypair in `target/deploy/orvix_staking-keypair.json` — **do not commit
keypairs**).

## Deploy (devnet)

```bash
./build.sh
anchor deploy --program-name orvix_staking --provider.cluster devnet
```

Status: **deployed** to devnet at slot 486061628 (owner
`BPFLoaderUpgradeab1e11111111111111111111111`, upgrade authority is the
deploying wallet). Subsequent deploys are upgrades to the same program ID.

## Initialize the vault

Before any stake can land, the program-owned vault token account must exist
at its PDA (`seeds ["vault"]`, authority `seeds ["vault_authority"]`). Run
`initialize_vault` once per mint (idempotent; caller pays the ATA rent):

```bash
# via the orchestrator service:
#   POST /v1/staking/user/initialize-vault   (admin/operator)
# or build+submit the instruction with the deployed program's `initialize_vault`
```

Until the vault exists, `stake` fails with an account-not-found constraint
error rather than moving funds — safe by construction.

## Lock periods

`stake` accepts `lock_days` in {3, 7, 14}. The deadline stored on the
`StakeAccount` is `max(current_deadline, now + lock_days)`, so topping up never
shortens an existing commitment. `unstake` refuses while
`now < stake_locked_until` (custom error `StakeLocked`).

## Tests

- Rust: `RUSTUP_TOOLCHAIN=1.89.0-sbpf-solana-v1.52 cargo test` (unit tests TBD).
- Orchestrator integration: `orchestrator/tests/test_user_staking.py` mocks the
  RPC and asserts transaction shapes and stake-status parsing.
