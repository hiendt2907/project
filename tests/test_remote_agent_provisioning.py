"""Coverage for scripts/lib/remote_agent_provisioning.py — canonical run.env
rendering. Guards against the cust-app gap: OMNI_REMOTE_DISCOVERY_ENABLED
silently defaulting off because provisioning never wrote it."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from remote_agent_provisioning import (  # noqa: E402
    AgentProvisioningSpec,
    effective_config_summary,
    is_idempotent_rewrite,
    render_run_env,
)


def _spec(**overrides) -> AgentProvisioningSpec:
    base = dict(
        tenant_id="tenant-replay-01",
        agent_id="tenant-replay-01_cust-app",
        hostname="cust-app",
        gateway_url="http://gateway.ai-agent.local",
    )
    base.update(overrides)
    return AgentProvisioningSpec(**base)


def test_discovery_enabled_by_default() -> None:
    spec = _spec()
    assert spec.discovery_enabled is True
    env = render_run_env(spec, api_key="k")
    assert "OMNI_REMOTE_DISCOVERY_ENABLED=true" in env


def test_explicit_false_override_supported() -> None:
    spec = _spec(discovery_enabled=False)
    env = render_run_env(spec, api_key="k")
    assert "OMNI_REMOTE_DISCOVERY_ENABLED=false" in env


def test_render_includes_tenant_and_identity() -> None:
    spec = _spec()
    env = render_run_env(spec, api_key="secret-key")
    assert "OMNI_AGENT_TENANT_ID=tenant-replay-01" in env
    assert "OMNI_AGENT_ID=tenant-replay-01_cust-app" in env
    assert "OMNI_AGENT_HOSTNAME=cust-app" in env
    assert "OMNI_AGENT_API_KEY=secret-key" in env


def test_rerender_with_same_spec_is_idempotent() -> None:
    spec = _spec()
    first = render_run_env(spec, api_key="k")
    second = render_run_env(spec, api_key="k")
    assert is_idempotent_rewrite(first, second)


def test_rerender_after_identity_change_is_not_idempotent() -> None:
    spec = _spec()
    first = render_run_env(spec, api_key="k")
    changed = render_run_env(_spec(agent_id="different-agent"), api_key="k")
    assert not is_idempotent_rewrite(first, changed)


def test_effective_config_summary_has_no_secrets() -> None:
    summary = effective_config_summary(_spec())
    assert "api_key" not in summary
    assert "mysql_pass" not in summary
    assert summary["discovery_enabled"] == "true"
    assert summary["tenant_id"] == "tenant-replay-01"


def test_database_fields_only_rendered_when_enabled() -> None:
    off = render_run_env(_spec(database_enabled=False), api_key="k")
    assert "OMNI_AGENT_DATABASE_ENABLED" not in off
    on = render_run_env(_spec(database_enabled=True, mysql_user="radmin"), api_key="k", mysql_pass="pw")
    assert "OMNI_AGENT_DATABASE_ENABLED=true" in on
    assert "OMNI_AGENT_MYSQL_USER=radmin" in on
    assert "OMNI_AGENT_MYSQL_PASS=pw" in on
