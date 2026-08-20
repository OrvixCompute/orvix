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
- Vault — token account PDA at seeds `["vault"]`; its authority is the
  `["vault"]`-seeded PDA (same seed, separate signer slot).

## Build

The bundled `anchor` CLI ships a Rust toolchain too old for the current
dependency tree (solana-program 2.x needs Rust 1.85+). Use `build.sh`, which
builds with the Solana platform-tools toolchain (rustc 1.89) against the
`sbpfv1` target and stages the artifact where `anchor deploy` expects it:

```bash
./build.sh
# -> target/deploy/orvix_staking.so  (SBPF ELF, loader-compatible)
```

The on-chain program ID is `CS4CWHL4DeSvbqZaUzT9AgK47VWweg94Ta2FZokvJZSg`
(keypair in `target/deploy/orvix_staking-keypair.json` — **do not commit
keypairs**).

## Deploy (devnet)

```bash
./build.sh
anchor deploy --program-name orvix_staking --provider.cluster devnet
```

`anchor deploy` runs `solana program deploy` against the staged `.so`. On a
machine without devnet DNS/network access this fails with a connection error
after passing the ELF verifier — the artifact itself is valid.

## Tests

- Rust: `RUSTUP_TOOLCHAIN=1.89.0-sbpf-solana-v1.52 cargo test` (unit tests TBD).
- Orchestrator integration: `orchestrator/tests/test_user_staking.py` mocks the
  RPC and asserts transaction shapes and stake-status parsing.
