"""PayLane configuration, extending agent-core's BaseSettings.

The load-bearing sponsor tech is Solana/Anchor: an on-chain `paylane` program
(`programs/paylane/src/lib.rs`) holds a `Treasury` account bound to one SPL mint,
and its `mint`/`settle` instructions run a reserve gate against the live
`Mint.supply` that provably cannot exceed attested reserves. That program is
exercised end to end against the real SPL Token program by
`programs/paylane/tests/program.rs`.

This Python layer is the PayLane agent: it runs the SAME reserve gate off-chain,
under agent-core guardrails, over a deterministic mirror of the treasury account,
so the operator workflow and the demo run keyless.

Honest limitation: there is NO live submission client in this repo, because
nothing is deployed and the Solana toolchain is not installed here. If the live
environment variables are set, `Treasury` refuses to construct instead of running
the mirror and calling it devnet. Testnet only; never mainnet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_core import BaseSettings, env_bool, env_str


@dataclass(frozen=True)
class PaylaneSettings(BaseSettings):
    env_prefix: str = "PAYLANE"
    app_name: str = "paylane"

    # Solana cluster (devnet only). Live path submits program instructions here.
    rpc_url: str = field(default_factory=lambda: env_str("PAYLANE_RPC_URL"))
    cluster: str = field(default_factory=lambda: env_str("PAYLANE_CLUSTER", "devnet"))

    # The deployed paylane program id (base58). Present -> live path is eligible.
    program_id: str = field(
        default_factory=lambda: env_str(
            "PAYLANE_PROGRAM_ID", "PayL111111111111111111111111111111111111111"
        )
    )

    # The stablecoin SPL mint the treasury controls, and its decimals.
    token_mint: str = field(default_factory=lambda: env_str("PAYLANE_TOKEN_MINT"))
    decimals: int = 6

    # Operator keypair path (live signing). Never read as a value here.
    keypair_path: str = field(default_factory=lambda: env_str("PAYLANE_KEYPAIR"))

    offline: bool = field(default_factory=lambda: env_bool("PAYLANE_OFFLINE", False))

    # Keyless by default: the audit journal stays in-process unless an operator
    # explicitly opts into Firestore. Without this, a stray GOOGLE_CLOUD_PROJECT
    # in the environment would make `paylane demo` write to someone's real
    # project, which is not something a demo should do behind your back.
    use_in_memory_state: bool = field(
        default_factory=lambda: env_bool("PAYLANE_IN_MEMORY_STATE", True)
    )

    @property
    def use_chain(self) -> bool:
        """True when a live Solana submission path is configured (else in-memory
        mirror of the on-chain treasury account)."""
        return bool(self.rpc_url and self.token_mint and self.keypair_path) and not self.offline


settings = PaylaneSettings()
