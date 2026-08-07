-- ============================================================================
-- Orvix Orchestrator — migration 018: persistent payment-listener cursors
-- Run AFTER 001-017. Idempotent (create if not exists).
--
-- The listener tracked how far it had read in memory, one entry per watched
-- address. Two consequences:
--
--   1. Every restart forgot the position, so the next poll re-read the most
--      recent 25 signatures on each address. Harmless (crediting is idempotent
--      on solana_signature) but wasteful, and it silently limited how far back
--      a cold start could see: anything older than 25 transactions was never
--      examined at all.
--   2. Nothing bounded the gap. A quiet address is fine, but if more than a
--      page of transactions landed between two polls, the cursor jumped to the
--      newest and the ones in between were never processed.
--
-- Storing the cursor lets the listener page backwards until it reaches the last
-- signature it actually handled, so the window is bounded by real progress
-- rather than by a page size.
-- ============================================================================

begin;

create table if not exists listener_cursors (
    address         text primary key,        -- watched account (wallet or token account)
    last_signature  text not null,           -- newest signature already processed
    updated_at      timestamptz not null default now()
);

comment on table listener_cursors is
    'How far the payment listener has read on each watched address. Survives restarts.';

-- RLS: service_role only, matching the rest of the schema.
alter table listener_cursors enable row level security;
drop policy if exists service_role_all on listener_cursors;
create policy service_role_all on listener_cursors for all to service_role using (true) with check (true);

commit;
