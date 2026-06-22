#!/usr/bin/env python3
"""Live E2E: real remote-agent process <-> real K8s cluster (namespace multi-agent).

Khác với tests/test_remote_agent_e2e.py (in-process, ASGITransport, FakeRedis,
AsyncMock kafka) — script này KHÔNG giả lập gì ở tầng hạ tầng:

  - Spawn `python -m remote_agent.agent` như 1 process thật, riêng biệt (đóng vai
    "máy khách" cài agent).
  - Agent gọi gateway thật qua network thật (ingress Traefik `gateway.ai-agent.local`,
    HTTP thật, không ASGITransport).
  - Redis thật trong cluster (đọc qua `kubectl exec redis-0 -- redis-cli`, không FakeRedis).
  - Kafka thật trong cluster (đọc qua `kubectl exec <kafka-pod> -- kafka-console-consumer.sh`,
    không AsyncMock).
  - API key thật lấy runtime từ Secret `omni-gateway-secret` trong cluster, không hardcode.

Usage:
    python scripts/e2e_remote_agent_live.py

Env overrides:
    OMNI_E2E_GATEWAY_URL    default: http://gateway.ai-agent.local
    OMNI_E2E_NAMESPACE      default: multi-agent
    OMNI_E2E_COLLECT_INTERVAL_S  default: 8 (collect_interval thật của agent, để emit nhanh)

Yêu cầu trước khi chạy:
    - kubectl context đang point vào cluster lab (OrbStack), namespace multi-agent đang Running.
    - /etc/hosts có entry cho gateway.ai-agent.local (đã có sẵn trong môi trường lab).
    - PYTHONPATH=src để import được remote_agent.* (script tự set khi spawn subprocess).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import uuid

NAMESPACE = os.getenv("OMNI_E2E_NAMESPACE", "multi-agent")
GATEWAY_URL = os.getenv("OMNI_E2E_GATEWAY_URL", "http://gateway.ai-agent.local")
COLLECT_INTERVAL_S = os.getenv("OMNI_E2E_COLLECT_INTERVAL_S", "8")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


def _section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _kubectl(*args: str, timeout: int = 15) -> str:
    out = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=True
    )
    return out.stdout


def _get_secret_value(secret: str, key: str) -> str:
    raw = _kubectl("get", "secret", secret, "-n", NAMESPACE, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode().strip()


def _redis_get(key: str) -> str | None:
    redis_pod = _kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", "app=redis", "-o", "jsonpath={.items[0].metadata.name}"
    ).strip() or "redis-0"
    out = _kubectl("exec", "-n", NAMESPACE, redis_pod, "--", "redis-cli", "GET", key).strip()
    return None if out == "(nil)" or not out else out


def _kafka_pod() -> str:
    return _kubectl(
        "get", "pods", "-n", NAMESPACE, "-l", "app=kafka", "-o", "jsonpath={.items[0].metadata.name}"
    ).strip()


def _kafka_grep(topic: str, needle: str, timeout_ms: int = 25000, max_messages: int = 30) -> bool:
    """Run real kafka-console-consumer inside the cluster, grep for needle."""
    pod = _kafka_pod()
    try:
        out = subprocess.run(
            [
                "kubectl", "exec", "-n", NAMESPACE, pod, "--",
                "/opt/kafka/bin/kafka-console-consumer.sh",
                "--bootstrap-server", "localhost:9092",
                "--topic", topic,
                "--timeout-ms", str(timeout_ms),
                "--max-messages", str(max_messages),
            ],
            capture_output=True, text=True, timeout=(timeout_ms / 1000) + 15,
        )
    except subprocess.TimeoutExpired as exc:
        out_stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return needle in out_stdout
    return needle in out.stdout


def main() -> int:
    results: dict[str, bool] = {}

    _section("STEP 0 — Lấy real API key từ Secret omni-gateway-secret trong cluster")
    tenant_keys_raw = _get_secret_value("omni-gateway-secret", "OMNI_TENANT_APIKEYS")
    admin_key = _get_secret_value("omni-gateway-secret", "OMNI_GATEWAY_API_KEY")
    tenant_key = ""
    for pair in tenant_keys_raw.split(","):
        tid, _, key = pair.partition(":")
        if tid == "default":
            tenant_key = key
            break
    if not tenant_key:
        print("  Không tìm thấy tenant 'default' trong OMNI_TENANT_APIKEYS — abort.")
        return 1
    print(f"  tenant_key (default): {tenant_key[:12]}...")
    print(f"  admin_key           : {admin_key[:12]}...")

    agent_id = f"e2e-live-{uuid.uuid4().hex[:8]}"
    print(f"  agent_id thật cho lần chạy này: {agent_id}")

    _section("STEP 1 — Spawn agent process THẬT (subprocess python -m remote_agent.agent)")
    env = os.environ.copy()
    env.update(
        PYTHONPATH=os.path.join(REPO_ROOT, "src"),
        OMNI_AGENT_GATEWAY_URL=GATEWAY_URL,
        OMNI_AGENT_API_KEY=tenant_key,
        OMNI_AGENT_ID=agent_id,
        OMNI_AGENT_TENANT_ID="default",
        OMNI_AGENT_COLLECT_INTERVAL=COLLECT_INTERVAL_S,
        OMNI_AGENT_K8S_ENABLED="false",  # máy host không có in-cluster kubeconfig cho probe k8s
    )
    log_path = f"/tmp/{agent_id}.log"
    log_file = open(log_path, "w")
    print(f"  GATEWAY_URL={GATEWAY_URL}  collect_interval={COLLECT_INTERVAL_S}s")
    print(f"  log: {log_path}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "remote_agent.agent"],
        cwd=REPO_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )

    try:
        _section("STEP 2 — Verify REGISTER thật ghi vào Redis thật")
        reg_key = f"omni:remote_agent:registry:{agent_id}"
        registered = None
        for _ in range(15):
            registered = _redis_get(reg_key)
            if registered:
                break
            time.sleep(2)
        results["register_real_redis"] = bool(registered)
        print(f"  {reg_key} = {registered}")
        print(f"  [{_PASS if registered else _FAIL}] register ghi registry thật vào Redis")

        _section("STEP 3 — Verify EVIDENCE thật (psutil real) tới Kafka thật")
        found_evidence = _kafka_grep("omni-diagnostic-evidence", agent_id, timeout_ms=int(COLLECT_INTERVAL_S) * 3000)
        results["evidence_real_kafka"] = found_evidence
        print(f"  [{_PASS if found_evidence else _FAIL}] tìm thấy agent_id={agent_id} trong topic omni-diagnostic-evidence")

        _section("STEP 4 — Command channel round-trip THẬT (enqueue -> agent poll thật -> execute subprocess thật -> submit)")
        import urllib.request

        cmd_id_marker = uuid.uuid4().hex[:10]
        body = json.dumps(
            {"agent_id": agent_id, "commands": [{"command": "uname", "args": ["-a"], "purpose": cmd_id_marker}]}
        ).encode()
        req = urllib.request.Request(
            f"{GATEWAY_URL}/webhook/agent/commands/enqueue",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {tenant_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                enqueue_resp = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 - report and continue to summary
            print(f"  enqueue thất bại: {exc}")
            enqueue_resp = {}
        print(f"  enqueue response: {enqueue_resp}")
        cmd_ids = enqueue_resp.get("cmd_ids", [])
        results["enqueue_real_http"] = bool(cmd_ids)

        cmd_result = None
        if cmd_ids:
            result_key = f"omni:diag:cmdresult:{cmd_ids[0]}"
            for _ in range(15):
                cmd_result = _redis_get(result_key)
                if cmd_result:
                    break
                time.sleep(2)
        results["command_roundtrip_real"] = cmd_result is not None
        print(f"  cmd result: {cmd_result}")
        print(f"  [{_PASS if cmd_result else _FAIL}] agent thật poll+execute+submit kết quả lệnh thật")

    finally:
        _section("CLEANUP — kill agent process thật")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()

    _section("KẾT QUẢ")
    all_pass = True
    for name, ok in results.items():
        all_pass &= ok
        print(f"  [{_PASS if ok else _FAIL}] {name}")
    print(f"\n  Agent log: {log_path}")
    print(f"\n  {'TẤT CẢ PASS' if all_pass else 'CÓ BƯỚC FAIL — xem log agent + output trên'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
