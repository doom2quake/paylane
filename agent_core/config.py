"""Base configuration for an agent-core app, loaded from environment variables.

No secrets live in code. On Google Cloud these come from the runtime env (Cloud
Run env vars / Secret Manager); locally from a `.env` file. Credential *values*
are never read here — auth is Application Default Credentials (ADC) on GCP.

Apps subclass `BaseSettings` to add their own domain fields, e.g.::

    @dataclass(frozen=True)
    class AtlasSettings(BaseSettings):
        env_prefix: str = "ATLAS"
        bq_dataset: str = field(default_factory=lambda: env_str("ATLAS_BQ_DATASET", "atlas_demo"))

    settings = AtlasSettings()

The `env_prefix` lets an app namespace its guardrail/routing env vars (e.g.
`ATLAS_DRY_RUN`) while sharing the same code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    # Optional: load a local .env for dev. No-op if python-dotenv is absent.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a dev convenience only
    pass


# --- env helpers (public — apps use these in their own field defaults) --------

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_str(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return default if val is None else val


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class BaseSettings:
    """Resolved runtime settings shared by every agent-core app.

    Instantiate once (module-level `settings = MySettings()`). Frozen so config
    is read-only after load.
    """

    # Namespace for this app's env vars (guardrails/routing read `{env_prefix}_*`).
    env_prefix: str = "AGENT"

    # --- Vertex AI / Gemini ---------------------------------------------------
    # ADK reads GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_PROJECT /
    # GOOGLE_CLOUD_LOCATION directly; surfaced here for logging + sanity checks.
    use_vertexai: bool = field(default_factory=lambda: env_bool("GOOGLE_GENAI_USE_VERTEXAI", True))
    project: str = field(default_factory=lambda: env_str("GOOGLE_CLOUD_PROJECT"))
    location: str = field(default_factory=lambda: env_str("GOOGLE_CLOUD_LOCATION", "us-central1"))

    # Cost-aware model tiers: Flash for routine reasoning, a deeper tier for
    # correlation/judgement. Override via AGENT_MODEL_FAST / AGENT_MODEL_DEEP
    # (or the app's own prefix if it overrides these fields).
    model_fast: str = field(default_factory=lambda: env_str("AGENT_MODEL_FAST", "gemini-3.5-flash"))
    model_deep: str = field(default_factory=lambda: env_str("AGENT_MODEL_DEEP", "gemini-3.6-flash"))

    # --- Durable state --------------------------------------------------------
    firestore_collection: str = field(
        default_factory=lambda: env_str("AGENT_FIRESTORE_COLLECTION", "agent_runs")
    )
    # When true (or when firestore/credentials are unavailable), state falls back
    # to an in-memory store for local dev.
    use_in_memory_state: bool = field(default_factory=lambda: env_bool("AGENT_IN_MEMORY_STATE", False))

    # --- Notify / ticket sinks (real when configured; safe no-op otherwise) ---
    slack_webhook_url: str = field(default_factory=lambda: env_str("AGENT_SLACK_WEBHOOK_URL"))
    ticket_endpoint: str = field(default_factory=lambda: env_str("AGENT_TICKET_ENDPOINT"))
    # GitHub issue/PR integration: "owner/repo" + a token (repo or issues scope).
    github_repo: str = field(default_factory=lambda: env_str("AGENT_GITHUB_REPO"))
    github_token: str = field(
        default_factory=lambda: env_str("AGENT_GITHUB_TOKEN") or env_str("GITHUB_TOKEN")
    )

    app_name: str = "agent"

    def __post_init__(self) -> None:
        """Let an app's `env_prefix` override the operational toggles.

        The field defaults read shared `AGENT_*` env names; if the app-namespaced
        `{env_prefix}_*` variant is set, it wins (e.g. `PP_IN_MEMORY_STATE`,
        `PP_SLACK_WEBHOOK_URL`). Uses object.__setattr__ since the dataclass is
        frozen.
        """
        p = self.env_prefix
        if not p or p == "AGENT":
            return
        str_fields = ("slack_webhook_url", "ticket_endpoint", "github_repo",
                      "github_token", "firestore_collection", "model_fast", "model_deep")
        for name in str_fields:
            raw = os.getenv(f"{p}_{name.upper()}")
            if raw is not None and raw != "":
                object.__setattr__(self, name, raw)
        raw = os.getenv(f"{p}_IN_MEMORY_STATE")
        if raw is not None and raw != "":
            object.__setattr__(self, "use_in_memory_state",
                               raw.strip().lower() in {"1", "true", "yes", "on"})

    def missing_for_gcp(self) -> list[str]:
        """Settings that must be provided before the app can hit GCP."""
        missing = []
        if not self.project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        return missing
