-- ============================================================================
-- Orvix Orchestrator — migration 013: treasury wallet roles
-- Run AFTER 001-012. Idempotent. Apply via Supabase SQL Editor (prod has no
-- schema_migrations tracking yet).
--
-- Three roles for the cold/hot/payout treasury split (buyback wallet is Phase 2,
-- intentionally omitted). Public keys are filled in AFTER running
-- scripts/generate_treasury_wallets.py — this migration only seeds the rows.
-- ============================================================================

begin;

create table if not exists treasury_wallets (
    wallet_role             text primary key
                                check (wallet_role in ('main', 'hot', 'payout')),
    public_key              text,                       -- filled in after keygen
    purpose                 text,
    balance_usdc            numeric(20,6),
    balance_orvx            numeric(20,6),
    balance_last_synced_at  timestamptz
);

-- Seed the three roles (public keys set later via admin/SQL once generated).
insert into treasury_wallets (wallet_role, purpose) values
    ('main',   'Cold storage — holds the bulk of funds; private key kept OFFLINE'),
    ('hot',    'Hot receiver — incoming USDC deposits; the payment listener watches this address'),
    ('payout', 'Provider payout signer — sends USDC to providers on withdrawal settlement')
on conflict (wallet_role) do nothing;

-- RLS: service_role only, matching the rest of the schema.
alter table treasury_wallets enable row level security;
drop policy if exists service_role_all on treasury_wallets;
create policy service_role_all on treasury_wallets for all to service_role using (true) with check (true);

commit;
