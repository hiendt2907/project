"""Slow-path retry: error signatures, context pruning messages, autopsy formatting."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

AttemptPhase = Literal["parse", "unknown_tool", "tool_error", "empty_model"]


@dataclass
class AttemptRecord:
    """Một vòng thất bại — dùng cho pruning, streak, autopsy, RAG exhausted payload."""

    attempt: int
    phase: AttemptPhase
    error_signature: str
    one_line: str
    detail_full: str = ""
    tool: str | None = None
    args_keys: tuple[str, ...] = field(default_factory=tuple)


def _norm_detail(d: str) -> str:
    return (d or "").strip()


def slow_path_error_signature(phase: AttemptPhase, detail: str, tool: str | None = None) -> str:
    """Chữ ký ổn định cho early-exit streak và metrics."""
    d = _norm_detail(detail).lower()
    if phase == "parse":
        return "parse_json"
    if phase == "empty_model":
        return "empty_model"
    if phase == "unknown_tool":
        t = (tool or "").strip()[:64] or "?"
        return f"unknown_tool:{t}"
    if phase == "tool_error":
        raw = _norm_detail(detail)
        low = raw.lower()
        if "403" in raw or "forbidden" in low or "khong_co_quyen" in low:
            return "tool_error:permission"
        if "thiếu pod" in low or "pod_name" in low or "missing pod" in low or "thiếu pod" in raw:
            return "tool_error:missing_pod"
        if ("thiếu" in low or "missing" in low) and "namespace" in low:
            return "tool_error:missing_namespace"
        if "apiexception" in low or ("kubernetes" in low and ("401" in raw or "403" in raw)):
            return "tool_error:k8s_api"
        if "timeout" in low or "timed out" in low:
            return "tool_error:timeout"
        if "connection" in low or "refused" in low:
            return "tool_error:network"
        return "tool_error:other"
    return "unknown_phase"


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 3].rstrip() + "..."


def truncate_for_prompt(text: str, max_len: int) -> str:
    """Rút gọn one_line / detail_full cho trace."""
    return _truncate(text, max_len)


def build_slow_path_recovery_user_message(
    user_goal: str,
    attempt_trace: list[AttemptRecord],
    *,
    max_full_detail: int = 720,
    max_one_line: int = 180,
    shell_allowed: bool = False,
) -> str:
    """Một block user thay cho N cặp assistant/user đầy đủ."""
    if not attempt_trace:
        return ""
    prev = attempt_trace[:-1]
    last = attempt_trace[-1]
    lines = [
        "[SLOW_PATH_RECOVERY]",
        f"[USER_GOAL] {_truncate(user_goal, 2000)}",
    ]
    if prev:
        lines.append("Summary of prior attempts:")
        for r in prev:
            ol = _truncate(r.one_line, max_one_line)
            tool_p = f" tool={r.tool}" if r.tool else ""
            lines.append(f"- attempt {r.attempt}{tool_p} ({r.phase}, {r.error_signature}): {ol}")
    full_src = last.detail_full or last.one_line
    lines.append("Last attempt error (fix JSON/tool for this turn):")
    lines.append(_truncate(full_src, max_full_detail))
    lines.append('Return only one JSON object {"tool":"...","args":{...}} — no prose outside JSON.')
    if shell_allowed:
        lines.append(
            "God/lab: you may use `execute_shell_command` with `command` (kubectl/shell) if needed; prefer SDK when pod/ns are known."
        )
    return "\n".join(lines)


def _error_buckets_from_trace(trace: list[AttemptRecord]) -> list[str]:
    buckets: list[str] = []
    for sig in {r.error_signature for r in trace}:
        if sig.startswith("parse_json"):
            buckets.append("parse")
        elif sig.startswith("unknown_tool"):
            buckets.append("hallucinated_tool")
        elif "permission" in sig or "k8s_api" in sig:
            buckets.append("permission_api")
        elif "missing_pod" in sig or "missing_namespace" in sig:
            buckets.append("missing_target")
        elif sig == "empty_model":
            buckets.append("empty_model")
        elif sig.startswith("tool_error"):
            buckets.append("tool_runtime")
    return sorted(set(buckets))


def _recommend_lines(buckets: list[str], tools_seen: list[str]) -> list[str]:
    rec: list[str] = []
    if "permission_api" in buckets:
        rec.append(
            "Kiểm tra RBAC ServiceAccount của omni-worker (ClusterRole/Role, metrics.k8s.io, VM URL)."
        )
    if "missing_target" in buckets:
        rec.append(
            "Chỉ rõ pod + namespace hoặc dùng `resolve_pod_identity` / `list_all_pods_sdk` / `k8s_list_pods` / `list_namespace_pods` trước khi `query_prometheus_metrics` (alias `query_victoria_metrics`)."
        )
    if "hallucinated_tool" in buckets:
        rec.append("Chỉ dùng tên tool ASCII trong system prompt; cấm kubectl/redis-cli như tên tool.")
    if "parse" in buckets:
        rec.append("Một khối JSON duy nhất, không markdown, không ``` quanh JSON.")
    if "empty_model" in buckets:
        rec.append("Model trả rỗng — giảm nhiễu prompt hoặc thử lại; kiểm tra Ollama/slot semaphore.")
    if "tool_runtime" in buckets and "missing_target" not in buckets:
        rec.append("Xem `audit_observability_stack` nếu nghi VM/scrape; kiểm tra `OMNI_VICTORIA_METRICS_URL`.")
    if not rec:
        rec.append("Thu hẹp yêu cầu (một đối tượng: pod, namespace, hoặc host) rồi thử lại.")
    return rec[:6]


def format_slow_path_autopsy(
    *,
    max_attempts: int,
    attempt_trace: list[AttemptRecord],
    exit_reason: str,
) -> str:
    """Báo cáo exhausted — deterministic, SIEM-friendly tag."""
    n = len(attempt_trace)
    tools_seen: list[str] = []
    for r in attempt_trace:
        if r.tool and r.tool not in tools_seen:
            tools_seen.append(r.tool)
    buckets = _error_buckets_from_trace(attempt_trace)
    diag_lines = [
        f"Đã thử {n} vòng (tối đa cấu hình {max_attempts}); thoát={exit_reason}.",
    ]
    for r in attempt_trace:
        t = f" `{r.tool}`" if r.tool else ""
        diag_lines.append(f"- Vòng {r.attempt}{t}: {r.phase} [{r.error_signature}] {_truncate(r.one_line, 120)}")
    bucket_txt = ", ".join(buckets) if buckets else "unknown"
    diag_lines.append(f"Nhóm lỗi: {bucket_txt}.")
    recs = _recommend_lines(buckets, tools_seen)
    tools_txt = ", ".join(tools_seen) if tools_seen else "(không có tool hợp lệ đã chạy)"
    body = (
        "[DATA] autopsy_exhausted\n"
        "[DIAGNOSIS]\n"
        + "\n".join(diag_lines)
        + f"\nTool đã thử (theo trace): {tools_txt}.\n"
        "[RECOMMEND]\n"
        + "\n".join(f"- {x}" for x in recs)
    )
    return body


def consecutive_same_signature_streak(trace: list[AttemptRecord]) -> int:
    """Số vòng thất bại liên tiếp cuối cùng cùng error_signature."""
    if not trace:
        return 0
    sig = trace[-1].error_signature
    n = 0
    for r in reversed(trace):
        if r.error_signature == sig:
            n += 1
        else:
            break
    return n


def primary_bucket_for_metrics(trace: list[AttemptRecord]) -> str:
    """Label Prometheus — signature hay gặp nhất hoặc mixed."""
    if not trace:
        return "none"
    c = Counter(r.error_signature for r in trace)
    sig, n = c.most_common(1)[0]
    if len(c) > 1 and n < len(trace):
        return "mixed"
    return sig[:64]


def summarize_attempts_for_rag(trace: list[AttemptRecord], *, max_chars: int = 1500) -> str:
    """Chuỗi nhỏ cho payload RAG."""
    rows = []
    for r in trace:
        rows.append(
            {
                "a": r.attempt,
                "phase": r.phase,
                "sig": r.error_signature,
                "tool": r.tool,
                "line": _truncate(r.one_line, 200),
            }
        )
    raw = json.dumps(rows, ensure_ascii=False)
    return raw if len(raw) <= max_chars else raw[: max_chars - 3] + "..."
