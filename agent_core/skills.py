"""Skills — an agent's capabilities as named, reusable units.

A *skill* is a self-contained capability: a name, a human-readable purpose, the
model tier it runs on, the tools it may call, and the instruction that defines
it. ADK agents are assembled directly from skills, so the mapping
"capability -> agent" is legible in one place and the same skill can be reused
by an ad-hoc path or served over MCP.

Keeping guardrail expectations *in the instruction* means the model is told its
own limits; keeping tools *in the skill* means capability and authority are
declared together.

Verified against the google-adk API:
  * `from google.adk.agents import Agent`  (Agent is an alias for LlmAgent).
  * Constructor args used: name, model, description, instruction, tools,
    sub_agents, output_key.
  * Tools are plain Python functions (ADK builds the declaration from the
    signature + docstring) — passed directly in `tools=[...]`.
  * A parent delegates to a child by listing it in `sub_agents=[...]`; the
    child's `description` is what the parent's LLM uses to decide to transfer.
  * `output_key` writes each agent's final text into shared session state so
    later stages can read the running analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from google.adk.agents import Agent


@dataclass(frozen=True)
class Skill:
    """A named agent capability: purpose + model tier + tools + instruction."""

    name: str
    summary: str            # one-line description (used as the ADK agent description)
    model: str              # model id this skill runs on
    instruction: str        # the behavioural contract for the agent
    tools: list[Callable] = field(default_factory=list)
    output_key: str = ""    # shared-state key the skill writes its result to

    @property
    def agent_name(self) -> str:
        """ADK-safe agent name (identifiers can't contain hyphens)."""
        return f"{self.name.replace('-', '_')}_agent"


def agent_from_skill(skill: Skill, **overrides) -> Agent:
    """Build an ADK Agent from a named Skill (capability -> agent, in one place).

    `overrides` may replace `tools` (e.g. to swap in an MCP toolset) or any other
    Agent kwarg.
    """
    kwargs = dict(
        name=skill.agent_name,
        model=skill.model,
        description=skill.summary,
        instruction=skill.instruction,
        tools=list(skill.tools),
        output_key=skill.output_key,
    )
    kwargs.update(overrides)
    return Agent(**kwargs)


def build_supervisor(
    *,
    name: str,
    description: str,
    instruction: str,
    skills: Sequence[Skill],
    model: str = "",
    sub_agents: Sequence[Agent] | None = None,
) -> Agent:
    """Assemble a supervisor (root) agent that delegates to sub-agents built from
    `skills`, in order.

    Pass `sub_agents` directly to override the default 1:1 skill->agent build
    (e.g. when one sub-agent needs an MCP toolset or an A2A remote handoff). The
    supervisor's `model` defaults to the first skill's model.
    """
    children = list(sub_agents) if sub_agents is not None else [agent_from_skill(s) for s in skills]
    return Agent(
        name=name,
        model=model or (skills[0].model if skills else ""),
        description=description,
        instruction=instruction,
        sub_agents=children,
    )
