"""Clarification loop: CPU/RAM mơ hồ (chưa rõ Host / Pod / Namespace) — không gọi tool; Redis wait-state."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from workers.infra_preflight import LearnedContext

from workers.session_state import SHORT_CLARIFICATION_QUESTION_VI, SessionState

# Giá trị state (tương thích ví dụ user: "monitoring"); payload JSON chứa thêm original_text.
WAIT_STATE_MONITORING = "monitoring"

TARGET_KIND = Literal["host", "pod", "namespace"]


def redis_key_wait_target(chat_id: int) -> str:
    return f"wait_target:{chat_id}"


# Alias tương thích test/code cũ — cùng nội dung câu hỏi ngắn (state machine).
CLARIFICATION_QUESTION_RESOURCE_VI = SHORT_CLARIFICATION_QUESTION_VI


_RE_RESOURCE = re.compile(
    r"(cpu|ram|memory|bộ nhớ|vcpu|kiểm tra\s+cpu|check\s+cpu|kiểm tra\s+ram|check\s+ram)",
    re.IGNORECASE,
)

_RE_POD_NAME = re.compile(
    r"(?:pod|workload|deployment|deploy)\s+([\w.\-]+)",
    re.IGNORECASE,
)

# Không coi là tên pod (regex cũ bắt nhầm "cụ" từ "pod cụ thể")
_BAD_POD_NAME_TOKENS = frozenset(
    {
        "cụ",
        "thể",
        "đi",
        "ở",
        "là",
        "theo",
        "một",
        "hai",
        "ba",
        "pods",
        "pod",
        "namespace",
        "ns",
        "cục",
    }
)


def parse_namespace_hint(text: str) -> str | None:
    """'ở namespace multi-agent', 'namespace multi-agent', 'ns multi-agent'."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(
        r"(?:ở\s+)?(?:namespace|ns)\s*[:=]?\s*([\w.\-]+)",
        raw,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return None

_RE_K8S_NAME = re.compile(r"\b[\w.\-]{2,64}-\w+-\w+\b")


def _has_explicit_target(text: str) -> bool:
    """Đã chỉ định Host / Pod (có tên) / Namespace — không coi là mơ hồ."""
    t = text.lower().strip()
    # kubectl -n / --namespace (không cần chữ "namespace")
    if re.search(r"(?:^|\s)(?:-n|--namespace)\s+[\w.-]+", t):
        return True
    if any(
        x in t
        for x in (
            "host",
            "node",
            "máy chủ",
            "localhost",
            "toàn bộ node",
            "toàn node",
            "worker node",
            "máy host",
            "physical",
        )
    ):
        return True
    if "namespace" in t or re.search(r"\bns\s*[:=]", t):
        return True
    if _RE_POD_NAME.search(text):
        return True
    # pod name kiểu deployment-hash-hash
    if _RE_K8S_NAME.search(text):
        return True
    return False


def is_scope_ambiguous_cpu_ram(text: str) -> bool:
    """Chỉ CPU/RAM mơ hồ (chưa host/pod/ns) — dùng preflight để tránh embed sớm."""
    s = (text or "").strip()
    if len(s) < 4:
        return False
    if "[CONTEXT:" in s and "mục tiêu" in s:
        return False
    if not _RE_RESOURCE.search(s):
        return False
    if _has_explicit_target(s):
        return False
    return True


def is_ambiguous_resource_check(
    text: str,
    state: SessionState | None = None,
    learned: LearnedContext | None = None,
) -> bool:
    """
    User hỏi CPU/RAM tổng quát mà không nói rõ đo Host, Pod hay Namespace.
    → Phải hỏi lại; **cấm** gọi tool cho tới khi có đáp án.
    """
    s = (text or "").strip()
    if len(s) < 4:
        return False
    if learned is not None and getattr(learned, "clarification_bypass", False):
        return False
    if "[CONTEXT:" in s and "mục tiêu" in s:
        return False
    if state is not None and (state.monitoring_target_type or "").strip().lower() == "host":
        return False
    if not _RE_RESOURCE.search(s):
        return False
    if _has_explicit_target(s):
        return False
    return True


def parse_resource_followup(text: str) -> tuple[TARGET_KIND, str | None] | None:
    """
    Parse câu trả lời sau clarification: Host / Pod / Namespace (+ tên tuỳ chọn).
    Hỗ trợ tiếng Việt: "1 pod cụ thể đi", "một pod", "pod omni-worker", v.v.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    t = raw.lower()

    # --- Tiếng Việt / câu dài (ưu tiên trước regex pod\s+\w+ để không bắt nhầm "cụ") ---
    if re.search(
        r"(pod\s+cụ\s+thể|một\s+pod|cụ\s+thể\s+là\s+pod|chọn\s+pod|theo\s+pod|"
        r"pod\s+riêng|pod\s+cụ\s+thể|1\s+pod\s+cụ\s+thể|^\s*1\s+pod\b|^\s*2\s*[\.)]?\s*$|^\s*2\s+pod\b)",
        t,
    ):
        return ("pod", None)
    if re.search(
        r"(^\s*1\s*[\.)]?\s*$|^\s*1\s+host\b|^\s*1\s+node\b|toàn\s+bộ\s+host|thứ\s+nhất\s*:\s*host)",
        t,
    ):
        return ("host", None)
    if re.search(r"(^\s*3\s*[\.)]?\s*$|^\s*3\s+namespace\b|theo\s+namespace|chọn\s+namespace)", t):
        return ("namespace", parse_namespace_hint(raw))

    if t in ("1", "1.", "một", "một."):
        return ("host", None)
    if t in ("2", "2.", "hai", "hai."):
        return ("pod", None)
    if t in ("3", "3.", "ba", "ba."):
        return ("namespace", parse_namespace_hint(raw))

    if any(
        k in t
        for k in (
            "host",
            "toàn bộ host",
            "node",
            "máy chủ",
            "máy host",
            "localhost",
            "worker node",
            "toàn node",
        )
    ):
        return ("host", None)

    if "namespace" in t or re.search(r"\bns\s*[:=]", t):
        return ("namespace", parse_namespace_hint(raw))

    m = _RE_POD_NAME.search(raw)
    if m:
        name = m.group(1).strip()
        if name.lower() not in _BAD_POD_NAME_TOKENS and len(name) >= 2:
            return ("pod", name)

    if "pod" in t:
        return ("pod", None)

    return None


def merge_clarification_context(
    *,
    original_user_text: str,
    followup_text: str,
    target: TARGET_KIND,
    detail: str | None,
    namespace_hint: str | None = None,
) -> str:
    """Nối ngữ cảnh cho slow-path (Tier-1 LLM) — bắt buộc gọi đúng tool theo target."""
    parts = [
        f"[CONTEXT: User đã chọn mục tiêu = {target.upper()}",
    ]
    if detail:
        parts[0] += f"; detail={detail}"
    if namespace_hint:
        parts[0] += f"; namespace={namespace_hint}"
    parts[0] += ".]"
    parts.append(f"Câu hỏi gốc: {original_user_text.strip()}")
    parts.append(f"Trả lời vừa gõ: {followup_text.strip()}")
    if target == "host":
        parts.append(
            "Hành động: gọi `system_psutil` (Host). "
            "Nếu cần time-series node: `query_prometheus_metrics` với metric node-level "
            "(intent phù hợp; không hỏi PromQL user). Vẽ chart khi có dữ liệu."
        )
    elif target == "pod":
        if namespace_hint:
            ns_line = f"namespace user nêu = `{namespace_hint}`."
        else:
            ns_line = "chưa có namespace — gọi tool `resolve_pod_identity` hoặc `list_all_pods_sdk` / `k8s_list_pods` trước; không đoán ns."
        parts.append(
            f"Hành động: {ns_line} "
            "Có tên pod → `inspect_pod_deep` (metrics.k8s.io + log) và/hoặc `query_prometheus_metrics` với đủ intent+ns+pod từ user. "
            "Cần liệt kê → `k8s_list_pods` / `list_all_pods_sdk` hoặc `list_namespace_pods` khi đã có namespace."
        )
    else:
        parts.append(
            "Hành động: `list_namespace_pods` hoặc `query_prometheus_metrics` gói namespace cụ thể."
        )
    return "\n".join(parts)


async def get_wait_payload(r: Any, chat_id: int) -> dict[str, Any] | None:
    key = redis_key_wait_target(chat_id)
    raw = await r.get(key)
    if not raw:
        return None
    if raw == WAIT_STATE_MONITORING:
        return {"state": WAIT_STATE_MONITORING, "original_text": ""}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("state") == WAIT_STATE_MONITORING:
            return data
    except json.JSONDecodeError:
        return None
    return None


async def set_wait_monitoring(r: Any, chat_id: int, original_text: str, *, ttl_sec: int = 60) -> None:
    key = redis_key_wait_target(chat_id)
    payload = json.dumps(
        {"state": WAIT_STATE_MONITORING, "original_text": original_text},
        ensure_ascii=False,
    )
    await r.set(key, payload, ex=ttl_sec)


async def clear_wait(r: Any, chat_id: int) -> None:
    await r.delete(redis_key_wait_target(chat_id))
