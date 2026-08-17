"""PayLane: a reserve-gated Solana stablecoin payment rail.

The treasury program provably cannot mint or settle past on-chain-attested
reserves. This Python package is the off-chain agent that drives a deterministic
mirror of that program's account state, running the identical reserve gate, so
the workflow and the demo are keyless.

It ships no live submission client: nothing is deployed (see README, "What is
actually verified, and what is not"). Configuring the live environment variables
makes `Treasury` refuse to construct rather than pass the simulation off as a
cluster. The program's real behaviour is proven by `cargo test`.
"""

from .chain import ChainUnavailable, decode_treasury, instruction_data
from .config import PaylaneSettings, settings
from .ledger import AttestOutcome, ReserveError, ReserveState
from .treasury import Treasury, TreasuryEvent
from .agent import PaylaneAgent

__all__ = [
    "PaylaneSettings",
    "settings",
    "AttestOutcome",
    "ReserveError",
    "ReserveState",
    "Treasury",
    "TreasuryEvent",
    "PaylaneAgent",
    "ChainUnavailable",
    "decode_treasury",
    "instruction_data",
]

__version__ = "0.1.0"
