-- ============================================================================
-- Orvix Orchestrator — migration 003: provider earnings & withdrawals (Prompt 6)
-- Run AFTER 002_nodes.sql. Idempotent.
-- ============================================================================

-- Provider-related columns on users.
alter table users add column if not exists is_provider boolean default false;
alter table users add column if not exists provider_secret_hash text;  -- sha256 hex
alter table users add column if not exists available_usdc numeric(20,6) default 0
    check (available_usdc >= 0);
alter table users add column if not exists pending_withdrawal_usdc numeric(20,6) default 0
    check (pending_withdrawal_usdc >= 0);
alter table users add column if not exists lifetime_earnings_usdc numeric(20,6) default 0;

-- Withdrawal requests / queue.
create table if not exists withdrawals (
    id                  uuid primary key default gen_random_uuid(),
    user_id             uuid references users(id) on delete cascade,
    amount              numeric(20,6) not null,
    destination_wallet  text not null,
    status              text default 'queued'
                            check (status in ('queued','processing','completed','failed','cancelled')),
    solana_signature    text unique,
    error_message       text,
    queued_at           timestamptz default now(),
    processed_at        timestamptz,
    metadata            jsonb default '{}'
);

create index if not exists idx_withdrawals_user_status on withdrawals (user_id, status);
create index if not exists idx_withdrawals_status_queued_at
    on withdrawals (status, queued_at) where status in ('queued','processing');

-- Atomically credit a provider's earnings.
create or replace function credit_provider_earnings(p_user_id uuid, p_amount numeric)
returns void as $$
begin
    update users set
        available_usdc = available_usdc + p_amount,
        lifetime_earnings_usdc = lifetime_earnings_usdc + p_amount,
        updated_at = now()
    where id = p_user_id;
end;
$$ language plpgsql;

-- Atomically move funds from available -> pending for a withdrawal.
-- Returns true on success, false if the balance is insufficient.
create or replace function lock_withdrawal(p_user_id uuid, p_amount numeric)
returns boolean as $$
declare
    current_available numeric;
begin
    select available_usdc into current_available from users where id = p_user_id for update;
    if current_available is null or current_available < p_amount then
        return false;
    end if;
    update users set
        available_usdc = available_usdc - p_amount,
        pending_withdrawal_usdc = pending_withdrawal_usdc + p_amount,
        updated_at = now()
    where id = p_user_id;
    return true;
end;
$$ language plpgsql;

-- Settle a withdrawal: clear it from pending. If p_refund is true, the amount
-- returns to available (used on payout failure).
create or replace function settle_withdrawal(p_user_id uuid, p_amount numeric, p_refund boolean)
returns void as $$
begin
    update users set
        pending_withdrawal_usdc = greatest(pending_withdrawal_usdc - p_amount, 0),
        available_usdc = available_usdc + case when p_refund then p_amount else 0 end,
        updated_at = now()
    where id = p_user_id;
end;
$$ language plpgsql;

-- RLS for withdrawals.
alter table withdrawals enable row level security;
drop policy if exists service_role_all on withdrawals;
create policy service_role_all on withdrawals for all to service_role using (true) with check (true);
