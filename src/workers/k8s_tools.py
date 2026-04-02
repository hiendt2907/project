"""Kubernetes — chỉ kubernetes_asyncio: list rộng vs inspect sâu (metrics + events + chart + logs)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException, CustomObjectsApi

from workers.lab_shell import _audit_lab_shell
from workers.telegram_ctx import effective_telegram_chat_id, should_send_telegram_chart
from visualization.chart_bytes import pod_cpu_memory_bar_png_bytes, pod_cpu_memory_usage_absolute_png_bytes

logger = logging.getLogger(__name__)

POD_INDEX_REDIS_KEY = "omni:k8s:pod_index:{ns}"


def redis_key_rollout_pending(chat_id: int) -> str:
    return f"omni:rollout_pending:{chat_id}"


def redis_key_write_pending(chat_id: int) -> str:
    """Pending JSON sau gated sandbox+validation (rollout allowlist, mở rộng sau)."""
    return f"omni:write_pending:{chat_id}"


def _discover_pairs_from_hint(hint: str, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ưu tiên exact name → prefix → substring (cùng logic tinh thần với _resolve_pod_name)."""
    hint_l = hint.strip().lower().replace("_", "-")
    exact = [p for p in pairs if p[1].lower() == hint_l]
    if exact:
        return exact
    pref = [p for p in pairs if p[1].lower().startswith(hint_l + "-")]
    if pref:
        return sorted(pref, key=lambda x: len(x[1]))
    sub = [p for p in pairs if hint_l in p[1].lower()]
    return sorted(sub, key=lambda x: len(x[1]))


async def discover_pod_across_namespaces(v1: client.CoreV1Api, hint: str) -> list[tuple[str, str]]:
    resp = await v1.list_pod_for_all_namespaces()
    pairs: list[tuple[str, str]] = []
    for p in resp.items or []:
        n = p.metadata.name or ""
        ns = p.metadata.namespace or ""
        if n:
            pairs.append((ns, n))
    return _discover_pairs_from_hint(hint, pairs)


@dataclass(frozen=True)
class PodIdentityResult:
    """Kết quả resolve pod — chỉ từ SDK scan hoặc list trong namespace user chỉ định (không đoán namespace mặc định)."""

    kind: Literal["resolved", "ambiguous", "not_found_cluster", "not_found_namespace"]
    namespace: str | None = None
    pod_name: str | None = None
    candidates: tuple[tuple[str, str], ...] = ()


async def resolve_pod_identity(
    v1: client.CoreV1Api,
    hint: str,
    explicit_namespace: str | None,
) -> PodIdentityResult:
    """
    Discovery-first: thiếu namespace → bắt buộc `list_pod_for_all_namespaces` (quét cluster).
    Có namespace → chỉ `list_namespaced_pod` trong namespace đó.
    """
    h = (hint or "").strip()
    if not h:
        return PodIdentityResult(kind="not_found_cluster")

    ens = (explicit_namespace or "").strip()
    if ens:
        try:
            lr = await v1.list_namespaced_pod(namespace=ens)
        except ApiException as e:
            if e.status == 404:
                return PodIdentityResult(kind="not_found_namespace", namespace=ens)
            raise
        resolved = _resolve_pod_name(h, list(lr.items))
        if resolved:
            return PodIdentityResult(kind="resolved", namespace=ens, pod_name=resolved)
        return PodIdentityResult(kind="not_found_namespace", namespace=ens)

    matches = await discover_pod_across_namespaces(v1, h)
    if len(matches) == 0:
        return PodIdentityResult(kind="not_found_cluster")
    if len(matches) == 1:
        ns, pn = matches[0]
        return PodIdentityResult(kind="resolved", namespace=ns, pod_name=pn)
    return PodIdentityResult(kind="ambiguous", candidates=tuple(matches))


@dataclass(frozen=True)
class DeploymentIdentityResult:
    """Kết quả resolve deployment — SDK scan hoặc list trong namespace."""

    kind: Literal["resolved", "ambiguous", "not_found_cluster", "not_found_namespace"]
    namespace: str | None = None
    deployment_name: str | None = None
    candidates: tuple[tuple[str, str], ...] = ()


async def resolve_deployment_identity(
    apps: Any,
    hint: str,
    explicit_namespace: str | None,
) -> DeploymentIdentityResult:
    """Thiếu namespace → quét cluster; có namespace → chỉ list deployment trong ns đó."""
    h = (hint or "").strip()
    if not h:
        return DeploymentIdentityResult(kind="not_found_cluster")

    ens = (explicit_namespace or "").strip()
    if ens:
        try:
            lr = await apps.list_namespaced_deployment(ens)
        except ApiException as e:
            if e.status == 404:
                return DeploymentIdentityResult(kind="not_found_namespace", namespace=ens)
            raise
        resolved = _resolve_pod_name(h, list(lr.items))
        if resolved:
            return DeploymentIdentityResult(kind="resolved", namespace=ens, deployment_name=resolved)
        return DeploymentIdentityResult(kind="not_found_namespace", namespace=ens)

    matches = await discover_deployment_across_namespaces(apps, h)
    if len(matches) == 0:
        return DeploymentIdentityResult(kind="not_found_cluster")
    if len(matches) == 1:
        ns, dn = matches[0]
        return DeploymentIdentityResult(kind="resolved", namespace=ns, deployment_name=dn)
    return DeploymentIdentityResult(kind="ambiguous", candidates=tuple(matches))


async def discover_deployment_across_namespaces(apps: Any, hint: str) -> list[tuple[str, str]]:
    resp = await apps.list_deployment_for_all_namespaces()
    pairs: list[tuple[str, str]] = []
    for d in resp.items or []:
        n = d.metadata.name or ""
        ns = d.metadata.namespace or ""
        if n:
            pairs.append((ns, n))
    return _discover_pairs_from_hint(hint, pairs)


async def execute_rollout_restart(namespace: str, deployment_name: str) -> str:
    await _load_k8s_config()
    apps = client.AppsV1Api()
    try:
        dep = await apps.read_namespaced_deployment(deployment_name, namespace)
        ann = dep.spec.template.metadata.annotations or {}
        ann = dict(ann)
        ann["kubectl.kubernetes.io/restartedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
        dep.spec.template.metadata.annotations = ann
        await apps.replace_namespaced_deployment(deployment_name, namespace, dep)
        return (
            f"[DATA] rollout_restart_ok deployment={deployment_name} ns={namespace}\n"
            "[DIAGNOSIS] Đã set restartedAt (tương đương kubectl rollout restart)."
        )
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    finally:
        await apps.api_client.close()


async def execute_rollout_restart_from_pending(_ctx: Any, data: dict[str, Any]) -> str:
    ns = data.get("namespace")
    dep = data.get("deployment")
    if not isinstance(ns, str) or not ns.strip():
        return "[DATA] error\n[DIAGNOSIS] Thiếu namespace trong pending."
    if not isinstance(dep, str) or not dep.strip():
        return "[DATA] error\n[DIAGNOSIS] Thiếu deployment trong pending."
    setattr(_ctx, "k8s_mutated", True)
    return await execute_rollout_restart(ns.strip(), dep.strip())


async def _load_k8s_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


def _format_pod_list(resp: Any, namespace: str) -> str:
    rows: list[str] = []
    for p in sorted(resp.items, key=lambda x: (x.metadata.name or "")):
        name = p.metadata.name or "?"
        phase = p.status.phase or "?"
        ip = p.status.pod_ip or "-"
        rows.append(f"- {name}\t{phase}\t{ip}")
    if not rows:
        return f"Namespace `{namespace}`: không có pod."
    header = f"Pods namespace `{namespace}` ({len(rows)}):\n"
    return header + "\n".join(rows)


def _cpu_to_cores(s: str | None) -> float:
    if not s:
        return 0.0
    raw = str(s).strip()
    if raw.endswith("n"):
        return int(raw[:-1]) / 1_000_000_000.0
    if raw.endswith("m"):
        return int(raw[:-1]) / 1000.0
    return float(raw)


def _mem_to_bytes(s: str | None) -> int:
    if not s:
        return 0
    raw = str(s).strip()
    for suf, mul in (
        ("Ki", 1024),
        ("Mi", 1024**2),
        ("Gi", 1024**3),
        ("Ti", 1024**4),
        ("K", 1000),
        ("M", 1000**2),
        ("G", 1000**3),
    ):
        if raw.endswith(suf):
            return int(float(raw[: -len(suf)]) * mul)
    if raw.isdigit():
        return int(raw)
    return 0


def _resolve_pod_name(hint: str, items: list[Any]) -> str | None:
    hint_l = hint.strip().lower()
    names = [p.metadata.name for p in items if getattr(p.metadata, "name", None)]
    for n in names:
        if n.lower() == hint_l:
            return n
    pref = [n for n in names if n.lower().startswith(hint_l + "-")]
    if pref:
        return sorted(pref, key=len)[0]
    sub = [n for n in names if hint_l in n.lower()]
    if sub:
        return sorted(sub, key=len)[0]
    return None


def _aggregate_limits(pod: Any) -> tuple[float, int]:
    cpu_cores = 0.0
    mem_b = 0
    for c in pod.spec.containers or []:
        lim = getattr(c.resources, "limits", None) or {}
        req = getattr(c.resources, "requests", None) or {}
        if lim.get("cpu") or lim.get("memory"):
            if lim.get("cpu"):
                cpu_cores += _cpu_to_cores(str(lim.get("cpu")))
            if lim.get("memory"):
                mem_b += _mem_to_bytes(str(lim.get("memory")))
        else:
            if req.get("cpu"):
                cpu_cores += _cpu_to_cores(str(req.get("cpu")))
            if req.get("memory"):
                mem_b += _mem_to_bytes(str(req.get("memory")))
    return cpu_cores, mem_b


def _usage_from_metrics_body(body: dict[str, Any]) -> tuple[float, int]:
    cpu_cores = 0.0
    mem_b = 0
    for c in body.get("containers") or []:
        u = c.get("usage") or {}
        cpu_cores += _cpu_to_cores(u.get("cpu"))
        mem_b += _mem_to_bytes(u.get("memory"))
    return cpu_cores, mem_b


def _pct(used: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return min(100.0, (used / cap) * 100.0)


def _event_is_warning_or_critical(e: Any) -> bool:
    typ = (getattr(e, "type", None) or "").strip()
    reason = (getattr(e, "reason", None) or "").strip()
    if typ == "Warning":
        return True
    crit = ("OOM", "BackOff", "Failed", "Kill", "Unhealthy", "Probe")
    return any(c in reason for c in crit)


async def _redis_pod_hints(ctx: Any, ns: str) -> str:
    r = getattr(ctx, "redis", None)
    if r is None:
        return ""
    try:
        raw = await r.get(POD_INDEX_REDIS_KEY.format(ns=ns))
        if not raw:
            return ""
        names = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        if isinstance(names, list) and names:
            return "Gợi ý từ cache (list gần nhất): " + ", ".join(names[:12])
    except Exception:
        pass
    return ""


async def tool_list_namespace_pods(ctx: Any, args: dict[str, Any]) -> str:
    """Liệt kê pod trong **một** namespace — bắt buộc args.namespace (không đoán mặc định)."""
    explicit = args.get("namespace")
    if explicit is None or (isinstance(explicit, str) and not explicit.strip()):
        return (
            "Chưa có namespace trong args — gõ rõ `namespace`, hoặc gọi `k8s_list_pods` không truyền "
            "`namespace` (tương đương kubectl get pods -A). God/lab: có thể "
            "`execute_shell_command` với kubectl trực tiếp."
        )
    ns = str(explicit).strip()

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"

    v1 = client.CoreV1Api()
    try:
        resp = await v1.list_namespaced_pod(namespace=ns)
        names = [p.metadata.name for p in resp.items if getattr(p.metadata, "name", None)]
        r = getattr(ctx, "redis", None)
        if r is not None and names:
            try:
                await r.setex(
                    POD_INDEX_REDIS_KEY.format(ns=ns),
                    600,
                    json.dumps(names, ensure_ascii=False),
                )
            except Exception as e:
                logger.debug("pod index cache: %s", e)
    except ApiException as e:
        logger.warning("k8s list_namespaced_pod: %s", e)
        return f"Lỗi Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        logger.warning("k8s list_namespaced_pod failed: %s", e)
        return f"Lỗi Kubernetes API: {e!s}"
    finally:
        await v1.api_client.close()

    return _format_pod_list(resp, ns)


_MAX_ALL_PODS_ROWS = 800
_KUBECTL_LIST_ALL_TIMEOUT = 120.0
_KUBECTL_LIST_ALL_MAX_BYTES = 400_000


def _ws_allows_kubectl_list_all(ctx: Any) -> bool:
    """God/lab: dùng kubectl subprocess; còn lại kubernetes_asyncio."""
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return False
    return bool(getattr(ws, "lab_unchained", False) or getattr(ws, "god_mode", False))


def _parse_kubectl_get_pods_lines(lines: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Từ stdout kubectl get pods -A: cặp (ns, name) và dòng hiển thị (bỏ header)."""
    pairs: list[tuple[str, str]] = []
    body_lines: list[str] = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "NAMESPACE" and parts[1] == "NAME":
            continue
        ns, name = parts[0], parts[1]
        pairs.append((ns, name))
        body_lines.append(line)
    return pairs, body_lines


async def _list_all_pods_via_kubectl(ctx: Any, args: dict[str, Any]) -> str:
    """God/lab: `kubectl get pods -A -o wide` (subprocess), audit audit:sandbox."""
    max_rows = int(args.get("limit") or _MAX_ALL_PODS_ROWS)
    max_rows = max(1, min(max_rows, 10_000))
    trace = str(getattr(ctx, "inbound_trace_id", "") or "kubectl-list-pods").strip() or "kubectl-list-pods"
    cmd = ("kubectl", "get", "pods", "-A", "-o", "wide")
    logger.info("[k8s] god/lab list_all_pods kubectl trace=%s", trace)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_KUBECTL_LIST_ALL_MAX_BYTES + 64_000,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_KUBECTL_LIST_ALL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await _audit_lab_shell(
            ctx,
            trace_id=trace,
            command=" ".join(cmd),
            exit_code=-1,
            stdout="",
            stderr="timeout",
        )
        return f"[DATA] error\n[DIAGNOSIS] kubectl timeout {_KUBECTL_LIST_ALL_TIMEOUT}s\n[TRACE] {trace}"

    stdout = (out_b or b"").decode("utf-8", errors="replace")
    stderr = (err_b or b"").decode("utf-8", errors="replace")
    code = int(proc.returncode if proc.returncode is not None else -1)
    await _audit_lab_shell(
        ctx,
        trace_id=trace,
        command=" ".join(cmd),
        exit_code=code,
        stdout=stdout[:8000],
        stderr=stderr[:4000],
    )
    if code != 0:
        return (
            f"[DATA] error\n[DIAGNOSIS] kubectl exit={code}: {stderr.strip() or stdout[:500]}\n[TRACE] {trace}"
        )

    lines = stdout.splitlines()
    pairs, body_lines = _parse_kubectl_get_pods_lines(lines)
    total = len(body_lines)
    clipped = body_lines[:max_rows]
    try:
        setattr(ctx, "pod_discovery_pairs", pairs[:max_rows])
    except Exception:
        pass
    if not clipped:
        return f"[DATA] empty\n[DIAGNOSIS] kubectl không trả dòng pod.\n[TRACE] {trace}"
    more = ""
    if total > max_rows:
        more = f"\n... (+{total - max_rows} pod — tăng args.limit nếu cần)"
    head = (
        f"Pods toàn cluster (kubectl get pods -A -o wide, god/lab, tối đa {max_rows} / {total} pod):\n"
    )
    return head + "\n".join(clipped) + more + f"\n[TRACE] {trace}"


async def tool_list_all_pods_sdk(ctx: Any, args: dict[str, Any]) -> str:
    """
    Liệt kê Pod trên toàn cluster — tương đương kubectl get pods -A.
    God/lab: subprocess kubectl; còn lại: kubernetes_asyncio list_pod_for_all_namespaces.
    args: limit? (số dòng tối đa, mặc định 800, tối đa 10000).
    """
    if _ws_allows_kubectl_list_all(ctx):
        return await _list_all_pods_via_kubectl(ctx, args)

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"
    max_rows = int(args.get("limit") or _MAX_ALL_PODS_ROWS)
    max_rows = max(1, min(max_rows, 10_000))

    v1 = client.CoreV1Api()
    try:
        resp = await v1.list_pod_for_all_namespaces()
    except ApiException as e:
        return f"[DATA] error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
    finally:
        await v1.api_client.close()

    items = sorted(
        resp.items or [],
        key=lambda x: ((x.metadata.namespace or ""), (x.metadata.name or "")),
    )
    rows: list[str] = []
    pairs: list[tuple[str, str]] = []
    for p in items[:max_rows]:
        name = p.metadata.name or "?"
        ns = p.metadata.namespace or "?"
        phase = p.status.phase or "?"
        ip = p.status.pod_ip or "-"
        pairs.append((ns, name))
        rows.append(f"{ns}\t{name}\t{phase}\t{ip}")
    try:
        setattr(ctx, "pod_discovery_pairs", pairs)
    except Exception:
        pass
    if not rows:
        return "[DATA] empty\n[DIAGNOSIS] Cluster không có pod trả về API."
    head = f"Pods toàn cluster (SDK quét mọi namespace, hiển thị tối đa {max_rows} / {len(items)} pod):\n"
    more = ""
    if len(items) > max_rows:
        more = f"\n... (+{len(items) - max_rows} pod — tăng args.limit nếu cần)"
    return head + "\n".join(rows) + more


async def tool_k8s_list_pods(ctx: Any, args: dict[str, Any]) -> str:
    """Không có namespace → quét toàn cluster (god/lab: kubectl; không thì SDK); có ns → `list_namespace_pods`."""
    ns = args.get("namespace")
    if ns is None or (isinstance(ns, str) and not str(ns).strip()):
        return await tool_list_all_pods_sdk(ctx, args)
    return await tool_list_namespace_pods(ctx, args)


async def tool_namespace_pods_top(ctx: Any, args: dict[str, Any]) -> str:
    """
    Tương đương `kubectl top pods -n <ns>` — CPU/RAM usage từ metrics.k8s.io (SDK, không shell).
    args: namespace (bắt buộc).
    """
    ns = str(args.get("namespace") or "").strip()
    if not ns:
        return "[DATA] error\n[DIAGNOSIS] Thiếu args.namespace."
    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"

    v1 = client.CoreV1Api()
    co = CustomObjectsApi()
    try:
        resp = await v1.list_namespaced_pod(namespace=ns)
        items = sorted(resp.items or [], key=lambda x: (x.metadata.name or ""))
        rows: list[str] = []
        for pod in items:
            name = pod.metadata.name or "?"
            phase = pod.status.phase or "?"
            use_cpu, use_mem = 0.0, 0
            ok_m = False
            try:
                mbody = await co.get_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="pods",
                    name=name,
                )
                if isinstance(mbody, dict):
                    use_cpu, use_mem = _usage_from_metrics_body(mbody)
                    ok_m = True
            except ApiException:
                pass
            except Exception as e:
                logger.debug("namespace_pods_top metrics %s: %s", name, e)
            if ok_m:
                rows.append(
                    f"{name}\t{phase}\tcpu_cores≈{use_cpu:.4f}\tmem_Mi≈{use_mem / (1024**2):.1f}"
                )
            else:
                rows.append(f"{name}\t{phase}\t(metrics.k8s.io: n/a)")

        if not rows:
            return f"[DATA] empty\n[DIAGNOSIS] Namespace `{ns}` không có pod."
        head = f"[DATA] kubectl top pods (SDK metrics.k8s.io) namespace=`{ns}` ({len(rows)} pod)\n"
        head += "NAME\tPHASE\tCPU/MEM\n"
        return head + "\n".join(rows) + "\n[DIAGNOSIS] Usage từ metrics-server; n/a = không đọc được PodMetrics."
    except ApiException as e:
        return f"[DATA] error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
    finally:
        await v1.api_client.close()
        await co.api_client.close()


async def tool_resolve_pod_identity(ctx: Any, args: dict[str, Any]) -> str:
    """
    Gợi ý cặp namespace/pod từ hint (SDK quét cluster khi thiếu namespace).
    args: pod_name hoặc hint hoặc name; namespace? (để trống = quét cluster).
    """
    hint = str(args.get("pod_name") or args.get("hint") or args.get("name") or "").strip()
    if not hint:
        return "Thiếu args.pod_name hoặc hint."
    explicit = args.get("namespace")
    explicit_ns = str(explicit).strip() if explicit else ""

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"

    v1 = client.CoreV1Api()
    try:
        ident = await resolve_pod_identity(v1, hint, explicit_ns or None)
        if ident.kind == "ambiguous":
            lines = [f"- `{pns}/{pname}`" for pns, pname in ident.candidates[:20]]
            return (
                "[DATA] ambiguous_pod\n"
                + "\n".join(lines)
                + "\n[DIAGNOSIS] Nhiều pod khớp — chọn một cặp namespace/pod rồi gọi `inspect_pod_deep` với `namespace` rõ."
            )
        if ident.kind == "not_found_cluster":
            return (
                f"[DATA] pod_not_found hint={hint!r}\n[DIAGNOSIS] "
                f"Không thấy pod khớp trên cluster; thử `list_all_pods_sdk` hoặc kiểm tra tên."
            )
        if ident.kind == "not_found_namespace":
            h = await _redis_pod_hints(ctx, ident.namespace or "")
            msg = (
                f"[DATA] pod_not_found hint={hint!r} ns={ident.namespace!r}\n"
                "[DIAGNOSIS] Không thấy pod khớp trong namespace này."
            )
            if h:
                msg += f" {h}"
            return msg
        assert ident.kind == "resolved"
        ns = ident.namespace or ""
        pn = ident.pod_name or ""
        return (
            f"[DATA] resolved_pod namespace={ns!r} pod_name={pn!r}\n"
            f"[DIAGNOSIS] Dùng cặp này cho `inspect_pod_deep` / `k8s_tail_logs` (pod_name={pn!r}, namespace={ns!r})."
        )
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
    finally:
        await v1.api_client.close()


async def tool_resolve_deployment_identity(ctx: Any, args: dict[str, Any]) -> str:
    """
    Gợi ý cặp namespace/deployment từ hint (SDK quét cluster khi thiếu namespace).
    args: deployment hoặc name hoặc hint; namespace? (để trống = quét cluster).
    """
    hint = str(args.get("deployment") or args.get("name") or args.get("hint") or "").strip()
    if not hint:
        return "Thiếu args.deployment hoặc hint."
    explicit = args.get("namespace")
    explicit_ns = str(explicit).strip() if explicit else ""

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"

    apps = client.AppsV1Api()
    try:
        ident = await resolve_deployment_identity(apps, hint, explicit_ns or None)
        if ident.kind == "ambiguous":
            lines = [f"- `{sns}/{sdep}`" for sns, sdep in ident.candidates[:20]]
            return (
                "[DATA] ambiguous_deployment\n"
                + "\n".join(lines)
                + "\n[DIAGNOSIS] Nhiều deployment khớp — chọn một cặp rồi gọi lại với `namespace` rõ."
            )
        if ident.kind == "not_found_cluster":
            return (
                f"[DATA] deployment_not_found hint={hint!r}\n[DIAGNOSIS] "
                "Không thấy deployment khớp trên cluster."
            )
        if ident.kind == "not_found_namespace":
            return (
                f"[DATA] deployment_not_found hint={hint!r} ns={ident.namespace!r}\n"
                "[DIAGNOSIS] Không thấy deployment khớp trong namespace này."
            )
        assert ident.kind == "resolved"
        ns = ident.namespace or ""
        dn = ident.deployment_name or ""
        return (
            f"[DATA] resolved_deployment namespace={ns!r} deployment={dn!r}\n"
            f"[DIAGNOSIS] Dùng cho `k8s_rollout_restart` (deployment={dn!r}, namespace={ns!r}) khi policy cho phép."
        )
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
    finally:
        await apps.api_client.close()


async def tool_inspect_pod_deep(ctx: Any, args: dict[str, Any]) -> str:
    """
    Deep-dive một pod (SDK): metrics + restart + log cuối + **events Warning** + bar chart.
    Định dạng: [DATA] … [DIAGNOSIS] …
    args: pod_name, namespace?, tail_lines? (1–500, mặc định 5), send_telegram?, chat_id?
    """
    hint = str(args.get("pod_name") or args.get("name") or "").strip()
    if not hint:
        return "Missing args.pod_name."
    explicit = args.get("namespace")
    explicit_ns = str(explicit).strip() if explicit else ""
    try:
        tail_n = int(args.get("tail_lines") or args.get("log_tail") or 5)
    except (TypeError, ValueError):
        tail_n = 5
    tail_n = max(1, min(tail_n, 500))

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Cannot load kubeconfig: {e!s}"

    v1 = client.CoreV1Api()
    co = CustomObjectsApi()
    try:
        ident = await resolve_pod_identity(v1, hint, explicit_ns or None)
        if ident.kind == "ambiguous":
            lines = [f"- `{pns}/{pname}`" for pns, pname in ident.candidates[:20]]
            return (
                "[DATA] ambiguous_pod\n"
                + "\n".join(lines)
                + "\n[DIAGNOSIS] Multiple matching pods — pick one namespace/pod pair and call again with explicit `namespace`."
            )
        if ident.kind == "not_found_cluster":
            return (
                f"[DATA] pod_not_found hint={hint!r}\n[DIAGNOSIS] "
                f"No pod named {hint!r} found cluster-wide; check the name or list pods to discover."
            )
        if ident.kind == "not_found_namespace":
            h = await _redis_pod_hints(ctx, ident.namespace or "")
            msg = (
                f"[DATA] pod_not_found hint={hint!r} ns={ident.namespace!r}\n"
                "[DIAGNOSIS] No matching pod in this namespace (SDK list in namespace)."
            )
            if h:
                msg += f" {h}"
            return msg

        assert ident.kind == "resolved"
        ns = ident.namespace or ""
        resolved = ident.pod_name or ""

        pod = await v1.read_namespaced_pod(name=resolved, namespace=ns)
        phase = pod.status.phase or "?"
        restarts = 0
        ready_n = 0
        for cs in pod.status.container_statuses or []:
            restarts += int(cs.restart_count or 0)
            if getattr(cs, "ready", False):
                ready_n += 1
        n_spec = len(pod.spec.containers or [])
        containers = pod.spec.containers or []
        main_container = containers[0].name if containers else None

        lim_cpu, lim_mem = _aggregate_limits(pod)
        use_cpu, use_mem = 0.0, 0
        metrics_ok = False
        try:
            mbody = await co.get_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=ns,
                plural="pods",
                name=resolved,
            )
            if isinstance(mbody, dict):
                use_cpu, use_mem = _usage_from_metrics_body(mbody)
                metrics_ok = True
        except ApiException as e:
            if e.status != 404:
                logger.warning("metrics pod %s: %s", resolved, e)
        except Exception as e:
            logger.warning("metrics pod %s: %s", resolved, e)

        denom_cpu = lim_cpu
        denom_mem = lim_mem
        has_limit_or_request = denom_cpu > 0 or denom_mem > 0
        cpu_pct = _pct(use_cpu, denom_cpu) if denom_cpu > 0 else 0.0
        mem_pct = _pct(float(use_mem), float(denom_mem)) if denom_mem > 0 else 0.0

        log_lines: list[str] = []
        if main_container:
            try:
                raw_log = await v1.read_namespaced_pod_log(
                    name=resolved,
                    namespace=ns,
                    container=main_container,
                    tail_lines=tail_n,
                )
                lines_kept = (raw_log or "").strip().split("\n")[-tail_n:]
                for ln in lines_kept:
                    log_lines.append(ln[:500])
            except Exception as e:
                log_lines.append(f"(log: {e!s})")
        else:
            log_lines.append("(no container)")

        ev_warn: list[str] = []
        try:
            ev_resp = await v1.list_namespaced_event(
                namespace=ns,
                field_selector=f"involvedObject.name={resolved},involvedObject.kind=Pod",
            )
            events = list(ev_resp.items or [])

            def _ev_ts(e: Any) -> datetime:
                lt = getattr(e, "last_timestamp", None) or getattr(e, "event_time", None)
                if lt is None:
                    return datetime.min.replace(tzinfo=UTC)
                if lt.tzinfo is None:
                    return lt.replace(tzinfo=UTC)
                return lt

            events.sort(key=_ev_ts, reverse=True)
            for e in events:
                if not _event_is_warning_or_critical(e):
                    continue
                reason = getattr(e, "reason", None) or ""
                msg = (getattr(e, "message", None) or "")[:220]
                typ = getattr(e, "type", None) or ""
                ev_warn.append(f"[{typ}] {reason}: {msg}")
                if len(ev_warn) >= 5:
                    break
        except Exception as e:
            ev_warn.append(f"(events: {e!s})")

        data_block = [
            f"pod={resolved} ns={ns} phase={phase} ready={ready_n}/{n_spec} restarts={restarts}",
        ]
        if metrics_ok:
            if has_limit_or_request:
                data_block.append(
                    f"resource: cpu_used_cores≈{use_cpu:.4f} mem_used_Mi≈{use_mem / (1024**2):.1f} "
                    f"pct_of_limit_cpu={cpu_pct:.1f}% pct_of_limit_mem={mem_pct:.1f}%"
                )
            else:
                data_block.append(
                    f"resource: cpu_used_cores≈{use_cpu:.4f} mem_used_Mi≈{use_mem / (1024**2):.1f} "
                    f"(no limits/requests in spec — no % of limit; chart shows absolute usage)"
                )
        else:
            data_block.append(
                "resource: metrics.k8s.io unavailable (metrics-server/RBAC/kubelet) — no usage data."
            )

        data_block.append(f"log_tail({tail_n}):")
        data_block.extend(log_lines)
        data_block.append("events_warning:")
        data_block.extend(ev_warn if ev_warn else ["(no Warning/critical events)"])

        diag = "Pod Running; no obvious issue from summary fields."
        if phase != "Running":
            diag = f"Phase={phase} — check workload health."
        if restarts > 3:
            diag = f"High restart count ({restarts}) — review Warning events and logs."
        if ev_warn and not ev_warn[0].startswith("(no Warning"):
            diag = "Warning/critical events present — see [DATA]."

        if metrics_ok and has_limit_or_request:
            png = pod_cpu_memory_bar_png_bytes(cpu_pct, mem_pct, title=f"{resolved} CPU/RAM % of limit")
        elif metrics_ok:
            png = pod_cpu_memory_usage_absolute_png_bytes(
                use_cpu_cores=use_cpu,
                use_mem_bytes=use_mem,
                title=f"{resolved} CPU/RAM usage",
            )
        else:
            png = pod_cpu_memory_bar_png_bytes(0.0, 0.0, title=f"{resolved} (no metrics)")

        send = should_send_telegram_chart(ctx, args)
        cid = effective_telegram_chat_id(ctx, args)
        tg = getattr(ctx, "telegram", None)
        chart_note = f"chart_png_bytes={len(png)}"
        flow = "flow: worker → matplotlib → PNG"
        if send and tg is not None and cid is not None:
            try:
                await tg.send_photo_bytes(cid, png, caption=f"{resolved} @ {ns}"[:200])
                chart_note += f" | telegram_photo chat_id={cid}"
                flow += " → Telegram sendPhoto (sent)"
            except Exception as e:
                chart_note += f" telegram_err={e!s}"
                flow += f" → Telegram error: {e!s}"
        else:
            chart_note += " | chart not sent (no telegram client or chat_id)"
            flow += " → text-only; no image sent"
        chart_note += f"\n{flow}"

        out = (
            "[DATA]\n"
            + "\n".join(data_block)
            + f"\n{chart_note}\n"
            + "[DIAGNOSIS]\n"
            + diag
        )
        return out
    except ApiException as e:
        return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"
    finally:
        await v1.api_client.close()
        await co.api_client.close()


async def tool_k8s_rollout_restart(ctx: Any, args: dict[str, Any]) -> str:
    """
    Rollout restart một Deployment (SDK replace + annotation restartedAt).
    Nếu user không nói rõ restart/rollout trong tin nhắn → Redis pending + [CONFIRM_REQUIRED] (Telegram).
    args: deployment (hoặc name), namespace? (bỏ trống = tìm cluster-wide).
    """
    name = str(args.get("deployment") or args.get("name") or "").strip()
    if not name:
        return "Thiếu args.deployment (tên Deployment)."
    explicit_ns = str(args.get("namespace") or "").strip()

    try:
        await _load_k8s_config()
    except Exception as e:
        return f"Không load kubeconfig: {e!s}"

    apps = client.AppsV1Api()
    try:
        if explicit_ns:
            try:
                await apps.read_namespaced_deployment(name, explicit_ns)
            except ApiException as e:
                if e.status == 404:
                    return (
                        f"[DATA] deployment_not_found deployment={name!r} ns={explicit_ns}\n"
                        "[DIAGNOSIS] Không có Deployment."
                    )
                return f"[DATA] api_error\n[DIAGNOSIS] Kubernetes API ({e.status}): {e.reason}"
            ns, dep_name = explicit_ns, name
        else:
            matches = await discover_deployment_across_namespaces(apps, name)
            if len(matches) == 0:
                return f"[DATA] deployment_not_found hint={name!r}\n[DIAGNOSIS] Không tìm thấy Deployment khớp."
            if len(matches) > 1:
                lines = [f"- `{sns}/{sdep}`" for sns, sdep in matches[:20]]
                return (
                    "[DATA] ambiguous_deployment\n"
                    + "\n".join(lines)
                    + "\n[DIAGNOSIS] Nhiều deployment khớp — gõ lại với `namespace` (hoặc tên deployment đầy đủ)."
                )
            ns, dep_name = matches[0]
    finally:
        await apps.api_client.close()

    explicit_user = getattr(ctx, "restart_rollout_explicit", False)
    proactive = getattr(ctx, "inbound_proactive", False) is True
    chat_id = getattr(ctx, "telegram_chat_id", None)
    r = getattr(ctx, "redis", None)
    ws = getattr(ctx, "settings", None)
    lab = bool(getattr(ws, "lab_unchained", False)) if ws is not None else False

    if explicit_user or lab or proactive:
        return await execute_rollout_restart(ns, dep_name)

    if chat_id is None or r is None:
        return (
            "[DATA] confirm_required\n[DIAGNOSIS] Rollout/restart cần xác nhận Telegram — "
            "hoặc user gõ rõ restart/rollout trong tin nhắn."
        )

    await r.setex(
        redis_key_rollout_pending(int(chat_id)),
        600,
        json.dumps({"namespace": ns, "deployment": dep_name}, ensure_ascii=False),
    )
    return (
        "[CONFIRM_REQUIRED] "
        f"Rollout restart Deployment `{dep_name}` tại namespace `{ns}`.\n"
        "Trả lời `xác nhận` hoặc `confirm` để thực hiện."
    )


async def tool_inspect_pod_details(ctx: Any, args: dict[str, Any]) -> str:
    """Alias `inspect_pod_deep` (tương thích)."""
    return await tool_inspect_pod_deep(ctx, args)
