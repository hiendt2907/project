"""Omni Remote Agent — main async loop.

Usage:
    python -m remote_agent.agent

Env vars:
    OMNI_AGENT_GATEWAY_URL      (required)
    OMNI_AGENT_API_KEY          (required)
    OMNI_AGENT_ID               (default: hostname)
    OMNI_AGENT_COLLECT_INTERVAL (default: 60)
    OMNI_AGENT_LOG_PATHS        (default: /var/log/syslog)
    OMNI_AGENT_K8S_ENABLED      (default: true)
    OMNI_AGENT_NAMESPACE        (default: "")
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("omni-agent")

# Re-register interval: must be < gateway TTL (120s)
_REGISTER_INTERVAL = 30
# Command-channel poll cadence — decoupled from collect_interval so diagnostic
# commands are picked up within seconds (diagnosis loop waits up to 90s).
_CMD_POLL_INTERVAL = 5


async def run_agent() -> None:
    from remote_agent.settings import AgentSettings
    from remote_agent.emitter import OmniEmitter
    from remote_agent.discovery import run_vm_discovery, derive_enabled_collectors
    from remote_agent.command_executor import execute_batch
    from remote_agent.collectors.system import collect_system_metrics
    from remote_agent.collectors.logs import collect_log_errors
    from remote_agent.collectors.k8s import collect_k8s_status
    from remote_agent.collectors.database import collect_mysql_health, collect_proxysql_stats
    from remote_agent.collectors.services import collect_haproxy_stats, collect_systemd_units
    from remote_agent.collectors.storage import collect_disk_usage, collect_nfs_health

    cfg = AgentSettings()
    cfg.validate()

    # ── Phase A: VM auto-discovery ────────────────────────────────────────────
    # Run discovery on startup; derive collector config from what's actually running.
    logger.info("omni-agent: running VM discovery ...")
    try:
        profile = await run_vm_discovery(cfg.agent_id, cfg.hostname)
        derived = derive_enabled_collectors(profile)
        # Override settings with discovered state (env var still wins if explicitly set)
        if not cfg.database_enabled and derived.get("database_enabled"):
            cfg.database_enabled = True
            logger.info("omni-agent: database_enabled=True (discovered mysql/mariadb)")
        if not cfg.proxysql_enabled and derived.get("proxysql_enabled"):
            cfg.proxysql_enabled = True
            logger.info("omni-agent: proxysql_enabled=True (discovered proxysql)")
        if not cfg.services_enabled and derived.get("services_enabled"):
            cfg.services_enabled = True
            logger.info("omni-agent: services_enabled=True (discovered haproxy/nginx)")
        if not cfg.k8s_enabled and derived.get("k8s_enabled"):
            cfg.k8s_enabled = True
            logger.info("omni-agent: k8s_enabled=True (discovered kubelet)")
        # Merge discovered log paths into collection list
        discovered_logs: list[str] = profile.get("log_paths", [])
        if discovered_logs:
            existing = set(cfg.log_paths)
            added = [p for p in discovered_logs if p not in existing]
            if added:
                cfg.log_paths = list(cfg.log_paths) + added
                logger.info(
                    "omni-agent: log_paths expanded %d → %d (added %d from discovery)",
                    len(existing),
                    len(cfg.log_paths),
                    len(added),
                )
        # Store discovered service names for fine-grained collector gating
        discovered_service_names: frozenset[str] = frozenset(
            s["name"].lower() for s in profile.get("services", [])
        )
    except Exception as exc:
        profile = {}
        discovered_service_names = frozenset()
        logger.warning("omni-agent: VM discovery failed (non-fatal): %s", exc)

    capabilities = ["metrics", "logs", "discovery"]
    if cfg.k8s_enabled:
        capabilities.append("k8s")
    if cfg.database_enabled or cfg.proxysql_enabled:
        capabilities.append("database")
    if cfg.services_enabled:
        capabilities.append("services")
    if cfg.storage_enabled:
        capabilities.append("storage")

    emitter = OmniEmitter(
        gateway_url=cfg.gateway_url,
        api_key=cfg.api_key,
        agent_id=cfg.agent_id,
        hostname=cfg.hostname,
    )

    logger.info(
        "omni-agent starting: id=%s hostname=%s gateway=%s interval=%ds caps=%s",
        cfg.agent_id,
        cfg.hostname,
        cfg.gateway_url,
        cfg.collect_interval,
        capabilities,
    )

    import time

    # Initial registration + profile upload
    await emitter.register(capabilities, version=cfg.version, k8s_namespace=cfg.k8s_namespace)
    if profile:
        await emitter.upload_profile(profile)
    last_register_ts = time.monotonic()
    last_discovery_ts = time.monotonic()

    _DISCOVERY_INTERVAL = 86400  # re-scan every 24h

    while True:
        # Re-register every _REGISTER_INTERVAL seconds (must be < gateway TTL 120s)
        if time.monotonic() - last_register_ts >= _REGISTER_INTERVAL:
            await emitter.register(capabilities, version=cfg.version, k8s_namespace=cfg.k8s_namespace)
            last_register_ts = time.monotonic()

        # Re-run VM discovery every 24h to pick up newly installed services
        if time.monotonic() - last_discovery_ts >= _DISCOVERY_INTERVAL:
            try:
                profile = await run_vm_discovery(cfg.agent_id, cfg.hostname)
                await emitter.upload_profile(profile)
                last_discovery_ts = time.monotonic()
            except Exception as exc:
                logger.warning("omni-agent: periodic discovery failed: %s", exc)

        evidence: list[dict] = []

        # Lane 1: system metrics
        sys_ev = await collect_system_metrics(cfg.hostname)
        if sys_ev:
            evidence.append(sys_ev)

        # Lane 2: log errors
        log_evs = await collect_log_errors(cfg.log_paths, cfg.hostname)
        evidence.extend(log_evs)

        # Lane 3: K8s status (optional)
        if cfg.k8s_enabled:
            k8s_evs = await collect_k8s_status(cfg.k8s_namespace, cfg.hostname)
            evidence.extend(k8s_evs)

        # Lane 4: Database health
        if cfg.database_enabled:
            mysql_ev = await collect_mysql_health(
                cfg.hostname,
                mysql_host=cfg.mysql_host,
                mysql_port=cfg.mysql_port,
                mysql_user=cfg.mysql_user,
                mysql_pass=cfg.mysql_pass,
            )
            if mysql_ev:
                evidence.append(mysql_ev)

        if cfg.proxysql_enabled:
            psql_ev = await collect_proxysql_stats(
                cfg.hostname,
                proxysql_host=cfg.proxysql_host,
                proxysql_admin_user=cfg.proxysql_admin_user,
                proxysql_admin_pass=cfg.proxysql_admin_pass,
            )
            if psql_ev:
                evidence.append(psql_ev)

        # Lane 5: Services health (opt-in via OMNI_AGENT_SERVICES_ENABLED)
        if cfg.services_enabled:
            # Only probe haproxy if it was actually discovered on this VM
            if any("haproxy" in n for n in discovered_service_names):
                haproxy_ev = await collect_haproxy_stats(cfg.hostname)
                if haproxy_ev:
                    evidence.append(haproxy_ev)
            # Pass discovered services so criticality is per-VM, not hardcoded
            systemd_ev = await collect_systemd_units(cfg.hostname, critical_services=discovered_service_names)
            if systemd_ev:
                evidence.append(systemd_ev)

        # Lane 6: Storage health (opt-in via OMNI_AGENT_STORAGE_ENABLED)
        if cfg.storage_enabled:
            disk_ev = await collect_disk_usage(cfg.hostname)
            if disk_ev:
                evidence.append(disk_ev)
            nfs_ev = await collect_nfs_health(cfg.hostname)
            if nfs_ev:
                evidence.append(nfs_ev)

        if evidence:
            await emitter.emit(evidence)

        # ── Phase B: Command channel poll ─────────────────────────────────────
        # Poll FAST (every _CMD_POLL_INTERVAL) for the whole collect_interval so
        # diagnostic commands are executed within seconds, not once per minute.
        # The diagnosis loop only waits ~90s — a 60s poll cadence races it.
        poll_deadline = time.monotonic() + cfg.collect_interval
        while True:
            try:
                commands = await emitter.poll_commands()
                if commands:
                    logger.info(
                        "omni-agent: received %d command(s) from Omni", len(commands)
                    )
                    results = await execute_batch(commands)
                    await emitter.submit_command_results(results)
            except Exception as exc:
                logger.warning("omni-agent: command channel error (non-fatal): %s", exc)

            remaining = poll_deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_CMD_POLL_INTERVAL, remaining))


def _handle_shutdown(sig: int, loop: asyncio.AbstractEventLoop) -> None:
    logger.info("omni-agent received signal %d — shutting down", sig)
    loop.stop()


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_shutdown, sig, loop)

    try:
        loop.run_until_complete(run_agent())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.close()
        logger.info("omni-agent stopped")


if __name__ == "__main__":
    main()
