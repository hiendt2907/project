#!/usr/bin/env python3
"""Omni Agent Provisioner Daemon — chạy trên Mac, expose HTTP API để UI tự động cài agent.

Start: python scripts/omni-provisioner.py
LaunchAgent: ~/Library/LaunchAgents/com.omni.provisioner.plist
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import asyncssh
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

_env_file = Path.home() / ".omni-provisioner.env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SSH_KEY = Path(os.environ.get("SSH_KEY_PATH", "~/Downloads/loyalty-uat-ssh-key.pem")).expanduser()
SSH_USER = os.environ.get("SSH_USER", "root")
VENV_SOURCE_HOST = os.environ.get("VENV_SOURCE_HOST", "10.210.14.86")
BUNDLE_PATH = Path(os.environ.get("BUNDLE_PATH", "~/project/dist/omni-agent-1.0.0.tar.gz")).expanduser()
_gw_key = os.environ.get("GATEWAY_API_KEY")
if not _gw_key:
    raise RuntimeError("GATEWAY_API_KEY env var is required — set it in ~/.omni-provisioner.env")
GATEWAY_API_KEY: str = _gw_key
GATEWAY_URL_REMOTE = os.environ.get("GATEWAY_URL_REMOTE", "http://127.0.0.1:8899")
PORT = int(os.environ.get("PROVISIONER_PORT", "9901"))

# ── Task store ────────────────────────────────────────────────────────────────

_tasks: dict[str, dict[str, Any]] = {}   # task_id → {status, steps, created_at}


def _task_emit(task_id: str, step: str, status: str = "running", detail: str = "") -> None:
    task = _tasks.get(task_id)
    if not task:
        return
    entry = {"ts": time.time(), "step": step, "status": status, "detail": detail}
    task["steps"].append(entry)
    if status in ("ok", "error"):
        logger.info("[provisioner] task=%s step=%s status=%s %s", task_id, step, status, detail)


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _ssh_connect_kwargs() -> dict:
    return {
        "username": SSH_USER,
        "client_keys": [str(SSH_KEY)],
        "known_hosts": None,
    }


async def _ssh_run(host: str, cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    async with asyncssh.connect(host, **_ssh_connect_kwargs()) as conn:
        result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
        return result.exit_status or 0, result.stdout or "", result.stderr or ""


async def _scp_to(host: str, local_path: Path, remote_path: str) -> None:
    async with asyncssh.connect(host, **_ssh_connect_kwargs()) as conn:
        await asyncssh.scp(str(local_path), (conn, remote_path))


async def _scp_from(host: str, remote_path: str, local_path: Path) -> None:
    async with asyncssh.connect(host, **_ssh_connect_kwargs()) as conn:
        await asyncssh.scp((conn, remote_path), str(local_path))


# ── Tunnel LaunchAgent ────────────────────────────────────────────────────────

def _launchagent_label(ip: str) -> str:
    return f"com.omni.ssh-tunnel-{ip.replace('.', '-')}"


def _launchagent_path(ip: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_launchagent_label(ip)}.plist"


def _create_tunnel_launchagent(ip: str) -> None:
    autossh = subprocess.check_output(["which", "autossh"]).decode().strip()
    label = _launchagent_label(ip)
    plist_path = _launchagent_path(ip)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{autossh}</string><string>-M</string><string>0</string><string>-N</string>
    <string>-i</string><string>{SSH_KEY}</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>StrictHostKeyChecking=accept-new</string>
    <string>-R</string><string>127.0.0.1:8899:127.0.0.1:18080</string>
    <string>{SSH_USER}@{ip}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/tmp/omni-tunnel-{ip}.log</string>
  <key>StandardErrorPath</key><string>/tmp/omni-tunnel-{ip}-err.log</string>
</dict></plist>"""
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)


def _remove_tunnel_launchagent(ip: str) -> None:
    plist_path = _launchagent_path(ip)
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink(missing_ok=True)


# ── Provisioning workflow ─────────────────────────────────────────────────────

async def _provision_task(
    task_id: str,
    server_ip: str,
    agent_id: str,
    log_paths: str,
    no_k8s: bool,
) -> None:
    emit = lambda step, status, detail="": _task_emit(task_id, step, status, detail)
    task = _tasks[task_id]

    try:
        # Step 1: SSH connectivity
        emit("ssh_check", "running", f"Testing SSH to {server_ip}...")
        rc, out, err = await _ssh_run(server_ip, "echo OK && hostname && python3 --version", timeout=15)
        if rc != 0:
            emit("ssh_check", "error", f"SSH failed: {err}"); task["status"] = "failed"; return
        emit("ssh_check", "ok", out.strip())

        # Step 2: Create & load SSH tunnel LaunchAgent
        emit("tunnel", "running", f"Creating autossh tunnel LaunchAgent for {server_ip}...")
        await asyncio.get_event_loop().run_in_executor(None, _create_tunnel_launchagent, server_ip)
        emit("tunnel", "ok", f"Tunnel LaunchAgent loaded: {_launchagent_label(server_ip)}")

        # Step 3: Verify tunnel port 8899 reachable on target
        emit("tunnel_verify", "running", "Waiting 4s for tunnel to establish...")
        await asyncio.sleep(4)
        rc, out, _ = await _ssh_run(server_ip, "ss -tlnp | grep 8899 && echo OPEN || echo CLOSED", timeout=10)
        if "OPEN" not in out:
            emit("tunnel_verify", "error", "Port 8899 not open on target — tunnel may need more time"); task["status"] = "failed"; return
        emit("tunnel_verify", "ok", "Port 8899 open on target ✓")

        # Step 4: Get/cache venv archive from source host (.86)
        venv_cache = Path("/tmp/omni-venv.tar.gz")
        if not venv_cache.exists():
            emit("venv_archive", "running", f"Archiving venv from {VENV_SOURCE_HOST}...")
            rc, _, err = await _ssh_run(
                VENV_SOURCE_HOST,
                "tar -czf /tmp/omni-venv.tar.gz -C /opt/omni-agent venv && echo OK",
                timeout=30,
            )
            if rc != 0:
                emit("venv_archive", "error", f"Archive failed: {err}"); task["status"] = "failed"; return
            emit("venv_download", "running", f"Downloading venv from {VENV_SOURCE_HOST}...")
            await _scp_from(VENV_SOURCE_HOST, "/tmp/omni-venv.tar.gz", venv_cache)
            emit("venv_download", "ok", f"venv.tar.gz cached at {venv_cache}")
        else:
            emit("venv_archive", "ok", f"Using cached venv at {venv_cache}")

        # Step 5: Upload venv + bundle to target
        emit("upload", "running", f"Uploading venv + bundle to {server_ip}...")
        await _scp_to(server_ip, venv_cache, "/tmp/omni-venv.tar.gz")
        if not BUNDLE_PATH.exists():
            emit("upload", "error", f"Bundle not found: {BUNDLE_PATH}"); task["status"] = "failed"; return
        await _scp_to(server_ip, BUNDLE_PATH, f"/tmp/{BUNDLE_PATH.name}")
        emit("upload", "ok", "Files uploaded ✓")

        # Step 6: Extract venv + mark package state done
        emit("extract", "running", "Extracting venv on target...")
        bundle_name = BUNDLE_PATH.stem  # e.g. omni-agent-1.0.0
        setup_cmd = (
            "mkdir -p /opt/omni-agent && "
            "tar -xzf /tmp/omni-venv.tar.gz -C /opt/omni-agent/ && "
            "mkdir -p /var/lib/omni-agent/install-state && "
            "touch /var/lib/omni-agent/install-state/package && "
            f"cd /tmp && tar -xzf {BUNDLE_PATH.name} && echo EXTRACTED"
        )
        rc, out, err = await _ssh_run(server_ip, setup_cmd, timeout=30)
        if rc != 0 or "EXTRACTED" not in out:
            emit("extract", "error", err[:300]); task["status"] = "failed"; return
        emit("extract", "ok", "Venv extracted, install-state marked ✓")

        # Step 7: Run installer
        emit("install", "running", "Running install.sh on target...")
        k8s_flag = "--no-k8s" if no_k8s else ""
        install_cmd = (
            f"bash /tmp/{bundle_name}/install.sh "
            f"--gateway-url {GATEWAY_URL_REMOTE} "
            f"--api-key {GATEWAY_API_KEY} "
            f"--agent-id {agent_id} "
            f"--log-paths {log_paths} "
            f"{k8s_flag} 2>&1 | tail -20"
        )
        rc, out, err = await _ssh_run(server_ip, install_cmd, timeout=60)
        if rc != 0:
            emit("install", "error", out[-300:]); task["status"] = "failed"; return
        emit("install", "ok", "install.sh completed ✓")

        # Step 8: Verify service active
        emit("verify_service", "running", "Verifying omni-agent service...")
        rc, out, _ = await _ssh_run(server_ip, "systemctl is-active omni-agent", timeout=10)
        if out.strip() != "active":
            emit("verify_service", "error", f"Service not active: {out.strip()}"); task["status"] = "failed"; return
        emit("verify_service", "ok", "omni-agent service active ✓")

        task["status"] = "done"
        emit("done", "ok", f"Agent {agent_id} installed and running on {server_ip}")

    except Exception as exc:
        _task_emit(task_id, "error", "error", str(exc))
        task["status"] = "failed"
        logger.exception("[provisioner] task %s failed", task_id)


# ── Agent operations ──────────────────────────────────────────────────────────

async def _run_ssh_action(host: str, cmd: str) -> dict:
    rc, out, err = await _ssh_run(host, cmd, timeout=30)
    return {"rc": rc, "stdout": out.strip(), "stderr": err.strip(), "ok": rc == 0}


async def _get_agent_config(host: str) -> dict[str, str]:
    rc, out, _ = await _ssh_run(host, "cat /etc/omni-agent/config.env 2>/dev/null || echo ''", timeout=10)
    config: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip()
    return config


async def _set_agent_config(host: str, config: dict[str, str]) -> None:
    content = "\n".join(f"{k}={v}" for k, v in config.items()) + "\n"
    escaped = content.replace("'", "'\\''")
    cmd = f"echo '{escaped}' > /etc/omni-agent/config.env && chmod 600 /etc/omni-agent/config.env"
    await _ssh_run(host, cmd, timeout=15)


async def _journal_stream(host: str) -> AsyncGenerator[str, None]:
    """Stream journalctl -u omni-agent -f via asyncssh."""
    async with asyncssh.connect(host, **_ssh_connect_kwargs()) as conn:
        async with conn.create_process("journalctl -u omni-agent -f -n 50 --no-pager") as proc:
            async for line in proc.stdout:
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                await asyncio.sleep(0)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Omni Provisioner", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ProvisionRequest(BaseModel):
    server_ip: str
    agent_id: str = ""
    log_paths: str = "/var/log/syslog,/var/log/auth.log"
    no_k8s: bool = True


class ConfigRequest(BaseModel):
    config: dict[str, str]
    restart: bool = True


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "ssh_key": str(SSH_KEY), "key_exists": SSH_KEY.exists()}


@app.get("/tasks")
async def list_tasks() -> dict:
    return {"tasks": [
        {k: v for k, v in t.items() if k != "steps"}
        for t in _tasks.values()
    ]}


@app.post("/provision")
async def provision(req: ProvisionRequest) -> dict:
    agent_id = req.agent_id or f"agent-{req.server_ip.split('.')[-1]}"
    task_id = uuid.uuid4().hex[:12]
    _tasks[task_id] = {
        "task_id": task_id,
        "server_ip": req.server_ip,
        "agent_id": agent_id,
        "status": "running",
        "created_at": time.time(),
        "steps": [],
    }
    asyncio.create_task(_provision_task(task_id, req.server_ip, agent_id, req.log_paths, req.no_k8s))
    return {"task_id": task_id, "agent_id": agent_id}


@app.get("/provision/{task_id}/stream")
async def provision_stream(task_id: str) -> StreamingResponse:
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    async def _gen() -> AsyncGenerator[str, None]:
        seen = 0
        while True:
            task = _tasks[task_id]
            steps = task["steps"]
            for step in steps[seen:]:
                yield f"data: {json.dumps(step)}\n\n"
                seen += 1
            if task["status"] in ("done", "failed"):
                yield f"data: {json.dumps({'step': '__end__', 'status': task['status']})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/agent/{server_ip}/restart")
async def agent_restart(server_ip: str) -> dict:
    return await _run_ssh_action(server_ip, "systemctl restart omni-agent")


@app.post("/agent/{server_ip}/stop")
async def agent_stop(server_ip: str) -> dict:
    return await _run_ssh_action(server_ip, "systemctl stop omni-agent")


@app.post("/agent/{server_ip}/enable")
async def agent_enable(server_ip: str) -> dict:
    return await _run_ssh_action(server_ip, "systemctl enable omni-agent")


@app.post("/agent/{server_ip}/disable")
async def agent_disable(server_ip: str) -> dict:
    return await _run_ssh_action(server_ip, "systemctl disable omni-agent")


@app.post("/agent/{server_ip}/uninstall")
async def agent_uninstall(server_ip: str) -> dict:
    result = await _run_ssh_action(
        server_ip,
        "bash /tmp/omni-agent-1.0.0/install.sh --uninstall 2>&1 | tail -10 && echo DONE || echo FAILED",
    )
    if result.get("ok"):
        # Remove tunnel LaunchAgent from Mac
        await asyncio.get_event_loop().run_in_executor(None, _remove_tunnel_launchagent, server_ip)
    return result


@app.get("/agent/{server_ip}/journal")
async def agent_journal(server_ip: str) -> StreamingResponse:
    return StreamingResponse(_journal_stream(server_ip), media_type="text/event-stream")


@app.get("/agent/{server_ip}/config")
async def agent_get_config(server_ip: str) -> dict:
    config = await _get_agent_config(server_ip)
    return {"server_ip": server_ip, "config": config}


@app.post("/agent/{server_ip}/config")
async def agent_set_config(server_ip: str, req: ConfigRequest) -> dict:
    await _set_agent_config(server_ip, req.config)
    if req.restart:
        await _run_ssh_action(server_ip, "systemctl restart omni-agent")
    return {"ok": True, "restarted": req.restart}


if __name__ == "__main__":
    logger.info("Omni Provisioner starting on :%d", PORT)
    logger.info("SSH key: %s (exists=%s)", SSH_KEY, SSH_KEY.exists())
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
