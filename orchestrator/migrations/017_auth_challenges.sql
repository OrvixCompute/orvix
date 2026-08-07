-- ============================================================================
-- Orvix Orchestrator — migration 017: persistent wallet-auth challenges
-- Run AFTER 001-016. Idempotent (create if not exists).
--
-- Challenge nonces used to live in an in-memory dict on AuthService, which had
-- two consequences in production:
--   1. Every orchestrator restart wiped all pending challenges, so anyone
--      mid-login got "Unknown or already-used challenge nonce" (401). Observed:
--      a challenge issued at 20:14:38, a restart at 20:14:42, the verify at
--      20:14:45 rejected 3 seconds later.
--   2. The dict was keyed by wallet and held ONE entry, so a second /challenge
--      call silently invalidated the first. A user who signed the earlier
--      message got the same 401 with no way to tell why.
--
-- Keying on the nonce fixes both: challenges survive restarts, and a wallet may
-- have several outstanding at once (each single-use, all short-lived).
-- ============================================================================

begin;

create table if not exists auth_challenges (
    nonce       text primary key,           -- the value embedded in the signed message
    wallet      text not null,              -- who it was issued to
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);

comment on table auth_challenges is
    'Short-lived single-use wallet-auth nonces. Rows are deleted on successful verify and swept when expired.';

create index if not exists idx_auth_challenges_wallet     on auth_challenges (wallet);
create index if not exists idx_auth_challenges_expires_at on auth_challenges (expires_at);

-- RLS: service_role only, matching the rest of the schema.
alter table auth_challenges enable row level security;
drop policy if exists service_role_all on auth_challenges;
create policy service_role_all on auth_challenges for all to service_role using (true) with check (true);

commit;
