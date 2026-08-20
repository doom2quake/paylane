"""Durable state / memory for agent runs.

Backed by Firestore in production, with a thin in-memory fallback so an app is
runnable locally without credentials. A *run* is one autonomous cycle; findings,
actions, and guardrail decisions are appended to the run document so the whole
causal chain is auditable.

Core run schema (app-specific fields go under `data`):
    {
      "run_id":     str,
      "status":     str,            # app-defined lifecycle label
      "started_at": ISO-8601 str,
      "updated_at": ISO-8601 str,
      "trigger":    dict,           # what kicked off the run
      "guardrails": list[dict],     # named guardrail decisions (audit trail)
      "signature":  str | None,     # stable key for recurrence detection
      "recurrence": dict | None,    # {count, window_days, last_seen, prior_run_ids}
      "error":      str | None,
      "data":       dict,           # free-form app payload (findings, impact, ...)
    }

`StateStore.create(settings)` picks Firestore when `google-cloud-firestore` +
ADC are available, else in-memory. The two-argument constructor is DI-friendly
for tests.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import threading
import uuid
from typing import Any, Optional

from .config import BaseSettings


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_run_id() -> str:
    return f"run-{_dt.datetime.now(_dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def signature_of(*parts: Any) -> str:
    """Stable short signature for recurrence detection (e.g. metric+region+direction)."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class _InMemoryBackend:
    """Process-local dict store. Local dev / tests only."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def set(self, run_id: str, doc: dict[str, Any]) -> None:
        with self._lock:
            self._runs[run_id] = dict(doc)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            doc = self._runs.get(run_id)
            return dict(doc) if doc else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            docs = sorted(self._runs.values(), key=lambda d: d.get("started_at", ""), reverse=True)
            return [dict(d) for d in docs[:limit]]

    def find_by_signature(self, signature: str, exclude_run_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            out = [dict(d) for d in self._runs.values()
                   if d.get("signature") == signature and d.get("run_id") != exclude_run_id]
            return sorted(out, key=lambda d: d.get("started_at", ""), reverse=True)


class _FirestoreBackend:
    """Firestore-backed store. Requires google-cloud-firestore + ADC."""

    def __init__(self, project: str, collection: str) -> None:
        from google.cloud import firestore  # lazy import so local dev needs no dep

        self._client = firestore.Client(project=project) if project else firestore.Client()
        self._collection = collection

    def _col(self):
        return self._client.collection(self._collection)

    def set(self, run_id: str, doc: dict[str, Any]) -> None:
        self._col().document(run_id).set(doc)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        snap = self._col().document(run_id).get()
        return snap.to_dict() if snap.exists else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        docs = self._col().order_by("started_at", direction="DESCENDING").limit(limit).stream()
        return [d.to_dict() for d in docs]

    def find_by_signature(self, signature: str, exclude_run_id: str = "") -> list[dict[str, Any]]:
        # No order_by: an equality-only query needs no composite index, so this
        # works on a fresh Firestore project. Sort in Python instead.
        docs = self._col().where("signature", "==", signature).limit(50).stream()
        out = [d.to_dict() for d in docs if d.to_dict().get("run_id") != exclude_run_id]
        return sorted(out, key=lambda d: d.get("started_at", ""), reverse=True)


class StateStore:
    """High-level run API over whichever backend is available."""

    def __init__(self, backend: Any, backend_name: str) -> None:
        self._backend = backend
        self.backend_name = backend_name

    @classmethod
    def create(cls, settings: BaseSettings) -> "StateStore":
        """Pick Firestore if possible, else fall back to in-memory."""
        if settings.use_in_memory_state:
            return cls(_InMemoryBackend(), "in-memory (forced)")
        try:
            backend = _FirestoreBackend(settings.project, settings.firestore_collection)
            _ = backend._col()  # touch client so misconfig surfaces now, not mid-run
            return cls(backend, f"firestore:{settings.firestore_collection}")
        except Exception as exc:  # pragma: no cover - depends on live GCP
            return cls(_InMemoryBackend(), f"in-memory (Firestore unavailable: {exc.__class__.__name__})")

    # --- run lifecycle -------------------------------------------------------

    def start_run(self, trigger: Optional[dict[str, Any]] = None, status: str = "started") -> str:
        run_id = new_run_id()
        now = _now()
        self._backend.set(run_id, {
            "run_id": run_id, "status": status, "started_at": now, "updated_at": now,
            "trigger": trigger or {}, "guardrails": [], "signature": None,
            "recurrence": None, "error": None, "data": {},
        })
        return run_id

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        return self._backend.get(run_id)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._backend.list(limit)

    def set_status(self, run_id: str, status: str, **fields: Any) -> None:
        self.update(run_id, status=status, **fields)

    def update(self, run_id: str, **fields: Any) -> None:
        doc = self._backend.get(run_id)
        if doc is None:
            return
        doc.update(fields)
        doc["updated_at"] = _now()
        self._backend.set(run_id, doc)

    def set_data(self, run_id: str, key: str, value: Any) -> None:
        """Set an app-specific field under the run's `data` payload."""
        doc = self._backend.get(run_id)
        if doc is None:
            return
        data = dict(doc.get("data") or {})
        data[key] = value
        doc["data"] = data
        doc["updated_at"] = _now()
        self._backend.set(run_id, doc)

    def append(self, run_id: str, key: str, item: Any) -> None:
        """Append to a top-level list field (creates it if absent)."""
        doc = self._backend.get(run_id)
        if doc is None:
            return
        lst = list(doc.get(key) or [])
        lst.append(item)
        doc[key] = lst
        doc["updated_at"] = _now()
        self._backend.set(run_id, doc)

    def record_guardrail(self, run_id: str, name: str, outcome: str, detail: str = "") -> None:
        """Append a named guardrail decision to the run's audit trail.

        Signature matches `agent_core.sinks.Recorder` so a `Notifier` can be wired
        to a run: `Notifier(..., recorder=lambda n, o, d: store.record_guardrail(rid, n, o, d))`.
        """
        self.append(run_id, "guardrails", {"name": name, "outcome": outcome, "detail": detail, "at": _now()})

    def fail(self, run_id: str, error: str) -> None:
        self.update(run_id, status="error", error=error)

    # --- recurrence memory ---------------------------------------------------

    def detect_recurrence(self, run_id: str, signature: str, window_days: int = 7) -> Optional[dict[str, Any]]:
        """Record `signature` on the run and, if the same signature was seen within
        `window_days`, return a recurrence record (raises severity for the app)."""
        try:
            prior = self._backend.find_by_signature(signature, exclude_run_id=run_id)
        except Exception:
            # Recurrence memory is best-effort; never let it break a run.
            self.update(run_id, signature=signature, recurrence=None)
            return None
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
        recent = []
        for d in prior:
            ts = d.get("started_at", "")
            try:
                when = _dt.datetime.fromisoformat(ts)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
            except Exception:
                continue
            if when >= cutoff:
                recent.append(d)
        recurrence = None
        if recent:
            recurrence = {
                "count": len(recent) + 1,
                "window_days": window_days,
                "last_seen": recent[0].get("started_at"),
                "prior_run_ids": [d.get("run_id") for d in recent][:10],
            }
        self.update(run_id, signature=signature, recurrence=recurrence)
        return recurrence
