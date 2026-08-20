//! Orvix non-custodial user staking program (Anchor v2).
//!
//! Users lock ORVX (SPL token) in a program-owned vault for a lock period
//! chosen at stake time (3 / 7 / 14 days). The program is the vault's
//! authority; no operator key can move staked tokens. Unstaking is only
//! allowed after `stake_locked_until` has passed.
//!
//! Each user has one `StakeAccount` PDA (seeds: ["stake", owner]). The vault
//! is a single PDA (seeds: ["vault"]) that holds everyone's ORVX.

use anchor_lang::prelude::*;
use anchor_spl::{
    mint::Mint,
    token::{self, Token, TokenAccount},
};

declare_id!("CS4CWHL4DeSvbqZaUzT9AgK47VWweg94Ta2FZokvJZSg");

// Allowed lock periods, in days. Chosen by the user at stake time and
// enforced against the clock at unstake time.
pub const LOCK_PERIODS_DAYS: [i64; 3] = [3, 7, 14];
pub const VAULT_SEED: &[u8] = b"vault";
pub const STAKE_SEED: &[u8] = b"stake";
pub const SECONDS_PER_DAY: i64 = 86_400;

#[program]
pub mod orvix_staking {
    use super::*;

    /// Lock `amount` ORVX for `lock_days` (3 / 7 / 14). Adds to any existing
    /// stake; the lock deadline extends to the later of the current deadline
    /// and the new one, so topping up never shortens an existing commitment.
    pub fn stake(ctx: &mut Context<Stake>, amount: u64, lock_days: i64) -> Result<()> {
        require!(
            LOCK_PERIODS_DAYS.contains(&lock_days),
            ErrorCode::InvalidLockPeriod
        );
        require!(amount > 0, ErrorCode::ZeroAmount);

        token::transfer_checked(
            CpiContext::new(
                ctx.accounts.token_program.address(),
                token::accounts::TransferChecked {
                    from: ctx.accounts.user_ata.cpi_handle_mut(),
                    mint: ctx.accounts.mint.cpi_handle(),
                    to: ctx.accounts.vault.cpi_handle_mut(),
                    authority: ctx.accounts.owner.cpi_handle(),
                },
            ),
            amount,
            ctx.accounts.mint.decimals(),
        )?;

        let stake = &mut ctx.accounts.stake_account;
        stake.amount = stake
            .amount
            .checked_add(amount)
            .ok_or(ErrorCode::Overflow)?;

        let new_deadline = Clock::get()?
            .unix_timestamp
            .checked_add(lock_days.checked_mul(SECONDS_PER_DAY).ok_or(ErrorCode::Overflow)?)
            .ok_or(ErrorCode::Overflow)?;
        if new_deadline > stake.stake_locked_until.get() {
            stake.stake_locked_until = new_deadline.into();
        }

        Ok(())
    }

    /// Release `amount` ORVX back to the owner once the lock has expired.
    /// Partial unstakes are allowed; the remaining stake keeps its deadline.
    pub fn unstake(ctx: &mut Context<Unstake>, amount: u64) -> Result<()> {
        require!(amount > 0, ErrorCode::ZeroAmount);

        let stake = &mut ctx.accounts.stake_account;
        require!(stake.amount >= amount, ErrorCode::InsufficientStake);

        let now = Clock::get()?.unix_timestamp;
        require!(now >= stake.stake_locked_until.get(), ErrorCode::StakeLocked);

        stake.amount = stake.amount.checked_sub(amount).ok_or(ErrorCode::Overflow)?;

        let signer_seeds: &[&[&[u8]]] = &[&[VAULT_SEED, &[ctx.bumps.vault_authority]]];

        token::transfer_checked(
            CpiContext::new(
                ctx.accounts.token_program.address(),
                token::accounts::TransferChecked {
                    from: ctx.accounts.vault.cpi_handle_mut(),
                    mint: ctx.accounts.mint.cpi_handle(),
                    to: ctx.accounts.user_ata.cpi_handle_mut(),
                    authority: ctx.accounts.vault_authority.cpi_handle(),
                },
            )
            .with_signer(signer_seeds),
            amount,
            ctx.accounts.mint.decimals(),
        )?;

        Ok(())
    }
}

/// Per-user stake state. Zero-copy fixed-size account (Pod layout).
#[account]
#[repr(C)]
pub struct StakeAccount {
    pub owner: Address,
    /// Raw token units staked (base units, not human decimals).
    pub amount: PodU64,
    /// Unix timestamp (seconds) before which unstaking is refused.
    pub stake_locked_until: PodI64,
    pub created_at: PodI64,
}

#[derive(Accounts)]
pub struct Stake {
    #[account(mut)]
    pub owner: Signer,

    #[account(
        init_if_needed,
        payer = owner,
        seeds = [STAKE_SEED, owner.address().as_ref()],
        bump,
        space = 8 + 32 + 8 + 8 + 8
    )]
    pub stake_account: Account<StakeAccount>,

    /// User's ORVX ATA (source of funds).
    #[account(
        mut,
        token::mint = mint,
        token::authority = owner
    )]
    pub user_ata: Account<TokenAccount>,

    /// Program-owned vault holding all staked ORVX.
    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump,
        token::mint = mint,
        token::authority = vault_authority
    )]
    pub vault: Account<TokenAccount>,

    /// PDA that is the vault's token authority (signs CPI transfers).
    #[account(seeds = [VAULT_SEED], bump)]
    pub vault_authority: UncheckedAccount,

    pub mint: Account<Mint>,
    pub token_program: Program<Token>,
    pub system_program: Program<System>,
}

#[derive(Accounts)]
pub struct Unstake {
    #[account(mut)]
    pub owner: Signer,

    #[account(
        mut,
        seeds = [STAKE_SEED, owner.address().as_ref()],
        bump,
        address = stake_account.owner
    )]
    pub stake_account: Account<StakeAccount>,

    /// User's ORVX ATA (receives unstaked funds).
    #[account(
        mut,
        token::mint = mint,
        token::authority = owner
    )]
    pub user_ata: Account<TokenAccount>,

    #[account(
        mut,
        seeds = [VAULT_SEED],
        bump,
        token::mint = mint,
        token::authority = vault_authority
    )]
    pub vault: Account<TokenAccount>,

    /// PDA that is the vault's token authority (signs CPI transfers).
    #[account(seeds = [VAULT_SEED], bump)]
    pub vault_authority: UncheckedAccount,

    pub mint: Account<Mint>,
    pub token_program: Program<Token>,
    pub system_program: Program<System>,
}

#[error_code]
pub enum ErrorCode {
    #[msg("Lock period must be one of 3, 7, or 14 days")]
    InvalidLockPeriod,
    #[msg("Amount must be greater than zero")]
    ZeroAmount,
    #[msg("Stake is still locked")]
    StakeLocked,
    #[msg("Insufficient staked balance")]
    InsufficientStake,
    #[msg("Arithmetic overflow")]
    Overflow,
}
