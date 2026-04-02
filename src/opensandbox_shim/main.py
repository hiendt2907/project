"""OpenSandbox shim — tạo Job tạm trong namespace opensandbox (không shell trên omni-worker)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from typing import Any

from aiohttp import web
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NS = os.environ.get("SANDBOX_NAMESPACE", "opensandbox")
LISTEN = os.environ.get("SHIM_LISTEN_PORT", "8888")


def _sanitize_job_name(run_id: str) -> str:
    h = re.sub(r"[^a-z0-9]", "", (run_id or "x").lower())[:12] or uuid.uuid4().hex[:12]
    return f"sbx-{h}"[:40]


def _sanitize_label_key(k: str) -> str:
    s = re.sub(r"[^-a-zA-Z0-9._]", "", (k or ""))[:63]
    return s.strip()


def _build_extra_env(body: dict[str, Any]) -> list:
    out: list = []
    raw = body.get("env") or []
    if not isinstance(raw, list):
        return out
    for e in raw[:64]:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()[:253]
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        val = str(e.get("value") if e.get("value") is not None else "")[:1024]
        out.append(client.V1EnvVar(name=name, value=val))
    return out


def _merge_pod_labels(body: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {"app": "opensandbox-exec"}
    extra = body.get("pod_labels") or {}
    if not isinstance(extra, dict):
        return labels
    n = 0
    for k, v in extra.items():
        if n >= 24:
            break
        kk = _sanitize_label_key(str(k))
        vv = str(v)[:128]
        if kk and vv and kk not in ("opensandbox.io/run",):
            labels[kk] = vv
            n += 1
    return labels


def _run_job_sync(body: dict[str, Any]) -> dict[str, Any]:
    cmd = str(body.get("command") or "").strip()
    if not cmd:
        return {"exit_code": -1, "stdout": "", "stderr": "empty command"}
    image = str(body.get("image") or "busybox:1.36").strip()
    timeout_sec = int(min(max(body.get("timeout_sec") or 120, 5), 600))

    config.load_incluster_config()
    batch = client.BatchV1Api()
    core = client.CoreV1Api()

    name = _sanitize_job_name(str(body.get("run_id") or uuid.uuid4().hex))
    pod_labels = _merge_pod_labels(body)
    extra_env = _build_extra_env(body)
    ctr = client.V1Container(
        name="exec",
        image=image,
        image_pull_policy="IfNotPresent",
        command=["/bin/sh", "-c", cmd],
        security_context=client.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=False,
            run_as_non_root=True,
            run_as_user=65534,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        ),
        resources=client.V1ResourceRequirements(
            limits={"cpu": "200m", "memory": "256Mi"},
            requests={"cpu": "25m", "memory": "32Mi"},
        ),
    )
    if extra_env:
        ctr.env = extra_env
    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=NS,
            labels={"app": "opensandbox-exec", "opensandbox.io/run": name},
        ),
        spec=client.V1JobSpec(
            ttl_seconds_after_finished=120,
            backoff_limit=0,
            active_deadline_seconds=timeout_sec + 30,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=pod_labels),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    security_context=client.V1PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65534,
                        fs_group=65534,
                        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[ctr],
                ),
            ),
        ),
    )

    try:
        batch.create_namespaced_job(namespace=NS, body=job)
    except ApiException as e:
        logger.exception("create job")
        return {"exit_code": -1, "stdout": "", "stderr": f"create_job:{e.status}:{e.reason}"}

    deadline = time.monotonic() + timeout_sec + 35
    pod_name = ""
    exit_code = -1
    log_text = ""
    err_text = ""

    try:
        while time.monotonic() < deadline:
            try:
                j = batch.read_namespaced_job(name=name, namespace=NS)
                st = j.status
                if st.active:
                    pass
                if st.succeeded is not None and st.succeeded >= 1:
                    exit_code = 0
                    break
                if st.failed is not None and st.failed >= 1:
                    exit_code = 1
                    break
                pl = core.list_namespaced_pod(
                    namespace=NS,
                    label_selector=f"job-name={name}",
                )
                if pl.items:
                    pod_name = pl.items[0].metadata.name or ""
                    phase = pl.items[0].status.phase if pl.items[0].status else None
                    if phase == "Failed":
                        exit_code = 1
                        break
            except ApiException:
                pass
            time.sleep(0.4)

        if pod_name:
            try:
                log_text = core.read_namespaced_pod_log(name=pod_name, namespace=NS, tail_lines=500) or ""
            except ApiException as e:
                err_text = f"log:{e.status}"
        else:
            err_text = "no pod for job"

        if exit_code < 0 and time.monotonic() >= deadline:
            exit_code = 124
            err_text = (err_text + " timeout").strip()
    finally:
        try:
            batch.delete_namespaced_job(
                name=name,
                namespace=NS,
                propagation_policy="Background",
            )
        except ApiException:
            pass

    return {
        "exit_code": exit_code,
        "stdout": log_text[:16000],
        "stderr": err_text[:4000],
    }


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def execute(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"exit_code": -1, "stdout": "", "stderr": "invalid json"}, status=400)
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_job_sync, body),
            timeout=float(min(int(body.get("timeout_sec") or 120) + 60, 700)),
        )
    except asyncio.TimeoutError:
        result = {"exit_code": 124, "stdout": "", "stderr": "shim executor timeout"}
    return web.json_response(result)


def main() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/v1/execute", execute)
    port = int(LISTEN)
    logger.info("opensandbox-shim ns=%s listen=:%s", NS, port)
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
