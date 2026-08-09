// Reserve accounting for the PayLane treasury: the load-bearing invariant.
//
// This module is pure Rust with no Anchor dependency on purpose. It is the part
// of the program that must be provably correct, so it can be unit-tested on its
// own (the sibling `reserve-core` crate includes this exact file) while the
// on-chain Anchor program in `lib.rs` imports it verbatim. One source of truth.
//
// The rule the treasury enforces: circulating stablecoin supply can never exceed
// the reserves attested on-chain. On-chain, `circulating` is never a number the
// program stores and hopes is right - it is read from the SPL `Mint.supply` of
// the bound mint on every instruction, so it cannot drift from the real token
// supply. Every mint is checked against `attested_reserve` in integer fixed-point
// math with explicit overflow handling. There is no float and no bypass.
//
// Reserve losses are accepted, not hidden. An attestation that reports LESS
// backing than is circulating is the truth about a shortfall, so the treasury
// records it and pauses minting until solvency is restored. Refusing such an
// attestation (the earlier design) would have left a stale, too-high reserve
// figure on-chain and allowed further minting against reserves that no longer
// exist.

/// Ceiling for the reported backing ratio: 100_000% in basis points. Also the
/// value reported when nothing is circulating, where the ratio is undefined.
pub const MAX_BACKING_BPS: u128 = 10_000_000;

/// Errors the reserve gate can raise. Mirrors the Anchor `#[error_code]` enum in
/// `lib.rs` (same order, same meaning) so off-chain and on-chain agree.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReserveError {
    /// The mint would push circulating supply past attested reserves.
    ExceedsReserves,
    /// Arithmetic would overflow/underflow a u64 amount.
    MathOverflow,
    /// A zero-amount action was requested (no-op, rejected to keep logs clean).
    ZeroAmount,
    /// The last attestation revealed a reserve shortfall; minting is paused.
    Undercollateralized,
}

/// The result of applying an attestation. An attestation is always accepted; the
/// outcome says whether the treasury is still solvent afterwards.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttestOutcome {
    /// Reserves cover circulating supply. Minting is (or becomes) enabled.
    Solvent,
    /// Reserves fell below circulating supply by `shortfall` base units.
    /// The treasury is paused: no new minting until an attestation covers it.
    Undercollateralized { shortfall: u64 },
}

/// The treasury's reserve ledger. All amounts are in the stablecoin's smallest
/// unit (e.g. 1e-6 USD for a 6-decimal token).
///
/// On-chain, `attested_reserve` and `paused` live in the `Treasury` account and
/// `circulating` is projected from `Mint.supply` at instruction time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReserveState {
    /// Reserves attested on-chain (by the reserve oracle / attestor authority).
    pub attested_reserve: u64,
    /// Stablecoin currently in circulation. On-chain this is `Mint.supply`.
    pub circulating: u64,
    /// Set when an attestation revealed a shortfall. Blocks all minting.
    pub paused: bool,
}

impl ReserveState {
    pub const fn new(attested_reserve: u64, circulating: u64) -> Self {
        Self { attested_reserve, circulating, paused: false }
    }

    pub const fn with_paused(attested_reserve: u64, circulating: u64, paused: bool) -> Self {
        Self { attested_reserve, circulating, paused }
    }

    /// Headroom: how much more can be minted before hitting the reserve ceiling.
    /// Zero while paused, so a paused treasury never advertises capacity.
    pub const fn available_to_mint(&self) -> u64 {
        if self.paused {
            return 0;
        }
        self.attested_reserve.saturating_sub(self.circulating)
    }

    /// The core gate. Returns the post-mint circulating supply if the mint is
    /// allowed, or a `ReserveError` explaining why it is rejected. Pure: it does
    /// not mutate; the caller commits the result only on `Ok`.
    pub fn check_mint(&self, amount: u64) -> Result<u64, ReserveError> {
        if self.paused {
            return Err(ReserveError::Undercollateralized);
        }
        if amount == 0 {
            return Err(ReserveError::ZeroAmount);
        }
        let next = self
            .circulating
            .checked_add(amount)
            .ok_or(ReserveError::MathOverflow)?;
        // The invariant, stated once: circulating after mint <= attested reserve.
        if next > self.attested_reserve {
            return Err(ReserveError::ExceedsReserves);
        }
        Ok(next)
    }

    /// Apply a mint, mutating state. Only commits if `check_mint` passes.
    pub fn apply_mint(&mut self, amount: u64) -> Result<(), ReserveError> {
        let next = self.check_mint(amount)?;
        self.circulating = next;
        Ok(())
    }

    /// Settle (redeem) stablecoin out of circulation by burning it. This can
    /// never break the reserve invariant (it lowers circulating) and stays
    /// available while paused, because redemption is how solvency is restored.
    /// It must not underflow.
    pub fn check_settle(&self, amount: u64) -> Result<u64, ReserveError> {
        if amount == 0 {
            return Err(ReserveError::ZeroAmount);
        }
        self.circulating
            .checked_sub(amount)
            .ok_or(ReserveError::MathOverflow)
    }

    /// Apply a settlement, mutating state. Recomputes the pause flag: burning
    /// enough tokens to get back under the attested reserve restores solvency.
    pub fn apply_settle(&mut self, amount: u64) -> Result<(), ReserveError> {
        let next = self.check_settle(amount)?;
        self.circulating = next;
        if self.paused && self.circulating <= self.attested_reserve {
            self.paused = false;
        }
        Ok(())
    }

    /// Would this attestation leave the treasury solvent? Pure, no mutation.
    pub const fn check_attest(&self, new_reserve: u64) -> AttestOutcome {
        if new_reserve < self.circulating {
            AttestOutcome::Undercollateralized {
                shortfall: self.circulating - new_reserve,
            }
        } else {
            AttestOutcome::Solvent
        }
    }

    /// Apply a new attestation. The attested figure is ALWAYS recorded, because
    /// it is the truth about the reserves. If it is below circulating supply the
    /// treasury is paused and no further minting is possible until an attestation
    /// covers circulating supply again (or holders redeem back under the line).
    pub fn apply_attest(&mut self, new_reserve: u64) -> AttestOutcome {
        let outcome = self.check_attest(new_reserve);
        self.attested_reserve = new_reserve;
        self.paused = matches!(outcome, AttestOutcome::Undercollateralized { .. });
        outcome
    }

    /// Backing ratio in basis points (10_000 = fully backed). Handy for
    /// telemetry. Saturates at `MAX_BACKING_BPS` so the figure is always a
    /// storable, displayable number: with nothing circulating the ratio is
    /// mathematically undefined, and `u64::MAX` as a sentinel overflowed every
    /// int64 journal it was written to.
    pub fn backing_bps(&self) -> u128 {
        if self.circulating == 0 {
            return MAX_BACKING_BPS; // vacuously over-collateralized
        }
        let raw = (u128::from(self.attested_reserve) * 10_000) / u128::from(self.circulating);
        if raw > MAX_BACKING_BPS {
            MAX_BACKING_BPS
        } else {
            raw
        }
    }
}

// ---------------------------------------------------------------------------
// Pure-Rust unit tests of the reserve invariant. These run in the Anchor crate
// and, byte for byte, in the `reserve-core` crate.
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mint_within_reserves_is_allowed() {
        let mut s = ReserveState::new(1_000_000, 0);
        assert_eq!(s.available_to_mint(), 1_000_000);
        s.apply_mint(400_000).unwrap();
        assert_eq!(s.circulating, 400_000);
        assert_eq!(s.available_to_mint(), 600_000);
    }

    #[test]
    fn mint_exactly_to_the_ceiling_is_allowed() {
        let mut s = ReserveState::new(500, 100);
        s.apply_mint(400).unwrap(); // 100 + 400 == 500 == attested
        assert_eq!(s.circulating, 500);
        assert_eq!(s.available_to_mint(), 0);
    }

    #[test]
    fn mint_one_past_the_ceiling_is_rejected() {
        let s = ReserveState::new(500, 100);
        // 100 + 401 = 501 > 500 -> rejected, and state is untouched.
        assert_eq!(s.check_mint(401), Err(ReserveError::ExceedsReserves));
    }

    #[test]
    fn mint_rejection_does_not_mutate_state() {
        let mut s = ReserveState::new(500, 100);
        let before = s;
        assert!(s.apply_mint(9_999).is_err());
        assert_eq!(s, before, "a rejected mint must leave circulating unchanged");
    }

    #[test]
    fn zero_amount_mint_is_rejected() {
        let s = ReserveState::new(1_000, 0);
        assert_eq!(s.check_mint(0), Err(ReserveError::ZeroAmount));
    }

    #[test]
    fn mint_overflow_is_caught_not_wrapped() {
        // circulating near u64::MAX; a big mint would wrap without checked_add.
        let s = ReserveState::new(u64::MAX, u64::MAX - 5);
        assert_eq!(s.check_mint(100), Err(ReserveError::MathOverflow));
    }

    #[test]
    fn settle_lowers_circulating() {
        let mut s = ReserveState::new(1_000, 600);
        s.apply_settle(250).unwrap();
        assert_eq!(s.circulating, 350);
        // settling frees headroom back up.
        assert_eq!(s.available_to_mint(), 650);
    }

    #[test]
    fn settle_more_than_circulating_underflows_safely() {
        let s = ReserveState::new(1_000, 100);
        assert_eq!(s.check_settle(101), Err(ReserveError::MathOverflow));
    }

    #[test]
    fn zero_amount_settle_is_rejected() {
        let s = ReserveState::new(1_000, 100);
        assert_eq!(s.check_settle(0), Err(ReserveError::ZeroAmount));
    }

    // --- reserve loss handling (the fix for "a real loss was rejected") -----

    #[test]
    fn attestation_below_circulating_is_recorded_and_pauses_minting() {
        let mut s = ReserveState::new(1_000, 800);
        let out = s.apply_attest(600);
        assert_eq!(out, AttestOutcome::Undercollateralized { shortfall: 200 });
        // The truth is written down, not discarded.
        assert_eq!(s.attested_reserve, 600, "the real, lower reserve is recorded");
        assert!(s.paused, "a shortfall pauses the treasury");
        assert_eq!(s.available_to_mint(), 0, "a paused treasury advertises no headroom");
    }

    #[test]
    fn no_minting_is_possible_while_undercollateralized() {
        let mut s = ReserveState::new(1_000, 800);
        s.apply_attest(600);
        // Before the fix, the 600 attestation was rejected, 1_000 stayed on the
        // books, and this 200 mint was allowed against reserves that were gone.
        assert_eq!(s.check_mint(200), Err(ReserveError::Undercollateralized));
        assert_eq!(s.check_mint(1), Err(ReserveError::Undercollateralized));
    }

    #[test]
    fn a_covering_attestation_unpauses_the_treasury() {
        let mut s = ReserveState::new(1_000, 800);
        s.apply_attest(600);
        assert!(s.paused);
        assert_eq!(s.apply_attest(900), AttestOutcome::Solvent);
        assert!(!s.paused);
        assert_eq!(s.available_to_mint(), 100);
        s.apply_mint(100).unwrap();
        assert_eq!(s.check_mint(1), Err(ReserveError::ExceedsReserves));
    }

    #[test]
    fn redemption_can_restore_solvency_while_paused() {
        let mut s = ReserveState::new(1_000, 800);
        s.apply_attest(600); // shortfall of 200
        assert!(s.paused);
        // Holders redeem 200: circulating 600 == attested 600, solvency restored.
        s.apply_settle(200).unwrap();
        assert!(!s.paused);
        assert_eq!(s.circulating, 600);
        assert_eq!(s.available_to_mint(), 0);
    }

    #[test]
    fn partial_redemption_leaves_the_treasury_paused() {
        let mut s = ReserveState::new(1_000, 800);
        s.apply_attest(600);
        s.apply_settle(100).unwrap(); // circulating 700, still above 600
        assert!(s.paused);
        assert_eq!(s.check_mint(1), Err(ReserveError::Undercollateralized));
    }

    #[test]
    fn raising_attestation_opens_mint_headroom() {
        let mut s = ReserveState::new(1_000, 1_000);
        assert_eq!(s.available_to_mint(), 0);
        assert_eq!(s.apply_attest(1_500), AttestOutcome::Solvent);
        assert_eq!(s.available_to_mint(), 500);
        s.apply_mint(500).unwrap();
        assert_eq!(s.check_mint(1), Err(ReserveError::ExceedsReserves));
    }

    #[test]
    fn backing_ratio_never_below_par_when_solvent() {
        let s = ReserveState::new(1_200, 1_000);
        assert_eq!(s.backing_bps(), 12_000); // 120% backed
        let full = ReserveState::new(1_000, 1_000);
        assert_eq!(full.backing_bps(), 10_000); // exactly par
    }

    #[test]
    fn backing_ratio_saturates_instead_of_returning_a_giant_sentinel() {
        // nothing circulating: undefined ratio, reported as the ceiling
        assert_eq!(ReserveState::new(1_000, 0).backing_bps(), MAX_BACKING_BPS);
        // absurd over-collateralization also clamps, so the figure is storable
        assert_eq!(ReserveState::new(u64::MAX, 1).backing_bps(), MAX_BACKING_BPS);
        assert!(MAX_BACKING_BPS < u128::from(i64::MAX as u64));
    }

    #[test]
    fn backing_ratio_reports_the_shortfall_honestly() {
        let mut s = ReserveState::new(1_000, 800);
        s.apply_attest(600);
        assert_eq!(s.backing_bps(), 7_500); // 75% backed, visible on-chain
    }

    #[test]
    fn end_to_end_sequence_preserves_the_invariant() {
        let mut s = ReserveState::new(1_000_000, 0);
        // a realistic operator sequence; the invariant holds at every step.
        s.apply_mint(300_000).unwrap();
        s.apply_mint(500_000).unwrap();
        assert!(s.apply_mint(300_000).is_err()); // 800k + 300k > 1M -> blocked
        s.apply_settle(100_000).unwrap(); // now 700k circulating
        s.apply_mint(300_000).unwrap(); // 700k + 300k == 1M -> ok
        assert_eq!(s.circulating, 1_000_000);
        assert!(s.circulating <= s.attested_reserve, "reserve invariant");
    }
}
