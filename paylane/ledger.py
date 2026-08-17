"""The reserve invariant, in Python: a faithful port of `programs/paylane/src/reserve.rs`.

This is deliberately a line-for-line mirror of the on-chain Rust gate so the
off-chain agent's decision is identical to what the program will enforce. Amounts
are u64 in the smallest token unit; overflow is checked explicitly (Python ints
are unbounded, so we assert the u64 ceiling ourselves to reproduce the on-chain
`checked_add` / `checked_sub` behaviour exactly).

The rule: circulating supply can never exceed attested reserves. A mint that
would break it is rejected and leaves state untouched, exactly as on-chain.

Reserve losses are accepted, not hidden. An attestation reporting LESS backing
than is circulating is the truth about a shortfall, so it is recorded and the
treasury is paused until solvency is restored, exactly as `apply_attest` does in
`reserve.rs`. `tests/test_paylane.py` pins each of these behaviours against the
matching Rust unit test by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

U64_MAX = (1 << 64) - 1

#: Ceiling for the reported backing ratio: 100_000% in basis points, and the
#: value reported when nothing is circulating and the ratio is undefined. Mirrors
#: `MAX_BACKING_BPS` in reserve.rs. A `u64::MAX` sentinel overflowed every int64
#: journal it was written to.
MAX_BACKING_BPS = 10_000_000


class ReserveError(Enum):
    """Mirrors the Rust `ReserveError` / on-chain `PaylaneError` variants."""

    EXCEEDS_RESERVES = "mint would exceed on-chain attested reserves"
    MATH_OVERFLOW = "arithmetic overflow"
    ZERO_AMOUNT = "amount must be greater than zero"
    UNDERCOLLATERALIZED = "treasury is undercollateralized; minting is paused"


class ReserveViolation(Exception):
    """Raised when a proposed action violates the reserve invariant."""

    def __init__(self, err: ReserveError) -> None:
        super().__init__(err.value)
        self.err = err


@dataclass(frozen=True)
class AttestOutcome:
    """Result of applying an attestation. Mirrors the Rust `AttestOutcome` enum.

    An attestation is always accepted; the outcome says whether the treasury is
    still solvent afterwards. `shortfall` is 0 when solvent.
    """

    solvent: bool
    shortfall: int = 0


@dataclass
class ReserveState:
    """Mirror of the on-chain `Treasury` reserve fields.

    On-chain `circulating` is not stored: it is read from the bound SPL
    `Mint.supply` on every instruction. Off-chain this field is the mirror of
    that supply.
    """

    attested_reserve: int
    circulating: int
    paused: bool = False

    def __post_init__(self) -> None:
        for name in ("attested_reserve", "circulating"):
            v = getattr(self, name)
            if not (0 <= v <= U64_MAX):
                raise ValueError(f"{name} out of u64 range: {v}")

    def available_to_mint(self) -> int:
        """Headroom before hitting the reserve ceiling (saturating at 0).
        Zero while paused, so a paused treasury never advertises capacity."""
        if self.paused:
            return 0
        return max(0, self.attested_reserve - self.circulating)

    def check_mint(self, amount: int) -> int:
        """Return post-mint circulating if allowed, else raise ReserveViolation.
        Pure: does not mutate."""
        if self.paused:
            raise ReserveViolation(ReserveError.UNDERCOLLATERALIZED)
        if amount <= 0:
            raise ReserveViolation(ReserveError.ZERO_AMOUNT)
        nxt = self.circulating + amount
        if nxt > U64_MAX:  # on-chain checked_add would fail here
            raise ReserveViolation(ReserveError.MATH_OVERFLOW)
        # The invariant, stated once.
        if nxt > self.attested_reserve:
            raise ReserveViolation(ReserveError.EXCEEDS_RESERVES)
        return nxt

    def apply_mint(self, amount: int) -> None:
        self.circulating = self.check_mint(amount)

    def check_settle(self, amount: int) -> int:
        """Redemption stays available while paused: burning tokens is how
        solvency is restored."""
        if amount <= 0:
            raise ReserveViolation(ReserveError.ZERO_AMOUNT)
        nxt = self.circulating - amount
        if nxt < 0:  # on-chain checked_sub would fail here
            raise ReserveViolation(ReserveError.MATH_OVERFLOW)
        return nxt

    def apply_settle(self, amount: int) -> None:
        self.circulating = self.check_settle(amount)
        if self.paused and self.circulating <= self.attested_reserve:
            self.paused = False

    def check_attest(self, new_reserve: int) -> AttestOutcome:
        """Would this attestation leave the treasury solvent? Pure, no mutation."""
        if not (0 <= new_reserve <= U64_MAX):
            raise ReserveViolation(ReserveError.MATH_OVERFLOW)
        if new_reserve < self.circulating:
            return AttestOutcome(solvent=False, shortfall=self.circulating - new_reserve)
        return AttestOutcome(solvent=True)

    def apply_attest(self, new_reserve: int) -> AttestOutcome:
        """The attested figure is ALWAYS recorded, because it is the truth about
        the reserves. Below circulating supply it pauses the treasury: no further
        minting until an attestation covers circulating again, or holders redeem
        back under the line."""
        outcome = self.check_attest(new_reserve)
        self.attested_reserve = new_reserve
        self.paused = not outcome.solvent
        return outcome

    def backing_bps(self) -> int:
        """Backing ratio in basis points (10_000 = par). Integer, matches Rust."""
        if self.circulating == 0:
            return MAX_BACKING_BPS
        return min(MAX_BACKING_BPS, (self.attested_reserve * 10_000) // self.circulating)
