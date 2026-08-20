"""PayLane agent: wraps the reserve-gated treasury in agent-core guardrails + audit.

Every treasury action runs as a recorded agent action. The submit step passes
agent-core's ActionLimiter (rate / dry-run guardrail) and the whole run is
journaled in the StateStore, so the UI can replay the agent's activity log (plan
-> gate -> submit -> the guardrail decisions) alongside the on-chain event
history. The reserve gate itself is the hard, program-enforced limit; the
ActionLimiter is the soft operational limit on how fast the agent may act.
"""

from __future__ import annotations

from typing import Any

from agent_core import ActionLimiter, ActionPolicy, StateStore, signature_of

from .config import settings
from .treasury import Treasury, TreasuryEvent

class PaylaneAgent:
    """One agent per treasury.

    The ActionLimiter is per-agent, not a module-level singleton: two operators
    running two treasuries in one process must not silently consume each other's
    action budget. Bounds come from PAYLANE_DRY_RUN /
    PAYLANE_MAX_ACTIONS_PER_CYCLE / PAYLANE_MAX_ACTIONS_PER_HOUR.
    """

    def __init__(
        self,
        treasury: Treasury | None = None,
        limiter: ActionLimiter | None = None,
    ) -> None:
        self.treasury = treasury or Treasury()
        self.limiter = limiter or ActionLimiter(ActionPolicy.from_env("PAYLANE"))

    def attest(self, new_reserve: int) -> dict[str, Any]:
        """Record a new on-chain reserve attestation."""
        return self._run("attest", new_reserve, self.treasury.attest_reserve)

    def mint(self, amount: int) -> dict[str, Any]:
        """Propose a mint. The reserve gate decides; the agent only submits the
        allowed ones. A rejected mint is journaled, not raised."""
        return self._run("mint", amount, self.treasury.mint)

    def settle(self, amount: int) -> dict[str, Any]:
        """Settle stablecoin out of circulation."""
        return self._run("settle", amount, self.treasury.settle)

    def _run(self, kind: str, amount: int, action) -> dict[str, Any]:
        store = StateStore.create(settings)
        run_id = store.start_run(trigger={kind: amount})

        allowed, reason = self.limiter.check(run_id, kind)
        if not allowed:
            store.record_guardrail(run_id, "ACTION_LIMITER", "blocked", f"{kind}: {reason}")
            store.set_status(run_id, "blocked")
            return {"status": "blocked", "reason": reason, "run_id": run_id, "kind": kind}

        store.record_guardrail(run_id, "ACTION_LIMITER", "allowed", reason)
        ev: TreasuryEvent = action(amount)

        store.set_data(run_id, "event", ev.to_dict())
        store.set_data(run_id, "state", {
            "circulating": self.treasury.state.circulating,
            "attested_reserve": self.treasury.state.attested_reserve,
            "paused": self.treasury.paused,
            "backing_bps": self.treasury.backing_bps(),
        })
        store.detect_recurrence(run_id, signature_of(kind, ev.signature or ev.reason))
        store.set_status(run_id, "settled" if ev.ok else "rejected")

        return {
            "status": "ok" if ev.ok else "rejected",
            "run_id": run_id,
            "kind": kind,
            "amount": amount,
            "event": ev.to_dict(),
            "circulating": self.treasury.state.circulating,
            "attested_reserve": self.treasury.state.attested_reserve,
            "available_to_mint": self.treasury.state.available_to_mint(),
            "backing_bps": self.treasury.backing_bps(),
            "paused": self.treasury.paused,
            "reason": ev.reason,
        }

    def state(self) -> dict[str, Any]:
        s = self.treasury.state
        return {
            "attested_reserve": s.attested_reserve,
            "circulating": s.circulating,
            "available_to_mint": s.available_to_mint(),
            "backing_bps": s.backing_bps(),
            "paused": s.paused,
            "simulated": not self.treasury.live,
        }

    def history(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.treasury.history()]
