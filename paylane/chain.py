"""Wire format for the on-chain paylane program, and the chain seam.

**Honest position.** This repo does not ship a live Solana submission client,
because nothing is deployed: there is no funded devnet keypair and no deployed
program id here, and the Solana/Anchor toolchain is not installed in this
environment. Rather than an adapter that pretends and then raises, the live path
is absent and the treasury FAILS CLOSED if it is configured (see
`treasury.Treasury`), before any state is touched.

What is real and tested here, with no network and no keys:

* `ix_discriminator` / `instruction_data` - the exact bytes Anchor expects for
  `attest_reserve`, `mint` and `settle` (sha256("global:<name>")[:8] + a
  little-endian u64).
* `decode_treasury` - the `Treasury` account layout from
  `programs/paylane/src/lib.rs`, byte for byte, including the space constant.

What is proven elsewhere: the program's actual behaviour, by
`programs/paylane/tests/program.rs`, which runs the instructions against the real
SPL Token program in `solana-program-test`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

# Anchor account layout for `Treasury` (lib.rs `Treasury::SPACE`): 8-byte
# discriminator, three 32-byte pubkeys, a u64, a bool, a u8 bump.
TREASURY_ACCOUNT_LEN = 8 + 32 * 3 + 8 + 1 + 1

# The single-u64 instructions of the program, by their `#[program]` name.
INSTRUCTIONS = ("attest_reserve", "mint", "settle")


class ChainUnavailable(RuntimeError):
    """The live path was requested but cannot run. Raised before any state
    change, so callers fail closed instead of recording a fiction."""


def ix_discriminator(name: str) -> bytes:
    """Anchor's 8-byte instruction discriminator: sha256("global:<name>")[:8].

    `name` is the snake_case instruction name as written in `#[program]`.
    """
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def encode_u64(value: int) -> bytes:
    if not (0 <= value <= (1 << 64) - 1):
        raise ValueError(f"value out of u64 range: {value}")
    return value.to_bytes(8, "little")


def instruction_data(name: str, amount: int) -> bytes:
    """Borsh-encoded payload for the single-u64 instructions."""
    if name not in INSTRUCTIONS:
        raise ValueError(f"unknown instruction: {name}")
    return ix_discriminator(name) + encode_u64(amount)


@dataclass(frozen=True)
class TreasuryAccount:
    """Decoded on-chain `Treasury`.

    `circulating` is deliberately absent: on-chain it is never stored, it is read
    from the bound `Mint.supply` on every instruction so the ledger cannot drift
    from the tokens that actually exist.
    """

    authority: bytes
    attestor: bytes
    mint: bytes
    attested_reserve: int
    paused: bool
    bump: int


def decode_treasury(data: bytes) -> TreasuryAccount:
    """Decode a raw `Treasury` account. Rejects a wrong-sized buffer or a
    non-boolean pause flag rather than silently reading garbage."""
    if len(data) != TREASURY_ACCOUNT_LEN:
        raise ValueError(
            f"treasury account is {len(data)} bytes, expected {TREASURY_ACCOUNT_LEN}"
        )
    body = data[8:]
    paused = body[104]
    if paused not in (0, 1):
        raise ValueError(f"paused flag is not a bool: {paused}")
    return TreasuryAccount(
        authority=body[0:32],
        attestor=body[32:64],
        mint=body[64:96],
        attested_reserve=int.from_bytes(body[96:104], "little"),
        paused=bool(paused),
        bump=body[105],
    )


class ChainClient(Protocol):
    """The seam a live client would implement. Nothing in this repo implements
    it against a real cluster; the tests inject a fake to pin the ordering rule
    that submission happens BEFORE any local state is recorded."""

    def submit(self, instruction: str, amount: int) -> str:
        """Submit and CONFIRM the instruction, returning the real transaction
        signature. Must raise on any failure; the caller then records nothing."""

    def fetch(self) -> tuple[int, int, bool]:
        """Canonical on-chain state after a confirmed submission:
        (attested_reserve, circulating supply, paused)."""
