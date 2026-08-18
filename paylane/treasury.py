"""Treasury adapter seam: the on-chain paylane program, mirrored off-chain.

Two modes, and the repo is explicit about which one it is in.

**Offline (default, keyless).** A deterministic in-memory mirror of the
`Treasury` account and the program's `attest_reserve` / `mint` / `settle`
instructions, running the SAME reserve gate (`ledger.ReserveState`) the on-chain
program runs. Events produced here are marked `simulated = True` and carry a
`sim:` prefixed digest, never a transaction signature: nothing has been sent
anywhere, and the UI and CLI say so.

**Live (a `ChainClient` is supplied).** Order matters and is the fix for the
worst bug this seam had: submit and confirm FIRST, take the real signature, then
re-read canonical state from the chain. If submission raises, the mirror is not
touched and a failed event is recorded with no signature. There is no path where
a failure leaves behind a "successful" event.

This repo ships no live client (see `chain.py` for why). If the environment is
configured for the live path and no client is injected, the treasury refuses to
construct rather than silently running the simulation and calling it devnet.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional

from .chain import ChainClient, ChainUnavailable
from .config import PaylaneSettings, settings
from .ledger import ReserveState, ReserveViolation


def _sim_digest(kind: str, seq: int, amount: int, circulating: int) -> str:
    """Deterministic digest for an offline event.

    Deliberately NOT shaped like a Solana signature. It is prefixed `sim:` so no
    reader, and no screenshot, can mistake it for something that happened on a
    cluster.
    """
    raw = f"{kind}:{seq}:{amount}:{circulating}".encode()
    return "sim:" + hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class TreasuryEvent:
    """One recorded program event, mirroring the on-chain `#[event]` structs."""

    kind: str  # "Minted" | "Settled" | "ReserveAttested" | "Rejected(...)" | "Failed(...)"
    amount: int
    circulating: int
    attested_reserve: int
    signature: str
    ok: bool = True
    reason: str = ""
    paused: bool = False
    #: True when this event came from the offline mirror and no transaction
    #: exists. False only for events backed by a confirmed on-chain signature.
    simulated: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "TreasuryEvent":
        return cls(
            kind=d["kind"],
            amount=d["amount"],
            circulating=d["circulating"],
            attested_reserve=d["attested_reserve"],
            signature=d.get("signature", ""),
            ok=d.get("ok", True),
            reason=d.get("reason", ""),
            paused=d.get("paused", False),
            simulated=d.get("simulated", True),
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "amount": self.amount,
            "circulating": self.circulating,
            "attested_reserve": self.attested_reserve,
            "signature": self.signature,
            "ok": self.ok,
            "reason": self.reason,
            "paused": self.paused,
            "simulated": self.simulated,
        }


@dataclass
class Treasury:
    """Off-chain mirror of the on-chain `Treasury` account + instruction surface."""

    cfg: PaylaneSettings = field(default_factory=lambda: settings)
    state: ReserveState = field(default_factory=lambda: ReserveState(0, 0))
    events: List[TreasuryEvent] = field(default_factory=list)
    chain: Optional[ChainClient] = None
    _seq: int = 0

    def __post_init__(self) -> None:
        if self.chain is None and self.cfg.use_chain:
            # Fail closed. Running the simulation under a live configuration is
            # exactly the lie this seam used to tell.
            raise ChainUnavailable(
                "PAYLANE_RPC_URL / PAYLANE_TOKEN_MINT / PAYLANE_KEYPAIR are set, but this "
                "build has no live submission client. Deploy the program and inject a "
                "ChainClient, or unset them (or set PAYLANE_OFFLINE=1) to run the offline "
                "mirror."
            )

    @property
    def live(self) -> bool:
        return self.chain is not None

    # --- instructions (mirror the Anchor program) --------------------------

    def attest_reserve(self, new_reserve: int) -> TreasuryEvent:
        """Attestor writes the real reserve figure.

        A figure below circulating supply is RECORDED and pauses minting, exactly
        as `attest_reserve` does on-chain. It is the truth about a shortfall, so
        hiding it would leave a stale, too-high figure on the books.
        """
        return self._apply("ReserveAttested", "attest_reserve", new_reserve)

    def mint(self, amount: int) -> TreasuryEvent:
        """Mint `amount`. The reserve gate runs first; a mint that would exceed
        attested reserves is rejected and state is untouched (as on-chain)."""
        return self._apply("Minted", "mint", amount)

    def settle(self, amount: int) -> TreasuryEvent:
        """Settle (redeem/burn) `amount` out of circulation. On-chain this burns
        the holder's tokens, so circulating only falls when tokens disappear."""
        return self._apply("Settled", "settle", amount)

    # --- history / views ---------------------------------------------------

    def history(self) -> List[TreasuryEvent]:
        return list(self.events)

    def backing_bps(self) -> int:
        return self.state.backing_bps()

    @property
    def paused(self) -> bool:
        return self.state.paused

    # --- persistence (so separate CLI invocations form one workflow) --------

    def snapshot(self) -> dict:
        """Everything needed to resume this treasury in a later process."""
        return {
            "attested_reserve": self.state.attested_reserve,
            "circulating": self.state.circulating,
            "paused": self.state.paused,
            "seq": self._seq,
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def restore(
        cls,
        snap: dict,
        cfg: PaylaneSettings | None = None,
        chain: Optional[ChainClient] = None,
    ) -> "Treasury":
        t = cls(cfg=cfg or settings, chain=chain)
        t.state = ReserveState(
            int(snap["attested_reserve"]),
            int(snap["circulating"]),
            bool(snap.get("paused", False)),
        )
        t._seq = int(snap.get("seq", 0))
        t.events = [TreasuryEvent.from_dict(e) for e in snap.get("events", [])]
        return t

    # --- internals ---------------------------------------------------------

    def _apply(self, kind: str, ix: str, amount: int) -> TreasuryEvent:
        """Gate, then submit, then commit. Never commit before submitting."""
        # 1. Preflight the same gate the program runs, on a COPY, so a rejection
        #    cannot leave the mirror half-updated.
        probe = ReserveState(
            self.state.attested_reserve, self.state.circulating, self.state.paused
        )
        try:
            if ix == "mint":
                probe.apply_mint(amount)
            elif ix == "settle":
                probe.apply_settle(amount)
            else:
                probe.apply_attest(amount)
        except ReserveViolation as v:
            return self._record(f"Rejected({kind})", amount, ok=False, reason=v.err.value)

        # 2. Submit. In live mode nothing local moves until the cluster confirms.
        signature = ""
        simulated = True
        if self.chain is not None:
            try:
                signature = self.chain.submit(ix, amount)
            except Exception as exc:  # submission failed: record it, change nothing
                return self._record(
                    f"Failed({kind})",
                    amount,
                    ok=False,
                    reason=f"submission failed: {exc}",
                    simulated=False,
                )
            simulated = False

        # 3. Commit locally, then re-read canonical state from the chain if live.
        self.state = probe
        if self.chain is not None:
            reserve, circulating, paused = self.chain.fetch()
            self.state = ReserveState(reserve, circulating, paused)
        return self._record(kind, amount, signature=signature, simulated=simulated)

    def _record(
        self,
        kind: str,
        amount: int,
        *,
        ok: bool = True,
        reason: str = "",
        signature: str = "",
        simulated: bool = True,
    ) -> TreasuryEvent:
        self._seq += 1
        if ok and simulated and not signature:
            signature = _sim_digest(kind, self._seq, amount, self.state.circulating)
        ev = TreasuryEvent(
            kind=kind,
            amount=amount,
            circulating=self.state.circulating,
            attested_reserve=self.state.attested_reserve,
            signature=signature,  # empty for anything that produced no transaction
            ok=ok,
            reason=reason,
            paused=self.state.paused,
            simulated=simulated,
        )
        self.events.append(ev)
        return ev
