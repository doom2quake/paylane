"""Run an assembled ADK agent graph and collect its per-stage output.

A thin, dependency-light wrapper over ADK's Runner + session service. It sends a
single user prompt to a root agent, streams events to completion, and returns
the final text plus the shared session state (each skill's `output_key` value),
so an app gets the whole running analysis without re-implementing the event loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from google.adk.agents import Agent


@dataclass
class RunResult:
    final_text: str
    state: dict[str, Any] = field(default_factory=dict)
    author_texts: list[tuple[str, str]] = field(default_factory=list)  # (agent_name, text)


async def run_agent(
    root_agent: Agent,
    prompt: str,
    *,
    app_name: str = "agent",
    user_id: str = "system",
    session_id: Optional[str] = None,
) -> RunResult:
    """Execute `root_agent` on `prompt`, return the final text + collected state.

    Uses an in-memory session so no external state service is required; an app
    that wants durable ADK sessions can swap the session service and call the
    Runner directly.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    session_service = InMemorySessionService()
    sid = session_id or "session"
    await session_service.create_session(app_name=app_name, user_id=user_id, session_id=sid)

    runner = Runner(agent=root_agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_text = ""
    authors: list[tuple[str, str]] = []
    async for event in runner.run_async(user_id=user_id, session_id=sid, new_message=content):
        text = _event_text(event)
        if text:
            authors.append((getattr(event, "author", "") or "", text))
        if getattr(event, "is_final_response", None) and event.is_final_response():
            if text:
                final_text = text

    session = await session_service.get_session(app_name=app_name, user_id=user_id, session_id=sid)
    state = dict(session.state) if session and getattr(session, "state", None) else {}
    return RunResult(final_text=final_text or (authors[-1][1] if authors else ""),
                     state=state, author_texts=authors)


def _event_text(event: Any) -> str:
    """Best-effort extraction of text from an ADK event's content parts."""
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return ""
    return "".join(getattr(p, "text", "") or "" for p in content.parts).strip()
