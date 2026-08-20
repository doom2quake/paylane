"""Domain-aware action router: send the outcome to the right place.

The core of an autonomous product's "last mile". An action is not a single
hard-coded call. The router first classifies the incident/decision DOMAIN
(deterministic keyword classifier, no model call, so routing stays auditable),
then ROUTES to one or more pluggable handlers. Each handler ships to a real
destination when configured and is a safe no-op otherwise.

Generic and app-agnostic:
  * `KeywordClassifier` — ordered (domain, regex) signals; the app supplies the
    signals for its own domains, or reuses `DEFAULT_SIGNALS`.
  * `Handler` — common interface: `name`, `can_handle(domain)`, `execute(incident)`
    returning a normalised {status, artifact_url, detail} dict; never raises for
    expected failures so the router continues to other handlers.
  * `Router` — holds a classifier + a handler registry + routing rules
    (domain -> ordered handler names), with a `{PREFIX}_ROUTING_{DOMAIN}` env
    override. Fires the routed handlers and returns a routing record for the
    audit trail: the detected domain, which handlers fired, and each artifact URL.

Ships two ready handlers built on `Notifier` (Slack alert, ticket). Apps add
their own (a GitHub-PR handler, PagerDuty, Jira, an on-chain tx, ...) with the
same interface.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .sinks import Notifier

# --- standard domain labels (apps may define their own) ----------------------

DOMAIN_FINANCE = "finance"
DOMAIN_INFRA = "infra"
DOMAIN_DATA_QUALITY = "data_quality"
DOMAIN_SECURITY = "security"
DOMAIN_UNKNOWN = "unknown"


# --- classifier --------------------------------------------------------------

# Ordered signals: the first match wins (precedence encoded by order). Apps pass
# their own list or extend this one.
DEFAULT_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    (DOMAIN_SECURITY, re.compile(
        r"\b(security|breach|unauthori[sz]ed|credential|token leak|intrusion|"
        r"exfiltrat|malicious|attack)\b", re.IGNORECASE)),
    (DOMAIN_INFRA, re.compile(
        r"\b(config|configuration|deploy|deployment|rollout|infra|infrastructure|"
        r"observability|latency|timeout|error rate|provider|routing|feature flag|"
        r"5\d\d error|outage)\b", re.IGNORECASE)),
    (DOMAIN_DATA_QUALITY, re.compile(
        r"\b(schema|null|missing value|duplicate|freshness|stale data|data quality|"
        r"pipeline|ingest|dbt|backfill)\b", re.IGNORECASE)),
    (DOMAIN_FINANCE, re.compile(
        r"\b(revenue|payment|payments|conversion|checkout|order|orders|refund|"
        r"chargeback|finance|gmv|aov|billing)\b", re.IGNORECASE)),
]

# Fields scanned for classification text.
_TEXT_FIELDS = ("summary", "correlation_summary", "description", "metric", "title", "kind")


class KeywordClassifier:
    """Deterministic domain classifier over an incident dict.

    `precedence` names domains that should win even if an earlier-ordered signal
    also matches (e.g. a revenue collapse is a finance incident even when its
    *cause* is a config change).
    """

    def __init__(
        self,
        signals: Sequence[tuple[str, re.Pattern[str]]] = tuple(DEFAULT_SIGNALS),
        precedence: Sequence[str] = (DOMAIN_FINANCE,),
        default: str = DOMAIN_UNKNOWN,
    ) -> None:
        self.signals = list(signals)
        self.precedence = list(precedence)
        self.default = default

    def _haystack(self, incident: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in _TEXT_FIELDS:
            val = incident.get(key)
            if isinstance(val, str):
                parts.append(val)
        nested = incident.get("anomaly")
        if isinstance(nested, dict):
            parts.extend(v for v in nested.values() if isinstance(v, str))
        return " ".join(parts)

    def classify(self, incident: dict[str, Any]) -> str:
        haystack = self._haystack(incident)
        by_domain = {d: p for d, p in self.signals}
        for d in self.precedence:
            pat = by_domain.get(d)
            if pat is not None and pat.search(haystack):
                return d
        for domain, pattern in self.signals:
            if pattern.search(haystack):
                return domain
        return self.default


# --- handler interface -------------------------------------------------------

class Handler:
    """Common interface for a routing destination.

    Subclasses set `name`, decide `can_handle(domain)`, and `execute(incident)` a
    real action, returning a normalised result dict:
        {"status": ..., "artifact_url": ..., "detail": {...}, "handler": name}
    Status is one of: fired, dry_run, disabled, suppressed, noop, error. `execute`
    must never raise for expected failures.
    """

    name: str = "handler"

    def can_handle(self, domain: str) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def execute(self, incident: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class AlertHandler(Handler):
    """Posts an alert via a `Notifier` (real Slack when configured). Fires for the
    domains in `domains` (default: finance)."""

    name = "alert"

    def __init__(self, notifier: Notifier, domains: Sequence[str] = (DOMAIN_FINANCE,),
                 channel: str = "#ops") -> None:
        self.notifier = notifier
        self.domains = set(domains)
        self.channel = channel

    def can_handle(self, domain: str) -> bool:
        return domain in self.domains

    def execute(self, incident: dict[str, Any]) -> dict[str, Any]:
        title = incident.get("title") or f"[{incident.get('metric') or 'signal'}] incident"
        message = str(incident.get("summary") or incident.get("correlation_summary")
                      or incident.get("description") or "Anomaly detected.")
        if incident.get("run_id"):
            message += f"\nRun: {incident['run_id']}"
        r = self.notifier.send_alert(self.channel, title, message, severity="critical")
        status_map = {"sent": "fired", "logged": "noop", "suppressed": "suppressed", "error": "error"}
        return {"status": status_map.get(r.get("status", "error"), r.get("status", "error")),
                "handler": self.name, "artifact_url": r.get("url"),
                "detail": {"delivery": r.get("delivery"), "channel": self.channel,
                           "reason": r.get("reason"), "error": r.get("error")}}


class TicketHandler(Handler):
    """Opens a ticket via a `Notifier` (GitHub issue / endpoint / stub). The generic
    fallback — fires for every domain so an incident always leaves a paper trail."""

    name = "ticket"

    def __init__(self, notifier: Notifier, priority: str = "P1") -> None:
        self.notifier = notifier
        self.priority = priority

    def can_handle(self, domain: str) -> bool:
        return True

    def execute(self, incident: dict[str, Any]) -> dict[str, Any]:
        summary = incident.get("title") or "Incident requires attention"
        description = str(incident.get("summary") or incident.get("correlation_summary")
                          or incident.get("description") or "Anomaly detected.")
        r = self.notifier.open_ticket(summary=summary, description=description, priority=self.priority)
        status_map = {"created": "fired", "logged": "noop", "suppressed": "suppressed", "error": "error"}
        return {"status": status_map.get(r.get("status", "error"), r.get("status", "error")),
                "handler": self.name, "artifact_url": r.get("url"),
                "detail": {"ticket_id": r.get("ticket_id"), "delivery": r.get("delivery"),
                           "reason": r.get("reason"), "error": r.get("error")}}


# --- routing rules -----------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """Ordered list of handler names to fire for a domain."""

    handlers: tuple[str, ...]


DEFAULT_ROUTING: dict[str, Route] = {
    DOMAIN_FINANCE: Route(("alert", "ticket")),
    DOMAIN_INFRA: Route(("ticket",)),
    DOMAIN_DATA_QUALITY: Route(("ticket",)),
    DOMAIN_SECURITY: Route(("alert", "ticket")),
    DOMAIN_UNKNOWN: Route(("ticket",)),
}


# --- the router --------------------------------------------------------------

class Router:
    """Classify an incident, fire the routed handlers, return a routing record."""

    def __init__(
        self,
        handlers: Sequence[Handler],
        classifier: Optional[KeywordClassifier] = None,
        routing: Optional[dict[str, Route]] = None,
        env_prefix: str = "AGENT",
    ) -> None:
        self.registry = {h.name: h for h in handlers}
        self.classifier = classifier or KeywordClassifier()
        self.routing = routing or dict(DEFAULT_ROUTING)
        self.env_prefix = env_prefix

    def _route_for(self, domain: str) -> Route:
        # {PREFIX}_ROUTING_{DOMAIN}=alert,ticket overrides the default handler list.
        raw = os.getenv(f"{self.env_prefix}_ROUTING_{domain.upper()}")
        if raw:
            names = tuple(n.strip() for n in raw.split(",") if n.strip())
            if names:
                return Route(names)
        return self.routing.get(domain, self.routing.get(DOMAIN_UNKNOWN, Route(("ticket",))))

    def route(self, incident: dict[str, Any]) -> dict[str, Any]:
        """Classify + fire. Returns {domain, route, handlers, artifacts,
        primary_artifact_url}. Never raises for expected handler failures."""
        domain = self.classifier.classify(incident)
        route = self._route_for(domain)

        fired: list[dict[str, Any]] = []
        artifacts: dict[str, Any] = {}
        primary: Optional[str] = None

        for name in route.handlers:
            handler = self.registry.get(name)
            if handler is None:
                fired.append({"handler": name, "status": "error",
                              "detail": {"error": f"unknown handler {name!r}"}})
                continue
            if not handler.can_handle(domain):
                continue
            try:
                result = handler.execute(incident)
            except Exception as exc:  # never let one handler break the route
                result = {"handler": name, "status": "error",
                          "detail": {"error": f"{exc.__class__.__name__}: {exc}"}}
            fired.append(result)

            url = result.get("artifact_url")
            if url:
                artifacts[name] = url
            det = result.get("detail") or {}
            if det.get("ticket_id"):
                artifacts.setdefault("ticket_id", det["ticket_id"])
            if result.get("status") in ("fired", "dry_run") and primary is None:
                primary = url or det.get("ticket_id")

        return {"domain": domain, "route": list(route.handlers), "handlers": fired,
                "artifacts": artifacts, "primary_artifact_url": primary}
