# Security Policy

Orvix takes the security of its software seriously. The system handles API keys, wallet-based
authentication, and a network of remote nodes, so we treat security reports with priority and
transparency.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues privately through one of:

- **Email:** security@orvix.xyz
- **GitHub Security Advisories:** https://github.com/OrvixCompute/orvix/security/advisories/new
- **Encrypted contact:** PGP key available on request

Please include:

- A description of the vulnerability
- Steps to reproduce
- Impact assessment (what could an attacker do?)
- Affected components (orchestrator / node / both)
- Your contact info for follow-up
- Whether you want public credit (default: yes, with your name/handle)

## Response Timeline

We commit to:

- **Acknowledge receipt:** within 48 hours
- **Initial assessment:** within 5 business days
- **Status updates:** every 7 days until resolution
- **Fix and disclosure:** depends on severity (see below)

## Severity Classification

| Severity | Examples | Target fix time |
|----------|----------|-----------------|
| Critical | Remote code execution, full system takeover, authentication bypass to other users' accounts | 24–72 hours |
| High | API key exposure, privilege escalation, node impersonation, JWT forgery | 1 week |
| Medium | Limited data leak, denial-of-service vectors, sensitive info in logs | 2 weeks |
| Low | Best-practice violations, hardening suggestions | 30 days |

## Disclosure Policy

- **Coordinated disclosure:** we'll work with you on a timeline.
- **Default embargo:** 90 days from initial report (longer if needed for complex fixes).
- **After fix:** we publish an advisory crediting the reporter.
- You're encouraged to publish a write-up after the embargo ends.

## Scope

**In scope:**

- Orchestrator (the FastAPI backend)
- Node software (the `orvix-node` Python package)
- Database schema and stored procedures
- The orchestrator ↔ node WebSocket protocol
- Wallet-based authentication and API key handling
- Staking flow (stake/unstake endpoints and the atomic stake RPCs)
- Buyback admin endpoint and CLI (especially `ADMIN_API_KEY` handling)
- Burn admin endpoint and CLI
- Treasury wallet and keypair management
- Frontend (when deployed)

**Out of scope:**

- Third-party services we depend on (report to them: Supabase, Helius, Solana RPC providers)
- Dependencies (report upstream — but we appreciate a heads-up)
- Denial-of-service via simple resource exhaustion that requires no authentication
- Social engineering of team members
- Physical security
- Reports from automated tools without analysis

## Recognition

**Status:** No formal bug bounty program yet — we're an early-stage project.

We are committed to:

- Public credit (Hall of Fame in this file + the published advisory)
- Coordinating disclosure timing and write-ups with you
- A formal bug bounty program once the project matures (planned post-mainnet)

## Past Advisories

No advisories yet. Once issued, they will be listed here in reverse chronological order:

- `YYYY-MM-DD` — [ID] — Brief description — Credited to: @handle

## Hall of Fame

We thank the following researchers for responsibly reporting security issues:

- *(empty for now)*

## Security Best Practices for Users

- Never share your API keys; rotate them if you suspect exposure.
- Use a dedicated wallet for Orvix sign-in, not your primary wallet.
- Always confirm you're on the official Orvix domain before signing any message.
- Never share your seed phrase with anyone — Orvix support will **never** ask for it.
- Enable wallet auto-lock.

## Known Limitations (Transparency)

Current alpha-state limitations we're actively working to address:

- The challenge-nonce store and request rate limiter are in-memory (single-process);
  moving them to a shared store (Redis) is planned so they hold across restarts and instances.
- Node-secret validation is minimal in the current build and will be hardened.
- The treasury and buyback wallets are hot wallets (not multisig yet); migration to
  separated wallets and then Squads multisig is planned.
- Admin buyback/burn endpoints are protected by a single shared API key
  (`ADMIN_API_KEY`) — rotate it regularly and prefer the CLI on the host.
- On-chain buyback/burn execution is gated behind stub flags (`BUYBACK_STUB`,
  `BURN_STUB`) until devnet-tested; real swaps/transfers are not yet implemented.
- Buyback execution is manual (subject to admin availability).
- No formal third-party security audit yet.
- No deployed production environment yet.

These are known and intentional for the alpha phase. We document them honestly —
transparency beats security through obscurity.
