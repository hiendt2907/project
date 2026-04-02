#!/usr/bin/env python3
"""Chaos drill v1 — stress CPU + ConfigMap delete, validate Prom/Loki, safe cleanup.

Uses kubernetes_asyncio + httpx only (no kubectl subprocess). Run from repo root:
  .venv/bin/python scripts/chaos_drill_v1.py --namespace chaos-drill
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

logger = logging.getLogger("chaos_drill_v1")

DEFAULT_PROMETHEUS_BASE = "http://prometheus.monitor.svc.cluster.local:9090"
DEFAULT_LOKI_BASE = "http://loki.monitor.svc.cluster.local:3100"
CHAOS_PREFIX = "chaos-drill-v1"
LOW_SIGMA_THRESHOLD = 0.01


# --- Pure helpers (tested in tests/test_chaos_drill_v1.py) ---


def parse_prometheus_instant(data: dict[str, Any]) -> tuple[float, float] | None:
    """Return (timestamp_unix, value) from /api/v1/query instant vector, or None."""
    if data.get("status") != "success":
        return None
    res = (data.get("data") or {}).get("result") or []
    if not res:
        return None
    val = res[0].get("value")
    if not val or len(val) < 2:
        return None
    try:
        return float(val[0]), float(val[1])
    except (TypeError, ValueError):
        return None


def is_sample_fresh(sample_ts: float, now: float, max_staleness_sec: float) -> bool:
    return (now - sample_ts) <= max_staleness_sec


def warn_low_sigma(stddev: float | None) -> bool:
    """Print [LOW_SIGMA] if stddev < 0.01. Returns True if warning printed."""
    if stddev is None:
        return False
    if stddev < LOW_SIGMA_THRESHOLD:
        print("[LOW_SIGMA] Z-score might be unstable", file=sys.stderr)
        return True
    return False


def self_awareness_heuristic_pass(reason_blob: str) -> bool:
    """
    Heuristic: missing ConfigMap is not fixed by rollout alone.
    PASS: CLEAR, or evidence of config/missing, or no k8s_rollout_restart in output.
    """
    u = reason_blob.upper()
    if "CLEAR" in u:
        return True
    low = reason_blob.lower()
    if "k8s_rollout_restart" in low or '"k8s_rollout_restart"' in low:
        return False
    if any(
        k in low
        for k in (
            "configmap",
            "config map",
            "missing",
            "not found",
            "createcontainerconfigerror",
            "secret",
        )
    ):
        return True
    return False


async def httpx_prometheus_instant(
    hc: httpx.AsyncClient,
    base_url: str,
    promql: str,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/query"
    r = await hc.get(url, params={"query": promql})
    r.raise_for_status()
    return r.json()


async def poll_until_z_or_timeout(
    hc: httpx.AsyncClient,
    base_url: str,
    *,
    abs_z_threshold: float,
    max_staleness_sec: float,
    poll_interval_sec: float,
    timeout_sec: float,
) -> tuple[float, float] | None:
    """Poll omni:node_cpu:z until |z| > threshold with fresh sample, or timeout."""
    deadline = time.time() + timeout_sec
    promql = "omni:node_cpu:z"
    while time.time() < deadline:
        try:
            data = await httpx_prometheus_instant(hc, base_url, promql)
        except Exception as e:
            logger.warning("prometheus query failed: %s", e)
            await asyncio.sleep(poll_interval_sec)
            continue
        parsed = parse_prometheus_instant(data)
        if parsed is None:
            await asyncio.sleep(poll_interval_sec)
            continue
        ts, z = parsed
        now = time.time()
        if not is_sample_fresh(ts, now, max_staleness_sec):
            await asyncio.sleep(poll_interval_sec)
            continue
        if abs(z) > abs_z_threshold:
            return ts, z
        await asyncio.sleep(poll_interval_sec)
    return None


async def loki_query_range_lines(
    hc: httpx.AsyncClient,
    loki_base: str,
    *,
    logql: str,
    start_sec: float,
    end_sec: float,
    limit: int = 500,
) -> list[str]:
    url = f"{loki_base.rstrip('/')}/loki/api/v1/query_range"
    # Loki expects nanoseconds
    params = {
        "query": logql,
        "limit": str(limit),
        "start": str(int(start_sec * 1e9)),
        "end": str(int(end_sec * 1e9)),
    }
    r = await hc.get(url, params=params)
    r.raise_for_status()
    body = r.json()
    lines: list[str] = []
    for stream in (body.get("data") or {}).get("result") or []:
        for ts_line in stream.get("values") or []:
            if len(ts_line) >= 2:
                lines.append(str(ts_line[1]))
    return lines


# --- K8s resources ---


def _build_configmap(namespace: str, name: str, data: dict[str, str]) -> client.V1ConfigMap:
    return client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"app": CHAOS_PREFIX, "chaos-drill": "v1"},
        ),
        data=data,
    )


def _build_consumer_deployment(namespace: str, cm_name: str, deploy_name: str) -> client.V1Deployment:
    labels = {"app": deploy_name}
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=deploy_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="consumer",
                            image="busybox:1.36",
                            command=["sleep", "infinity"],
                            env_from=[
                                client.V1EnvFromSource(
                                    config_map_ref=client.V1ConfigMapEnvSource(name=cm_name),
                                )
                            ],
                        )
                    ],
                ),
            ),
        ),
    )


def _build_stress_deployment(namespace: str, deploy_name: str, stress_image: str, cpu_workers: int) -> client.V1Deployment:
    labels = {"app": deploy_name}
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=deploy_name,
            namespace=namespace,
            labels=labels,
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="stress",
                            image=stress_image,
                            # polinux/stress: ENTRYPOINT stress — args only (multi-arch friendly)
                            args=["--cpu", str(cpu_workers), "--timeout", "0"],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "500m"},
                                limits={"cpu": "2000m"},
                            ),
                        )
                    ],
                ),
            ),
        ),
    )


async def wait_deployment_ready(
    apps_v1: client.AppsV1Api,
    namespace: str,
    name: str,
    *,
    timeout_sec: float = 180.0,
    poll_interval: float = 2.0,
) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            dep = await apps_v1.read_namespaced_deployment_status(name, namespace)
        except ApiException:
            await asyncio.sleep(poll_interval)
            continue
        st = dep.status
        ready = getattr(st, "ready_replicas", None) or 0
        repl = getattr(st, "replicas", None) or 0
        if repl and ready >= repl:
            return True
        await asyncio.sleep(poll_interval)
    return False


@dataclass
class ChaosDrillState:
    namespace: str
    cm_name: str
    consumer_deploy: str
    stress_deploy: str
    cm_snapshot: client.V1ConfigMap | None = None
    cleanup_done: bool = False
    skip_cleanup: bool = False


async def kube_load() -> None:
    try:
        await config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


async def ensure_namespace(core_v1: client.CoreV1Api, namespace: str) -> None:
    try:
        await core_v1.read_namespace(namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        await core_v1.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace, labels={"chaos-drill": "v1"}))
        )


async def apply_chaos_stack(
    *,
    namespace: str,
    stress_image: str,
    cpu_workers: int,
) -> ChaosDrillState:
    cm_name = f"{CHAOS_PREFIX}-config"
    consumer_deploy = f"{CHAOS_PREFIX}-consumer"
    stress_deploy = f"{CHAOS_PREFIX}-stress"

    await kube_load()
    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()

    await ensure_namespace(core_v1, namespace)

    cm_body = _build_configmap(
        namespace,
        cm_name,
        {"CHAOS_DRILL_KEY": "chaos-drill-value", "REQUIRED_FOR_CONTAINER": "true"},
    )
    try:
        await core_v1.create_namespaced_config_map(namespace, cm_body)
    except ApiException as e:
        if e.status == 409:
            await core_v1.replace_namespaced_config_map(cm_name, namespace, cm_body)
        else:
            raise

    # Snapshot for finally restore (deep copy via dict round-trip)
    created = await core_v1.read_namespaced_config_map(cm_name, namespace)
    cm_snapshot = created

    cons_dep = _build_consumer_deployment(namespace, cm_name, consumer_deploy)
    try:
        await apps_v1.create_namespaced_deployment(namespace, cons_dep)
    except ApiException as e:
        if e.status == 409:
            await apps_v1.replace_namespaced_deployment(consumer_deploy, namespace, cons_dep)
        else:
            raise

    ok_c = await wait_deployment_ready(apps_v1, namespace, consumer_deploy, timeout_sec=120.0)
    if not ok_c:
        raise RuntimeError(f"consumer deployment not ready: {consumer_deploy}")

    stress_dep = _build_stress_deployment(namespace, stress_deploy, stress_image, cpu_workers)
    try:
        await apps_v1.create_namespaced_deployment(namespace, stress_dep)
    except ApiException as e:
        if e.status == 409:
            await apps_v1.replace_namespaced_deployment(stress_deploy, namespace, stress_dep)
        else:
            raise

    ok_s = await wait_deployment_ready(apps_v1, namespace, stress_deploy, timeout_sec=120.0)
    if not ok_s:
        raise RuntimeError(f"stress deployment not ready: {stress_deploy}")

    return ChaosDrillState(
        namespace=namespace,
        cm_name=cm_name,
        consumer_deploy=consumer_deploy,
        stress_deploy=stress_deploy,
        cm_snapshot=cm_snapshot,
    )


async def delete_configmap(core_v1: client.CoreV1Api, namespace: str, name: str) -> None:
    try:
        await core_v1.delete_namespaced_config_map(name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


async def delete_stress_deployment(apps_v1: client.AppsV1Api, namespace: str, name: str) -> None:
    try:
        await apps_v1.delete_namespaced_deployment(name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise


async def restore_configmap(core_v1: client.CoreV1Api, snapshot: client.V1ConfigMap, namespace: str) -> None:
    if snapshot is None:
        return
    body = copy.deepcopy(snapshot)
    if body.metadata is not None:
        body.metadata.resource_version = None
        body.metadata.uid = None
    name = body.metadata.name if body.metadata else ""
    try:
        await core_v1.create_namespaced_config_map(namespace, body)
    except ApiException as e:
        if e.status == 409:
            await core_v1.replace_namespaced_config_map(name, namespace, body)
        else:
            raise


async def restart_consumer_pods(core_v1: client.CoreV1Api, namespace: str, app_label: str) -> None:
    pods = await core_v1.list_namespaced_pod(namespace, label_selector=f"app={app_label}")
    for p in pods.items or []:
        n = p.metadata.name if p.metadata else None
        if n:
            try:
                await core_v1.delete_namespaced_pod(n, namespace)
            except ApiException as e:
                if e.status != 404:
                    logger.warning("delete pod %s: %s", n, e)


async def cleanup_chaos(state: ChaosDrillState, *, repair_consumer: bool) -> None:
    if state.skip_cleanup or state.cleanup_done:
        return
    await kube_load()
    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    ns = state.namespace

    try:
        await delete_stress_deployment(apps_v1, ns, state.stress_deploy)
    except Exception as e:
        logger.warning("cleanup stress deployment: %s", e)

    try:
        await restore_configmap(core_v1, state.cm_snapshot, ns)
    except Exception as e:
        logger.warning("restore configmap: %s", e)

    if repair_consumer:
        try:
            await restart_consumer_pods(core_v1, ns, state.consumer_deploy)
        except Exception as e:
            logger.warning("repair consumer pods: %s", e)

    state.cleanup_done = True


async def async_main() -> int:
    p = argparse.ArgumentParser(description="Chaos drill v1 (stress + CM delete + validation)")
    p.add_argument("--namespace", default="chaos-drill", help="Target namespace (created if missing)")
    p.add_argument("--wait-sec", type=float, default=300.0, help="Sleep after deleting ConfigMap")
    p.add_argument("--prometheus-base-url", default=os.environ.get("CHAOS_PROM_URL", DEFAULT_PROMETHEUS_BASE))
    p.add_argument("--loki-base-url", default=os.environ.get("CHAOS_LOKI_URL", DEFAULT_LOKI_BASE))
    p.add_argument("--max-staleness-sec", type=float, default=90.0)
    p.add_argument("--z-poll-timeout-sec", type=float, default=600.0)
    p.add_argument("--z-threshold", type=float, default=3.0)
    p.add_argument("--stress-image", default="polinux/stress:latest")
    p.add_argument("--stress-cpu-workers", type=int, default=8)
    p.add_argument("--skip-cleanup", action="store_true", help="DANGEROUS: skip finally restore")
    p.add_argument("--repair-consumer", action="store_true", help="After restore CM, delete consumer pods to recreate")
    p.add_argument(
        "--assert-self-awareness",
        action="store_true",
        help="Fail if Loki [AUTONOMOUS_DECIDER_REASON] heuristic fails",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(message)s")

    if args.skip_cleanup:
        print("WARNING: --skip-cleanup set; cluster may be left dirty", file=sys.stderr)

    state: ChaosDrillState | None = None
    exit_code = 0
    t_delete_cm: float | None = None

    try:
        state = await apply_chaos_stack(
            namespace=args.namespace,
            stress_image=args.stress_image,
            cpu_workers=args.stress_cpu_workers,
        )

        async with httpx.AsyncClient(timeout=30.0) as hc:
            z_hit = await poll_until_z_or_timeout(
                hc,
                args.prometheus_base_url,
                abs_z_threshold=args.z_threshold,
                max_staleness_sec=args.max_staleness_sec,
                poll_interval_sec=5.0,
                timeout_sec=args.z_poll_timeout_sec,
            )
            if z_hit is None:
                logger.warning("timeout waiting for |omni:node_cpu:z| > %s", args.z_threshold)
            else:
                logger.info("observed |omni:node_cpu:z| spike (ts,z)=%s", z_hit)

            try:
                sdata = await httpx_prometheus_instant(hc, args.prometheus_base_url, "omni:node_cpu:stddev_24h")
                sp = parse_prometheus_instant(sdata)
                stddev = sp[1] if sp else None
                if sp:
                    ts_s, _ = sp
                    if not is_sample_fresh(ts_s, time.time(), args.max_staleness_sec):
                        print("[WARN] omni:node_cpu:stddev_24h sample is stale", file=sys.stderr)
                warn_low_sigma(stddev)
            except Exception as e:
                logger.warning("stddev query failed: %s", e)

            core_v1 = client.CoreV1Api()
            assert state is not None
            await delete_configmap(core_v1, state.namespace, state.cm_name)
            t_delete_cm = time.time()

            await asyncio.sleep(args.wait_sec)

            end_sec = time.time()
            start_sec = t_delete_cm if t_delete_cm else (end_sec - args.wait_sec)

            logql_fix = '{namespace="multi-agent", pod_name=~"omni-worker.*"} |= "[AUTONOMOUS_FIX]"'
            logql_reason = '{namespace="multi-agent", pod_name=~"omni-worker.*"} |= "[AUTONOMOUS_DECIDER_REASON]"'

            lines_fix: list[str] = []
            lines_reason: list[str] = []
            try:
                lines_fix = await loki_query_range_lines(
                    hc, args.loki_base_url, logql=logql_fix, start_sec=start_sec, end_sec=end_sec
                )
                lines_reason = await loki_query_range_lines(
                    hc, args.loki_base_url, logql=logql_reason, start_sec=start_sec, end_sec=end_sec
                )
            except Exception as e:
                logger.warning("loki query failed: %s", e)
                exit_code = 3

            if not lines_fix:
                logger.warning("no [AUTONOMOUS_FIX] lines in Loki window")
                exit_code = max(exit_code, 2)
            else:
                logger.info("loki: found %s [AUTONOMOUS_FIX] lines", len(lines_fix))

            if not lines_reason:
                logger.warning("no [AUTONOMOUS_DECIDER_REASON] lines in Loki window")
            else:
                logger.info("loki: found %s [AUTONOMOUS_DECIDER_REASON] lines", len(lines_reason))

            if args.assert_self_awareness and lines_reason:
                blob = "\n".join(lines_reason)
                if not self_awareness_heuristic_pass(blob):
                    logger.error("self-awareness heuristic FAILED")
                    exit_code = max(exit_code, 4)
                else:
                    logger.info("self-awareness heuristic PASS")

            # Z cooldown: after wait, stress still running — optional second read; real cooldown after cleanup
            try:
                zdata = await httpx_prometheus_instant(hc, args.prometheus_base_url, "omni:node_cpu:z")
                zp = parse_prometheus_instant(zdata)
                if zp:
                    _ts, znow = zp
                    if is_sample_fresh(_ts, time.time(), args.max_staleness_sec):
                        logger.info("omni:node_cpu:z (end of wait window) = %s", znow)
            except Exception as e:
                logger.warning("final z query: %s", e)

    except Exception as e:
        logger.exception("chaos drill failed: %s", e)
        exit_code = 1
    finally:
        if state is not None and not args.skip_cleanup:
            await cleanup_chaos(state, repair_consumer=args.repair_consumer)

    # Post-cleanup Z (stress gone, CM restored)
    if state is not None and not args.skip_cleanup:
        try:
            async with httpx.AsyncClient(timeout=30.0) as hc:
                await asyncio.sleep(3.0)
                zdata = await httpx_prometheus_instant(hc, args.prometheus_base_url, "omni:node_cpu:z")
                zp = parse_prometheus_instant(zdata)
                if zp:
                    ts, zc = zp
                    if is_sample_fresh(ts, time.time(), args.max_staleness_sec):
                        logger.info("omni:node_cpu:z (after cleanup) = %s", zc)
        except Exception as e:
            logger.warning("post-cleanup z: %s", e)

    return exit_code


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
