-- ============================================================================
-- Orvix Orchestrator — atomic top-up crediting
-- Paste this whole file into the Supabase SQL Editor and run it.
-- Safe to re-run: CREATE OR REPLACE.
-- ============================================================================

-- credit_topup — record the ledger row AND credit the balance in ONE
-- transaction. The unique constraint on transactions.solana_signature is the
-- single source of idempotency: a duplicate signature raises unique_violation,
-- which we catch and turn into a NULL return, crediting nothing.
--
-- Returns the new balance, or NULL when the signature was already processed.
--
-- This replaces the old "credit first, insert second" flow in the payment
-- listener, which could double-credit if the process crashed (or the insert
-- failed) after the balance had already been bumped.
create or replace function credit_topup(
    p_user_id   uuid,
    p_amount    numeric,
    p_signature text,
    p_memo      text,
    p_intent_id uuid
)
returns numeric as $$
declare
    new_balance numeric;
begin
    insert into transactions (
        user_id, type, amount, token, solana_signature, status, metadata
    )
    values (
        p_user_id, 'topup', p_amount, 'USDC', p_signature, 'confirmed',
        jsonb_build_object('memo', p_memo, 'intent_id', p_intent_id::text)
    );

    update users
        set balance_usdc = balance_usdc + p_amount
        where id = p_user_id
        returning balance_usdc into new_balance;

    return new_balance;
exception
    when unique_violation then
        -- Signature already credited by a prior run: do nothing, credit nothing.
        return null;
end;
$$ language plpgsql;
