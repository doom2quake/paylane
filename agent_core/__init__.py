"""agent-core — a small, reusable core for autonomous multi-agent products on ADK.

Distilled from a shipped Google ADK product (Atlas). The pieces every autonomous
agent product re-implements, done once and tested:

  * skills      — capabilities as named units; assemble a supervisor from them.
  * guardrails  — read-only-SQL screen, content-safety screen, action rate limiter.
  * config      — env-driven settings base (subclass per app).
  * sinks       — guarded outbound actions (Slack alert, GitHub/endpoint ticket).
  * router      — domain-aware "send the outcome to the right place" router.
  * state       — durable run memory (Firestore + in-memory) with recurrence.
  * runner      — run an ADK agent graph and collect per-stage output.
  * mcp         — serve/consume tools over the Model Context Protocol.

Typical wiring:

    from agent_core import (BaseSettings, Skill, build_supervisor, ActionLimiter,
                            ActionPolicy, Notifier, Router, AlertHandler, TicketHandler,
                            StateStore, run_agent)

    settings = BaseSettings(env_prefix="MYAPP")
    limiter  = ActionLimiter(ActionPolicy.from_env("MYAPP"))
    store    = StateStore.create(settings)
    notifier = Notifier(settings, limiter)
    router   = Router([AlertHandler(notifier), TicketHandler(notifier)])
"""

from .config import BaseSettings, env_bool, env_int, env_str
from .guardrails import (
    ActionLimiter,
    ActionPolicy,
    assert_read_only,
    screen_content,
)
from .router import (
    DEFAULT_ROUTING,
    DEFAULT_SIGNALS,
    AlertHandler,
    Handler,
    KeywordClassifier,
    Route,
    Router,
    TicketHandler,
)
from .runner import RunResult, run_agent
from .sinks import Notifier
from .skills import Skill, agent_from_skill, build_supervisor
from .state import StateStore, new_run_id, signature_of

__all__ = [
    "BaseSettings", "env_bool", "env_int", "env_str",
    "ActionLimiter", "ActionPolicy", "assert_read_only", "screen_content",
    "Skill", "agent_from_skill", "build_supervisor",
    "Notifier",
    "Router", "Handler", "KeywordClassifier", "Route", "AlertHandler",
    "TicketHandler", "DEFAULT_ROUTING", "DEFAULT_SIGNALS",
    "StateStore", "new_run_id", "signature_of",
    "RunResult", "run_agent",
]

__version__ = "0.1.0"
