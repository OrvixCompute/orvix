# Orvix Governance

Orvix governance lets the ORVX community steer network parameters and direction.
**v1 is off-chain signal voting via [Snapshot](https://snapshot.box/#/orvix)** —
gasless, signature-based, and counted by ORVX holdings. v2 will move to on-chain
governance with smart contracts.

## Who can participate

- **Vote:** any ORVX holder (vote weight = ORVX held).
- **Propose:** holders with **≥ 100,000 ORVX**.

## How voting works

Voting happens on Snapshot. Connect your Solana wallet, open a proposal, and sign
your vote — no gas, no on-chain transaction. Your weight reflects your ORVX
balance at the proposal's snapshot block.

## Proposal lifecycle

1. **Idea** — posted in Discord `#governance-discussion`.
2. **Refinement** — discussed for at least 24 hours, shaped into a formal proposal
   using a [template](./proposal-templates/).
3. **Submission** — posted to Snapshot.
4. **Voting delay** — 1 day (time to read before voting opens).
5. **Voting period** — 5 days.
6. **Result** — binding for off-chain decisions: the team commits to executing
   approved proposals within **30 days**.

## What can be governed

- Buyback percentage (currently 50% of the platform fee)
- Burn schedule (currently monthly)
- Minimum stake requirements (currently 25,000 ORVX for providers; tier thresholds)
- New model additions
- Treasury allocations above a threshold (e.g. 10% of treasury)
- Network parameters

## What cannot be governed (yet)

- Smart-contract changes (no custom contracts in v1)
- Anything requiring real-time response (e.g. security incidents)

## Decision binding

v1 is off-chain signal voting. The team commits to executing approved proposals
within 30 days and to publishing rationale if a proposal cannot be executed as
written. v2 will enforce decisions on-chain.

## Links

- Snapshot space: https://snapshot.box/#/orvix
- Setup guide (admins): [setup-snapshot.md](./setup-snapshot.md)
- Proposal templates: [proposal-templates/](./proposal-templates/)
