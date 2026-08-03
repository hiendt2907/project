#!/usr/bin/env python3
"""Bắt tay MCP stdio với một server khai trong .mcp.json.

Đọc đúng command/args/env mà Claude Code sẽ dùng, spawn server, gửi
initialize + tools/list, in ra serverInfo và danh sách tool.

    python3 .mcp/handshake.py .mcp.json kubernetes

stdout:
    server=<name> v<version>
    tools=<tên tool, phân tách bằng dấu phẩy>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

HANDSHAKE_TIMEOUT_SEC = 180
PROTOCOL_VERSION = "2025-06-18"


def load_server(config_path: str, name: str) -> dict:
    with open(config_path) as fh:
        config = json.load(fh)
    servers = config.get("mcpServers", {})
    if name not in servers:
        raise KeyError(f"server '{name}' không có trong {config_path}")
    return servers[name]


def build_env(spec: dict) -> dict:
    env = os.environ.copy()
    for key, value in (spec.get("env") or {}).items():
        env[key] = os.path.expandvars(value)
    return env


def handshake(spec: dict) -> tuple[str, list[str]]:
    argv = [spec["command"], *spec.get("args", [])]

    requests = [
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "omni-verify", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(r) + "\n" for r in requests)

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=build_env(spec),
    )

    # Server chỉ trả lời sau khi đọc hết yêu cầu; đóng stdin để nó không chờ thêm.
    killer = threading.Timer(HANDSHAKE_TIMEOUT_SEC, proc.kill)
    killer.start()
    try:
        stdout, stderr = proc.communicate(input=payload)
    finally:
        killer.cancel()

    server_info: dict | None = None
    tools: list[str] | None = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = msg.get("result")
        if not isinstance(result, dict):
            continue
        if "serverInfo" in result:
            server_info = result["serverInfo"]
        if "tools" in result:
            tools = [t.get("name", "?") for t in result["tools"]]

    if server_info is None:
        raise RuntimeError(f"không nhận được initialize\nstderr: {stderr[:500]}")
    if tools is None:
        raise RuntimeError(f"initialize OK nhưng thiếu tools/list\nstderr: {stderr[:500]}")

    label = f"{server_info.get('name', '?')} v{server_info.get('version', '?')}"
    return label, tools


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: handshake.py <.mcp.json> <server-name>", file=sys.stderr)
        return 2

    try:
        spec = load_server(sys.argv[1], sys.argv[2])
        label, tools = handshake(spec)
    except Exception as exc:  # noqa: BLE001 — script xác minh, báo lỗi nguyên văn
        print(str(exc), file=sys.stderr)
        return 1

    print(f"server={label}")
    print("tools=" + ",".join(tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
