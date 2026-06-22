-- ============================================================================
-- Orvix Orchestrator — migration 005: switch the money layer from ORVX to USDC
-- Run AFTER 001-004 on a database that was originally created with ORVX columns.
-- Idempotent: each step is guarded so re-running (or running on a fresh USDC
-- schema) is a no-op. Numeric precision drops from 9 to 6 decimals (USDC).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- users.balance: fold balance_orvx into balance_usdc, then drop balance_orvx.
-- (Fresh schemas already have only balance_usdc.)
-- ----------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from information_schema.columns
               where table_name = 'users' and column_name = 'balance_orvx') then
        if not exists (select 1 from information_schema.columns
                       where table_name = 'users' and column_name = 'balance_usdc') then
            alter table users add column balance_usdc numeric(20,6) not null default 0;
        end if;
        update users set balance_usdc = balance_orvx where balance_orvx <> 0;
        alter table users drop column balance_orvx;
    end if;
end $$;

-- ----------------------------------------------------------------------------
-- Rename the provider/earnings columns *_orvx -> *_usdc and shrink precision.
-- ----------------------------------------------------------------------------
do $$
declare
    pair text[];
    pairs text[][] := array[
        array['users','available_orvx','available_usdc'],
        array['users','pending_withdrawal_orvx','pending_withdrawal_usdc'],
        array['users','lifetime_earnings_orvx','lifetime_earnings_usdc'],
        array['nodes','total_earned_orvx','total_earned_usdc'],
        array['jobs','cost_orvx','cost_usdc'],
        array['jobs','provider_earning_orvx','provider_earning_usdc'],
        array['topup_intents','expected_amount_orvx','expected_amount_usdc']
    ];
begin
    foreach pair slice 1 in array pairs loop
        if exists (select 1 from information_schema.columns
                   where table_name = pair[1] and column_name = pair[2]) then
            execute format('alter table %I rename column %I to %I', pair[1], pair[2], pair[3]);
        end if;
    end loop;
end $$;

-- Shrink numeric precision to USDC's 6 decimals where the columns exist.
alter table users          alter column balance_usdc             type numeric(20,6);
alter table users          alter column available_usdc           type numeric(20,6);
alter table users          alter column pending_withdrawal_usdc  type numeric(20,6);
alter table users          alter column lifetime_earnings_usdc   type numeric(20,6);
alter table nodes          alter column total_earned_usdc        type numeric(20,6);
alter table jobs           alter column cost_usdc                type numeric(20,6);
alter table jobs           alter column provider_earning_usdc    type numeric(20,6);
alter table topup_intents  alter column expected_amount_usdc     type numeric(20,6);
alter table transactions   alter column amount                   type numeric(20,6);
alter table withdrawals    alter column amount                   type numeric(20,6);

-- ----------------------------------------------------------------------------
-- transactions.token: default to USDC and constrain to USDC only.
-- ----------------------------------------------------------------------------
update transactions set token = 'USDC' where token <> 'USDC';
alter table transactions alter column token set default 'USDC';
do $$
begin
    if exists (select 1 from pg_constraint where conname = 'transactions_token_check') then
        alter table transactions drop constraint transactions_token_check;
    end if;
    alter table transactions add constraint transactions_token_check check (token in ('USDC'));
end $$;

-- ----------------------------------------------------------------------------
-- Redefine the balance functions to operate on the USDC columns.
-- (Re-run of the CREATE OR REPLACE bodies from 001/003/004.)
-- ----------------------------------------------------------------------------
create or replace function deduct_balance(p_user_id uuid, p_amount numeric)
returns numeric as $$
declare new_balance numeric;
begin
    update users set balance_usdc = balance_usdc - p_amount
        where id = p_user_id and balance_usdc >= p_amount
        returning balance_usdc into new_balance;
    return new_balance;
end;
$$ language plpgsql;

create or replace function credit_balance(p_user_id uuid, p_amount numeric)
returns numeric as $$
declare new_balance numeric;
begin
    update users set balance_usdc = balance_usdc + p_amount
        where id = p_user_id
        returning balance_usdc into new_balance;
    return new_balance;
end;
$$ language plpgsql;

create or replace function credit_topup(
    p_user_id uuid, p_amount numeric, p_signature text, p_memo text, p_intent_id uuid
)
returns numeric as $$
declare new_balance numeric;
begin
    insert into transactions (user_id, type, amount, token, solana_signature, status, metadata)
    values (p_user_id, 'topup', p_amount, 'USDC', p_signature, 'confirmed',
            jsonb_build_object('memo', p_memo, 'intent_id', p_intent_id::text));
    update users set balance_usdc = balance_usdc + p_amount
        where id = p_user_id
        returning balance_usdc into new_balance;
    return new_balance;
exception
    when unique_violation then
        return null;
end;
$$ language plpgsql;

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

create or replace function lock_withdrawal(p_user_id uuid, p_amount numeric)
returns boolean as $$
declare current_available numeric;
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
