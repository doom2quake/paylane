"""Outbound sinks — deliver an alert or open a ticket to a real destination.

Real delivery when configured (Slack incoming webhook, GitHub issue, or a
generic HTTP endpoint); a durable, observable no-op otherwise, so a demo still
shows the action and nothing breaks without credentials.

Every outbound action passes the ACTION_LIMITER guardrail first (rate/spend cap
+ dry-run). A `Notifier` bundles the settings + limiter + an optional recorder
callback, so it is decoupled from any app's module globals::

    notifier = Notifier(settings, ActionLimiter(ActionPolicy.from_env("ATLAS")))
    notifier.send_alert("#data-ops", "Revenue drop", "z ~ -7 ...", "critical")

Apps expose ADK agent tools as thin wrappers around a shared `Notifier` (the
tool's docstring carries the app-specific meaning the model reasons over).
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from typing import Any, Callable, Optional

from .config import BaseSettings
from .guardrails import ActionLimiter

# recorder(guardrail_name, outcome, detail) -> None ; best-effort audit hook.
Recorder = Callable[[str, str, str], None]


class Notifier:
    """Guarded outbound actions (alert / ticket) to real or stub destinations."""

    def __init__(
        self,
        settings: BaseSettings,
        limiter: ActionLimiter,
        recorder: Optional[Recorder] = None,
        source_label: str = "agent-core",
    ) -> None:
        self.settings = settings
        self.limiter = limiter
        self._recorder = recorder
        self.source_label = source_label
        self._run_id = "adhoc"

    def bind_run(self, run_id: str) -> None:
        """Scope guardrails/audit to the current cycle."""
        self._run_id = run_id or "adhoc"

    def _allowed(self, kind: str) -> tuple[bool, str]:
        allowed, reason = self.limiter.check(self._run_id, kind)
        self._record("ACTION_LIMITER", "allowed" if allowed else "blocked", f"{kind}: {reason}")
        return allowed, reason

    def _record(self, name: str, outcome: str, detail: str = "") -> None:
        if self._recorder is None:
            return
        try:
            self._recorder(name, outcome, detail)
        except Exception:
            pass

    # --- alert ---------------------------------------------------------------

    def send_alert(self, channel: str, title: str, message: str, severity: str = "warning") -> dict[str, Any]:
        """Send an alert. Real Slack POST when a webhook is configured, else a logged no-op.

        Returns a dict with: status ("sent" | "logged" | "suppressed" | "error"),
        channel, title, delivery ("slack" | "stub" | "guardrail"), error if any.
        """
        payload = {"channel": channel, "title": title, "message": message, "severity": severity}
        allowed, reason = self._allowed("alert")
        if not allowed:
            return {"status": "suppressed", "delivery": "guardrail", "reason": reason, **payload}

        if self.settings.slack_webhook_url:
            try:
                emoji = {"critical": ":rotating_light:", "warning": ":warning:"}.get(
                    severity, ":information_source:")
                req = urllib.request.Request(
                    self.settings.slack_webhook_url,
                    data=json.dumps({"text": f"{emoji} *[{severity.upper()}] {title}*\n{message}"}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10)  # noqa: S310 - trusted webhook
                return {"status": "sent", "delivery": "slack", **payload}
            except Exception as exc:
                return {"status": "error", "delivery": "slack", "error": str(exc), **payload}
        return {"status": "logged", "delivery": "stub", **payload}

    # --- ticket --------------------------------------------------------------

    def open_ticket(self, summary: str, description: str, priority: str = "P2", assignee: str = "") -> dict[str, Any]:
        """Open a ticket. Precedence: GitHub issue -> generic endpoint -> synthetic id.

        Returns a dict with: status ("created" | "suppressed" | "error"),
        ticket_id, url, priority, delivery, error if any.
        """
        allowed, reason = self._allowed("ticket")
        if not allowed:
            return {"status": "suppressed", "delivery": "guardrail", "reason": reason, "priority": priority}

        s = self.settings
        # 1) Real GitHub issue.
        if s.github_repo and s.github_token:
            try:
                body_md = (
                    f"{description}\n\n---\n"
                    f"*Priority:* `{priority}`  \n"
                    f"*Filed by {self.source_label}.*"
                )
                data = json.dumps({
                    "title": summary,
                    "body": body_md,
                    "labels": ["agent", "incident", priority],
                    **({"assignees": [assignee]} if assignee else {}),
                }).encode()
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{s.github_repo}/issues",
                    data=data,
                    headers={
                        "Authorization": f"Bearer {s.github_token}",
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "User-Agent": self.source_label,
                    },
                )
                resp = urllib.request.urlopen(req, timeout=15)  # noqa: S310
                out = json.loads(resp.read().decode())
                return {"status": "created", "delivery": "github",
                        "ticket_id": f"#{out.get('number')}", "url": out.get("html_url"),
                        "priority": priority}
            except Exception as exc:
                return {"status": "error", "delivery": "github", "error": str(exc), "priority": priority}

        ticket_id = f"{self.source_label.upper().replace('-', '')[:6] or 'AGENT'}-{uuid.uuid4().hex[:8].upper()}"
        # 2) Generic ticket endpoint.
        if s.ticket_endpoint:
            try:
                body = json.dumps({
                    "summary": summary, "description": description,
                    "priority": priority, "assignee": assignee,
                }).encode()
                req = urllib.request.Request(
                    s.ticket_endpoint, data=body, headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=10)  # noqa: S310
                return {"status": "created", "delivery": "endpoint", "ticket_id": ticket_id,
                        "url": f"{s.ticket_endpoint}/{ticket_id}", "priority": priority,
                        "http_status": resp.status}
            except Exception as exc:
                return {"status": "error", "delivery": "endpoint", "error": str(exc),
                        "ticket_id": ticket_id, "priority": priority}
        # 3) Safe no-op.
        return {"status": "created", "ticket_id": ticket_id,
                "url": f"https://tracker.example/{ticket_id}", "priority": priority, "delivery": "stub"}
