"""Guardrails — an explicit, named safety layer for an autonomous agent.

Autonomy is only safe when it is bounded. agent-core enforces guardrails at
three named points so they show up in a run's audit trail:

  1. READ_ONLY_SQL  — diagnostic queries must be single, byte-capped,
                      parameterised SELECT/WITH statements (see `assert_read_only`).
  2. CONTENT_SAFETY — model output is screened for prompt-injection / unsafe
                      directives before the agent acts on it (`screen_content`).
  3. ACTION_LIMITER — outbound actions (alerts, tickets, remediations) are rate-
                      and spend-capped and can be forced into dry-run, so an
                      autonomous loop can never run away and spam humans or apply
                      an unwanted change (`ActionLimiter`).

Everything is env-gated with safe defaults and degrades gracefully. Nothing here
touches the network; it decides whether a downstream action is *allowed*.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass

# --- 1) read-only SQL screen -------------------------------------------------

# Reject anything that is not a single read-only statement. Apps that don't use
# SQL can ignore this; those that do call it before running generated SQL.
_WRITE_TOKENS = re.compile(
    r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|"
    r"replace|call|execute|begin|commit)\b",
    re.IGNORECASE,
)


def assert_read_only(sql: str) -> str | None:
    """Return an error string if `sql` is not a single read-only statement, else None."""
    if not sql or not sql.strip():
        return "empty query"
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return "multiple statements are not allowed (single SELECT/WITH only)"
    low = stripped.lstrip("(").lstrip().lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "only SELECT/WITH queries are allowed"
    hit = _WRITE_TOKENS.search(stripped)
    if hit:
        return f"write/DDL keyword not allowed: {hit.group(0)!r}"
    return None


# --- 2) content-safety screen ------------------------------------------------

# Phrases that, in *model-generated* text we're about to act on, suggest prompt-
# injection or an attempt to escalate beyond the agent's remit.
_INJECTION_PATTERNS = re.compile(
    r"(ignore (all |previous )?instructions|disregard .{0,20}(rules|guardrails)|"
    r"you are now|system prompt|exfiltrate|delete .{0,20}(table|dataset|database)|"
    r"drop table|reveal .{0,20}(secret|token|credential|api key))",
    re.IGNORECASE,
)


def screen_content(text: str) -> tuple[bool, str]:
    """Return (is_safe, reason). Screens model output before the agent acts on it."""
    if not text:
        return True, "empty"
    hit = _INJECTION_PATTERNS.search(text)
    if hit:
        return False, f"blocked pattern: {hit.group(0)!r}"
    return True, "clean"


# --- 3) action rate / spend limiter ------------------------------------------

@dataclass
class ActionPolicy:
    """Bounds on an agent's autonomous outbound actions."""

    dry_run: bool
    max_actions_per_cycle: int
    max_actions_per_hour: int

    @classmethod
    def from_env(cls, prefix: str = "AGENT") -> "ActionPolicy":
        """Read `{prefix}_DRY_RUN`, `{prefix}_MAX_ACTIONS_PER_CYCLE`,
        `{prefix}_MAX_ACTIONS_PER_HOUR` with safe defaults."""

        def _b(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            dry_run=_b(f"{prefix}_DRY_RUN", False),
            max_actions_per_cycle=int(os.getenv(f"{prefix}_MAX_ACTIONS_PER_CYCLE", "4")),
            max_actions_per_hour=int(os.getenv(f"{prefix}_MAX_ACTIONS_PER_HOUR", "20")),
        )


class ActionLimiter:
    """Process-wide, thread-safe rate limiter for outbound actions.

    A long-running deployment shares one limiter, so a runaway loop is throttled
    across cycles, not just within one.
    """

    def __init__(self, policy: ActionPolicy | None = None) -> None:
        self.policy = policy or ActionPolicy.from_env()
        self._lock = threading.Lock()
        self._recent: list[float] = []  # unix timestamps of allowed actions
        self._cycle_counts: dict[str, int] = {}

    def check(self, run_id: str, kind: str) -> tuple[bool, str]:
        """Return (allowed, reason). `kind` is e.g. 'alert', 'ticket', 'remediate'."""
        if self.policy.dry_run:
            return False, f"dry-run enabled; {kind} suppressed"
        now = time.time()
        with self._lock:
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= self.policy.max_actions_per_hour:
                return False, f"hourly action cap reached ({self.policy.max_actions_per_hour}/h)"
            used = self._cycle_counts.get(run_id, 0)
            if used >= self.policy.max_actions_per_cycle:
                return False, f"per-cycle action cap reached ({self.policy.max_actions_per_cycle}/cycle)"
            self._recent.append(now)
            self._cycle_counts[run_id] = used + 1
            return True, f"allowed ({used + 1}/{self.policy.max_actions_per_cycle} this cycle)"

    def reset_cycle(self, run_id: str) -> None:
        """Clear the per-cycle counter for a run (call at the start of a new cycle)."""
        with self._lock:
            self._cycle_counts.pop(run_id, None)
