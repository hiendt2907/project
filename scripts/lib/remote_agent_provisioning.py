"""Canonical source of truth for remote-agent `run.env` provisioning.

Extracted from the inline f-string in scripts/e2e_onboarding_full_flow.py
(TC-OB02) so every provisioning caller — onboarding E2E, fleet scripts,
future fresh-tenant flows — renders the same env and can never again forget
OMNI_REMOTE_DISCOVERY_ENABLED (see docs/post-mortems for the cust-app gap
this caused). Pure functions only: no orb/subprocess/network calls, so this
module is trivially unit-testable and safe to import from any script.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentProvisioningSpec:
    """Non-secret identity + policy inputs for one remote-agent install."""

    tenant_id: str
    agent_id: str
    hostname: str
    gateway_url: str
    collect_interval: int = 20
    discovery_enabled: bool = True
    k8s_enabled: bool = False
    database_enabled: bool = False
    mysql_host: str = "127.0.0.1"
    mysql_user: str = ""
    storage_enabled: bool = False
    doc_search_dirs: tuple[str, ...] = ("/etc", "/opt", "/srv")
    log_paths: tuple[str, ...] = ("/var/log/syslog",)
    extra_env: dict[str, str] = field(default_factory=dict)


def render_run_env(spec: AgentProvisioningSpec, *, api_key: str, mysql_pass: str = "") -> str:
    """Render `run.env` content. Secrets (api_key/mysql_pass) are kept out of
    ``AgentProvisioningSpec`` so the spec itself is safe to log."""
    lines = [
        f"OMNI_AGENT_GATEWAY_URL={spec.gateway_url}",
        f"OMNI_AGENT_API_KEY={api_key}",
        f"OMNI_AGENT_ID={spec.agent_id}",
        f"OMNI_AGENT_HOSTNAME={spec.hostname}",
        f"OMNI_AGENT_TENANT_ID={spec.tenant_id}",
        f"OMNI_AGENT_K8S_ENABLED={'true' if spec.k8s_enabled else 'false'}",
        f"OMNI_AGENT_COLLECT_INTERVAL={spec.collect_interval}",
        f"OMNI_REMOTE_DISCOVERY_ENABLED={'true' if spec.discovery_enabled else 'false'}",
    ]
    if spec.database_enabled:
        lines += [
            "OMNI_AGENT_DATABASE_ENABLED=true",
            f"OMNI_AGENT_MYSQL_HOST={spec.mysql_host}",
            f"OMNI_AGENT_MYSQL_USER={spec.mysql_user}",
            f"OMNI_AGENT_MYSQL_PASS={mysql_pass}",
        ]
    if spec.storage_enabled:
        lines.append("OMNI_AGENT_STORAGE_ENABLED=true")
    lines.append(f"OMNI_AGENT_DOC_SEARCH_DIRS={','.join(spec.doc_search_dirs)}")
    lines.append(f"OMNI_AGENT_LOG_PATHS={','.join(spec.log_paths)}")
    for k, v in sorted(spec.extra_env.items()):
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def effective_config_summary(spec: AgentProvisioningSpec) -> dict[str, str]:
    """Non-secret config to log at agent startup so drift (like the cust-app
    gap where discovery silently stayed off) is visible immediately instead
    of discovered later via a data hole in the System Twin."""
    return {
        "tenant_id": spec.tenant_id,
        "agent_id": spec.agent_id,
        "hostname": spec.hostname,
        "discovery_enabled": str(spec.discovery_enabled).lower(),
        "k8s_enabled": str(spec.k8s_enabled).lower(),
        "database_enabled": str(spec.database_enabled).lower(),
        "storage_enabled": str(spec.storage_enabled).lower(),
        "collect_interval": str(spec.collect_interval),
    }


def is_idempotent_rewrite(existing_content: str, new_content: str) -> bool:
    """True if writing ``new_content`` over ``existing_content`` is a no-op
    (same rendered env) — callers should skip the write + restart entirely
    in that case so repeated provisioning runs don't needlessly bounce the
    agent or duplicate log lines."""
    return existing_content.strip() == new_content.strip()
