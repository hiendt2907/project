from __future__ import annotations

import os
import pathlib
import socket


def _default_agent_id() -> str:
    return os.getenv("OMNI_AGENT_ID") or socket.gethostname()


def _read_version() -> str:
    # Check env override first
    if v := os.getenv("OMNI_AGENT_VERSION", "").strip():
        return v
    # Read VERSION file next to this package
    version_file = pathlib.Path(__file__).parent / "VERSION"
    if version_file.exists():
        v = version_file.read_text().strip()
        if v:
            return v
    return "1.0.0"


class AgentSettings:
    gateway_url: str
    api_key: str
    agent_id: str
    hostname: str
    collect_interval: int
    log_paths: list[str]
    k8s_enabled: bool
    k8s_namespace: str
    version: str

    def __init__(self) -> None:
        self.version = _read_version()
        self.gateway_url = os.getenv("OMNI_AGENT_GATEWAY_URL", "http://omni-gateway:8080").rstrip("/")
        self.api_key = os.getenv("OMNI_AGENT_API_KEY", "")
        self.hostname = os.getenv("OMNI_AGENT_HOSTNAME") or socket.gethostname()
        self.agent_id = os.getenv("OMNI_AGENT_ID") or self.hostname
        self.collect_interval = int(os.getenv("OMNI_AGENT_COLLECT_INTERVAL", "60"))
        raw_paths = os.getenv("OMNI_AGENT_LOG_PATHS", "/var/log/syslog")
        self.log_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
        self.k8s_enabled = os.getenv("OMNI_AGENT_K8S_ENABLED", "true").lower() not in ("false", "0", "no")
        self.k8s_namespace = os.getenv("OMNI_AGENT_NAMESPACE", "")
        # Domain collectors — opt-in via env (default disabled to avoid noise on non-DB hosts)
        self.database_enabled = os.getenv("OMNI_AGENT_DATABASE_ENABLED", "false").lower() not in ("false", "0", "no")
        self.mysql_host = os.getenv("OMNI_AGENT_MYSQL_HOST", "127.0.0.1")
        self.mysql_port = int(os.getenv("OMNI_AGENT_MYSQL_PORT", "3306"))
        self.mysql_user = os.getenv("OMNI_AGENT_MYSQL_USER", "")
        self.mysql_pass = os.getenv("OMNI_AGENT_MYSQL_PASS", "")
        self.proxysql_enabled = os.getenv("OMNI_AGENT_PROXYSQL_ENABLED", "false").lower() not in ("false", "0", "no")
        self.proxysql_host = os.getenv("OMNI_AGENT_PROXYSQL_HOST", "127.0.0.1")
        self.proxysql_admin_user = os.getenv("OMNI_AGENT_PROXYSQL_ADMIN_USER", "radmin")
        self.proxysql_admin_pass = os.getenv("OMNI_AGENT_PROXYSQL_ADMIN_PASS", "")
        self.services_enabled = os.getenv("OMNI_AGENT_SERVICES_ENABLED", "false").lower() not in ("false", "0", "no")
        self.storage_enabled = os.getenv("OMNI_AGENT_STORAGE_ENABLED", "false").lower() not in ("false", "0", "no")

    def validate(self) -> None:
        if not self.gateway_url:
            raise ValueError("OMNI_AGENT_GATEWAY_URL is required")
        if not self.api_key:
            raise ValueError("OMNI_AGENT_API_KEY is required")
