//! PayLane - a reserve-gated Solana stablecoin treasury (Anchor program).
//!
//! The treasury is a PDA bound to one SPL mint. It stores the reserves attested
//! on-chain and a pause flag. It deliberately does NOT store circulating supply:
//! circulating is read from the bound `Mint.supply` on every instruction, so the
//! ledger can never drift from the tokens that actually exist.
//!
//! * `initialize` - the current mint authority hands the mint to the treasury PDA
//!   in the same transaction, so the treasury cannot be front-run or seized.
//! * `attest_reserve` - the attestor writes the real reserve figure. A figure
//!   below circulating supply is RECORDED, not rejected, and pauses minting.
//! * `mint` - runs the reserve gate against `Mint.supply`, then performs a real
//!   `spl_token::mint_to` CPI signed by the treasury PDA, then re-reads the mint
//!   and asserts the supply moved by exactly the minted amount.
//! * `settle` - burns the holder's tokens (holder signs) so redemption removes
//!   supply atomically. There is no way to reduce circulating without burning.
//!
//! The gate itself lives in `reserve.rs`: pure Rust, no Anchor, unit-tested here
//! and in the sibling `reserve-core` crate. `tests/program.rs` drives the real
//! instructions against the real SPL Token program under `solana-program-test`.

use anchor_lang::prelude::*;
use anchor_lang::solana_program::program_option::COption;
use anchor_spl::token::{
    self, Burn, Mint as SplMint, MintTo, SetAuthority, Token, TokenAccount as SplTokenAccount,
};
use anchor_spl::token::spl_token::instruction::AuthorityType;

pub mod reserve;

use reserve::{AttestOutcome, ReserveError, ReserveState};

declare_id!("PayL111111111111111111111111111111111111111");

/// PDA seed prefix for the per-mint treasury.
pub const TREASURY_SEED: &[u8] = b"treasury";

#[program]
pub mod paylane {
    use super::*;

    /// Create the treasury for `mint` and atomically move the mint authority to
    /// the treasury PDA.
    ///
    /// Only the CURRENT mint authority can call this, which is what makes the
    /// treasury unseizable: the PDA is derived from the mint, and nobody who
    /// cannot sign as that mint's authority can create it. After this
    /// instruction the only code that can ever mint is this program.
    pub fn initialize(ctx: Context<Initialize>, attestor: Pubkey) -> Result<()> {
        // Separated powers: the key that attests reserves must not be the key
        // that mints against them.
        require_keys_neq!(
            attestor,
            ctx.accounts.mint_authority.key(),
            PaylaneError::AttestorMustDiffer
        );

        let mint_key = ctx.accounts.mint.key();
        {
            let t = &mut ctx.accounts.treasury;
            t.authority = ctx.accounts.mint_authority.key();
            t.attestor = attestor;
            t.mint = mint_key;
            t.attested_reserve = 0;
            t.paused = false;
            t.bump = ctx.bumps.treasury;
        }

        // Hand mint authority to the PDA in the same transaction.
        token::set_authority(
            CpiContext::new(
                ctx.accounts.token_program.to_account_info(),
                SetAuthority {
                    current_authority: ctx.accounts.mint_authority.to_account_info(),
                    account_or_mint: ctx.accounts.mint.to_account_info(),
                },
            ),
            AuthorityType::MintTokens,
            Some(ctx.accounts.treasury.key()),
        )?;

        ctx.accounts.mint.reload()?;
        require!(
            ctx.accounts.mint.mint_authority == COption::Some(ctx.accounts.treasury.key()),
            PaylaneError::MintAuthorityNotTransferred
        );

        emit!(TreasuryInitialized {
            treasury: ctx.accounts.treasury.key(),
            mint: mint_key,
            authority: ctx.accounts.treasury.authority,
            attestor,
        });
        Ok(())
    }

    /// Attestor-only: write the real attested reserve on-chain.
    ///
    /// A figure below circulating supply is the truth about a shortfall, so it is
    /// recorded and the treasury is paused. Rejecting it (the old behaviour)
    /// would have left a stale higher figure on the books and let the issuer keep
    /// minting against reserves that no longer exist.
    pub fn attest_reserve(ctx: Context<Attest>, new_reserve: u64) -> Result<()> {
        let circulating = ctx.accounts.mint.supply;
        let t = &mut ctx.accounts.treasury;
        let mut state = ReserveState::with_paused(t.attested_reserve, circulating, t.paused);
        let outcome = state.apply_attest(new_reserve);
        t.attested_reserve = state.attested_reserve;
        t.paused = state.paused;

        let shortfall = match outcome {
            AttestOutcome::Solvent => 0,
            AttestOutcome::Undercollateralized { shortfall } => shortfall,
        };
        emit!(ReserveAttested {
            new_reserve,
            circulating,
            paused: t.paused,
            shortfall,
        });
        Ok(())
    }

    /// Authority-only: mint stablecoin to `recipient_token_account`.
    ///
    /// The reserve gate runs against the live `Mint.supply`. On success a real
    /// SPL `mint_to` CPI is signed by the treasury PDA, and the mint is re-read
    /// to prove the supply moved by exactly `amount`.
    pub fn mint(ctx: Context<MintTokens>, amount: u64) -> Result<()> {
        let circulating = ctx.accounts.mint.supply;
        let state = ReserveState::with_paused(
            ctx.accounts.treasury.attested_reserve,
            circulating,
            ctx.accounts.treasury.paused,
        );
        // THE GATE. Same code path as the off-chain agent.
        let expected = state.check_mint(amount).map_err(PaylaneError::from)?;

        let mint_key = ctx.accounts.mint.key();
        let bump = ctx.accounts.treasury.bump;
        let seeds: &[&[u8]] = &[TREASURY_SEED, mint_key.as_ref(), &[bump]];
        token::mint_to(
            CpiContext::new_with_signer(
                ctx.accounts.token_program.to_account_info(),
                MintTo {
                    mint: ctx.accounts.mint.to_account_info(),
                    to: ctx.accounts.recipient_token_account.to_account_info(),
                    authority: ctx.accounts.treasury.to_account_info(),
                },
                &[seeds],
            ),
            amount,
        )?;

        // Prove the CPI actually happened. A no-op mint helper cannot pass this.
        ctx.accounts.mint.reload()?;
        require_eq!(
            ctx.accounts.mint.supply,
            expected,
            PaylaneError::SupplyMismatch
        );

        emit!(Minted {
            amount,
            circulating: ctx.accounts.mint.supply,
            attested_reserve: ctx.accounts.treasury.attested_reserve,
        });
        Ok(())
    }

    /// Redeem stablecoin out of circulation by burning it.
    ///
    /// The holder signs and their tokens are burned atomically, so circulating
    /// supply only falls when tokens really disappear. There is no instruction
    /// that lowers the ledger without burning, which is what previously created
    /// unbacked remint headroom.
    pub fn settle(ctx: Context<Settle>, amount: u64) -> Result<()> {
        let circulating = ctx.accounts.mint.supply;
        let mut state = ReserveState::with_paused(
            ctx.accounts.treasury.attested_reserve,
            circulating,
            ctx.accounts.treasury.paused,
        );
        state.apply_settle(amount).map_err(PaylaneError::from)?;
        let expected = state.circulating;

        token::burn(
            CpiContext::new(
                ctx.accounts.token_program.to_account_info(),
                Burn {
                    mint: ctx.accounts.mint.to_account_info(),
                    from: ctx.accounts.holder_token_account.to_account_info(),
                    authority: ctx.accounts.holder.to_account_info(),
                },
            ),
            amount,
        )?;

        ctx.accounts.mint.reload()?;
        require_eq!(
            ctx.accounts.mint.supply,
            expected,
            PaylaneError::SupplyMismatch
        );

        // Redemption can restore solvency, which unpauses the treasury.
        let t = &mut ctx.accounts.treasury;
        t.paused = state.paused;

        emit!(Settled {
            amount,
            circulating: expected,
            paused: t.paused,
        });
        Ok(())
    }
}

// --- accounts --------------------------------------------------------------

#[account]
pub struct Treasury {
    /// The issuer key allowed to mint (never allowed to attest).
    pub authority: Pubkey,
    /// The reserve oracle key allowed to attest (never allowed to mint).
    pub attestor: Pubkey,
    /// The SPL mint this treasury governs. Circulating supply is `mint.supply`.
    pub mint: Pubkey,
    /// Reserves attested on-chain, in the mint's smallest unit.
    pub attested_reserve: u64,
    /// True when the last attestation revealed a shortfall. Blocks minting.
    pub paused: bool,
    pub bump: u8,
}

impl Treasury {
    /// 8 discriminator + 3 * 32 pubkeys + 8 reserve + 1 paused + 1 bump.
    pub const SPACE: usize = 8 + 32 * 3 + 8 + 1 + 1;
}

#[derive(Accounts)]
pub struct Initialize<'info> {
    #[account(
        init,
        payer = payer,
        space = Treasury::SPACE,
        seeds = [TREASURY_SEED, mint.key().as_ref()],
        bump
    )]
    pub treasury: Account<'info, Treasury>,
    /// The mint this treasury will govern. Must still be controlled by
    /// `mint_authority`, who signs, so nobody else can claim this PDA.
    #[account(
        mut,
        constraint = mint.mint_authority == COption::Some(mint_authority.key())
            @ PaylaneError::NotMintAuthority
    )]
    pub mint: Account<'info, SplMint>,
    pub mint_authority: Signer<'info>,
    #[account(mut)]
    pub payer: Signer<'info>,
    pub token_program: Program<'info, Token>,
    pub system_program: Program<'info, System>,
}

#[derive(Accounts)]
pub struct Attest<'info> {
    #[account(
        mut,
        seeds = [TREASURY_SEED, mint.key().as_ref()],
        bump = treasury.bump,
        has_one = attestor @ PaylaneError::Unauthorized,
        has_one = mint @ PaylaneError::WrongMint,
    )]
    pub treasury: Account<'info, Treasury>,
    pub attestor: Signer<'info>,
    /// Read-only: supplies the live circulating figure the attestation is
    /// compared against.
    pub mint: Account<'info, SplMint>,
}

#[derive(Accounts)]
pub struct MintTokens<'info> {
    #[account(
        seeds = [TREASURY_SEED, mint.key().as_ref()],
        bump = treasury.bump,
        has_one = authority @ PaylaneError::Unauthorized,
        has_one = mint @ PaylaneError::WrongMint,
    )]
    pub treasury: Account<'info, Treasury>,
    pub authority: Signer<'info>,
    #[account(
        mut,
        constraint = mint.mint_authority == COption::Some(treasury.key())
            @ PaylaneError::MintNotGoverned
    )]
    pub mint: Account<'info, SplMint>,
    #[account(mut, constraint = recipient_token_account.mint == treasury.mint
        @ PaylaneError::WrongMint)]
    pub recipient_token_account: Account<'info, SplTokenAccount>,
    pub token_program: Program<'info, Token>,
}

#[derive(Accounts)]
pub struct Settle<'info> {
    #[account(
        mut,
        seeds = [TREASURY_SEED, mint.key().as_ref()],
        bump = treasury.bump,
        has_one = mint @ PaylaneError::WrongMint,
    )]
    pub treasury: Account<'info, Treasury>,
    #[account(mut)]
    pub mint: Account<'info, SplMint>,
    #[account(
        mut,
        constraint = holder_token_account.mint == treasury.mint @ PaylaneError::WrongMint,
        constraint = holder_token_account.owner == holder.key() @ PaylaneError::Unauthorized,
    )]
    pub holder_token_account: Account<'info, SplTokenAccount>,
    /// The token owner. Redemption burns their tokens, so they must sign.
    pub holder: Signer<'info>,
    pub token_program: Program<'info, Token>,
}

// --- events (the on-chain history the UI mirrors) --------------------------

#[event]
pub struct TreasuryInitialized {
    pub treasury: Pubkey,
    pub mint: Pubkey,
    pub authority: Pubkey,
    pub attestor: Pubkey,
}

#[event]
pub struct Minted {
    pub amount: u64,
    pub circulating: u64,
    pub attested_reserve: u64,
}

#[event]
pub struct Settled {
    pub amount: u64,
    pub circulating: u64,
    pub paused: bool,
}

#[event]
pub struct ReserveAttested {
    pub new_reserve: u64,
    pub circulating: u64,
    pub paused: bool,
    pub shortfall: u64,
}

// --- errors ----------------------------------------------------------------

#[error_code]
pub enum PaylaneError {
    #[msg("mint would exceed on-chain attested reserves")]
    ExceedsReserves,
    #[msg("arithmetic overflow")]
    MathOverflow,
    #[msg("amount must be greater than zero")]
    ZeroAmount,
    #[msg("treasury is undercollateralized; minting is paused")]
    Undercollateralized,
    #[msg("signer is not authorized for this action")]
    Unauthorized,
    #[msg("account does not belong to the treasury's mint")]
    WrongMint,
    #[msg("signer is not the current mint authority")]
    NotMintAuthority,
    #[msg("mint authority was not transferred to the treasury PDA")]
    MintAuthorityNotTransferred,
    #[msg("mint authority is not the treasury PDA")]
    MintNotGoverned,
    #[msg("token supply after the CPI does not match the gated amount")]
    SupplyMismatch,
    #[msg("the attestor key must differ from the mint authority")]
    AttestorMustDiffer,
}

impl From<ReserveError> for PaylaneError {
    fn from(e: ReserveError) -> Self {
        match e {
            ReserveError::ExceedsReserves => PaylaneError::ExceedsReserves,
            ReserveError::MathOverflow => PaylaneError::MathOverflow,
            ReserveError::ZeroAmount => PaylaneError::ZeroAmount,
            ReserveError::Undercollateralized => PaylaneError::Undercollateralized,
        }
    }
}
