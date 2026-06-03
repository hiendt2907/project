"""Fast-Path (RAG/pgvector + tool); Slow-Path: session + Plan (reasoning) + thực thi JSON tool (gemma3:27b khi ops/heavy)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

import redis.asyncio as redis
from rag.pgvector_store import (
    COLLECTION_ACTION_EXPERIENCE, 
    COLLECTION_SOP, 
    PGVectorStore
)
from ingest.telegram import TelegramClient
from observability.normalize import redact
from llm.vllm_client import VLLMClient
from rag.error_ledger import ErrorLedger
from workers.clarification import (
    is_ambiguous_resource_check,
    parse_resource_followup,
)
from workers.entity_extract import extract_entities_llm, merge_llm_entities_into_slots
from workers.infra_context import enrich_working_text_with_infra, fetch_infra_injection_for_fallback
from workers.llm_context_budget import build_llm_options, effective_reply_max_words
from workers.autonomous_route import try_autonomous_sdk_route
from workers.infra_preflight import LearnedContext, preflight_infra_kb
from pkg.rag.gate import evaluate_rag_gate
from workers.metrics_exporter import (
    inc_experience_saved,
    inc_fastpath_hits,
    inc_llm_requests,
    inc_messages_processed,
    inc_slow_path_exhausted,
)
from workers.health_server import record_message_processed as _hc_record_msg
from execution.experience import (
    fetch_action_experience_context,
    record_routing_exhausted_no_data,
    record_routing_from_success,
)
from pkg.executor import (
    execute_rollout_restart_from_pending,
    execute_write_pending_from_redis,
    redis_key_rollout_pending,
    redis_key_write_pending,
)
from workers.baseline_snapshot import fetch_baseline_system_prompt
from workers.promql_presets import resolve_intent_from_keywords
from workers.model_routing import classify_route, dispatch_task
from workers.llm_semaphore import LLMSemaphore
from workers.session_state import (
    PENDING_AWAIT_VM_SLOTS,
    SessionState,
    load_session,
    save_session,
)
from workers.react_logging import log_react_json
from workers.request_trace import log_end_request, log_start_request, pop_trace_id, push_trace_id
from workers.otel_tracing import child_span, inbound_trace_span
from workers.routing_policy import ROUTING_SOURCES_FAST_PATH_EXECUTE, shell_fast_path_enabled
from workers.slow_path_trace import (
    AttemptRecord,
    build_slow_path_recovery_user_message,
    consecutive_same_signature_streak,
    format_slow_path_autopsy,
    primary_bucket_for_metrics,
    slow_path_error_signature,
    truncate_for_prompt,
)
from messaging.kafka_bus import KafkaBus
from workers.settings import WorkerSettings
from workers.tool_observation import prepare_tool_return_for_llm
from workers.tool_registry import get_tool_registry
from workers.tools import TOOL_REGISTRY, ToolCallPayload
from workers import llm_prompts_en as ope
from workers.prometheus_alert_enrichment import build_llm_anchor_en, infer_alert_trigger_dimension
from workers.handler_context import WorkerHandlerContext
from workers.vm_slot_accumulation import (
    enrich_slots_from_discovery,
    extract_vm_slots_from_text,
    followup_indicates_host,
    merge_vm_slots,
    nudge_vm_slots_message,
    vm_slots_ready,
    vm_slots_to_tool_args,
)

logger = logging.getLogger(__name__)


def _cap_inbound_user_reply(text: str | None, ctx: WorkerHandlerContext) -> str:
    """Giới hạn từ cho reply user-facing; không cắt JSON object trả về nguyên khối."""
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        try:
            json.loads(s)
            return s
        except Exception:
            pass
    return ope.truncate_plain_text_to_max_words(
        s,
        max_words=effective_reply_max_words(ctx.settings),
    )


# User nhắn rõ restart/rollout → tool không cần hỏi lại Telegram
RE_RESTART_ROLLOUT_EXPLICIT = re.compile(
    r"(restart|rollout|roll\s*out|khởi\s*động\s+lại|deploy\s+lại|"
    r"restart\s+deployment|rollout\s+restart)",
    re.I,
)

# kubectl get po -A / liệt kê pod → list_all_pods_sdk (god/lab: kubectl trong worker; không god: SDK)
RE_LIST_ALL_PODS_CHAT = re.compile(
    r"(get\s+po(ts)?\s+-a\b|kubectl\s+get\s+pods?\s+-a\b|liệt\s*kê\s+pod|tìm\s+pod|"
    r"list\s+all\s+pods|quét\s+pod\s+cluster|list_all_pods_sdk)",
    re.I,
)


def _effective_inbound_text_preview(payload: dict[str, Any]) -> str:
    """Text đưa vào handler (gồm parse Alertmanager) — dùng cho log text_len và LLM.

    Ghi chú: Alertmanager thường có thêm labels (namespace, pod, deployment, …).
    Chỉ dùng alertname/instance/summary khiến LLM thiếu ngữ cảnh và dễ bịa pod/tool sai.
    """
    raw = str(payload.get("text") or payload.get("message") or "").strip()
    if raw:
        return raw
    d = payload.get("data") or payload.get("payload")
    if not d:
        return ""
    try:
        if isinstance(d, dict) and "alerts" in d:
            al_list: list[str] = []
            for al in d["alerts"]:
                labels = al.get("labels", {}) if isinstance(al, dict) else {}
                annots = al.get("annotations", {}) if isinstance(al, dict) else {}
                aname = str(labels.get("alertname", "UnknownAlert") or "UnknownAlert").strip() or "UnknownAlert"
                summ = str(annots.get("summary", annots.get("description", "")) or "").strip()
                pod = str(labels.get("pod") or labels.get("pod_name") or "").strip()
                ns = str(labels.get("namespace") or "").strip()
                dep = str(labels.get("deployment") or "").strip()
                inst_raw = str(labels.get("instance") or "").strip()
                # Không dùng default "unknown" cho instance — model hay bắt nhầm thành tên pod/host.
                inst_meaningful = bool(
                    inst_raw and inst_raw.lower() not in ("unknown", "none", "n/a", "<none>")
                )
                if pod:
                    line = f"Alert: {aname} pod={pod}"
                    if ns:
                        line += f" namespace={ns}"
                    if dep:
                        line += f" deployment={dep}"
                    line += f" - {summ}" if summ else f" - (no summary)"
                elif dep:
                    line = f"Alert: {aname} deployment={dep}"
                    if ns:
                        line += f" namespace={ns}"
                    line += f" - {summ}" if summ else " - (no summary)"
                elif inst_meaningful:
                    line = f"Alert: {aname} on {inst_raw} - {summ}" if summ else f"Alert: {aname} on {inst_raw}"
                else:
                    line = f"Alert: {aname} - {summ}" if summ else f"Alert: {aname}"
                extra_bits: list[str] = []
                for key in ("namespace", "pod", "pod_name", "deployment", "job", "container", "node"):
                    v = labels.get(key)
                    if v is not None and str(v).strip():
                        extra_bits.append(f"{key}={v}")
                if extra_bits:
                    line += " | " + " ".join(extra_bits)
                trig = infer_alert_trigger_dimension(labels, annots, aname, summ)
                line += "\n" + build_llm_anchor_en(
                    namespace=ns,
                    pod=pod,
                    deployment=dep,
                    trigger=trig,
                )
                al_list.append(line)
            return "\n".join(al_list).strip()
        if isinstance(d, dict) and "text" in d:
            return str(d["text"]).strip()
    except Exception:
        return ""
    return ""


def _parse_alert_pod_namespace_from_preview(text: str) -> tuple[str | None, str | None]:
    """Lấy pod + namespace từ dòng chuẩn hoá ``Alert: ... pod=... namespace=...`` (Alertmanager)."""
    if not (text or "").strip():
        return None, None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("Alert:"):
            continue
        m_pod = re.search(r"\bpod=([^\s|]+)", line)
        m_ns = re.search(r"\bnamespace=([^\s|]+)", line)
        pod = m_pod.group(1).strip() if m_pod else None
        ns = m_ns.group(1).strip() if m_ns else None
        if pod and ns:
            return pod, ns
    m_pod = re.search(r"\bpod=([^\s|]+)", text)
    m_ns = re.search(r"\bnamespace=([^\s|]+)", text)
    if m_pod and m_ns:
        return m_pod.group(1).strip(), m_ns.group(1).strip()
    return None, None


def _preflight_hints_from_inbound(
    payload: dict[str, Any],
    raw_user_text: str,
    _src: str,
) -> dict[str, str] | None:
    """Namespace/pod từ payload hoặc dòng chuẩn hoá alert — không Redis."""
    h: dict[str, str] = {}
    ns_payload = str(payload.get("namespace") or "").strip()
    if ns_payload:
        h["namespace"] = ns_payload
    pod, ns = _parse_alert_pod_namespace_from_preview(raw_user_text)
    if ns:
        h["namespace"] = ns
    if pod:
        h["pod_name"] = pod
    return h if h else None


def _extract_duration(text: str) -> str:
    m = re.search(r"\b(\d+)\s*(h|m)\b", (text or "").lower())
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return "1h"


def _wants_host_vm_chart(state: SessionState, text: str) -> bool:
    if (state.monitoring_target_type or "").strip().lower() != "host":
        return False
    t = (text or "").lower()
    if re.search(r"\b(?:namespace|ns)\s*[:=]", t):
        return False
    if re.search(r"\bpod\s+[\w.\-]", t):
        return False
    return bool(
        re.search(
            r"(chart|biểu đồ|1h|24h|30m|victoria|time[- ]?series|lịch sử|historical|\bvm\b|query)",
            t,
        )
    )


def _user_confirms_rollout_telegram(text: str) -> bool:
    """Sau khi bot đã gửi [CONFIRM_REQUIRED], user trả lời ngắn để xác nhận."""
    t = (text or "").strip().lower()
    if len(t) > 48:
        return False
    if t in ("xác nhận", "xac nhan", "confirm", "ok", "yes", "y", "có", "co", "đồng ý", "dong y"):
        return True
    return bool(re.match(r"^(xác\s*nhận|confirm|ok|yes|có)\s*!?\s*$", t, re.I))

FINAL_FORMAT_VI = (
    "Đầu ra cuối (user-facing): **chỉ** số liệu + nhận định ngắn + ghi chart/Telegram nếu có; "
    "không lời dẫn dài, không giải thích khái niệm. Cấm yapping."
)

K8S_TOOL_GUIDANCE_VI = (
    "\n[K8S — routing ngắn]\n"
    "- Chưa rõ namespace pod: `resolve_pod_identity` (pod_name/hint, namespace?) hoặc `list_all_pods_sdk` / `k8s_list_pods`.\n"
    "- Chưa rõ namespace deployment: `resolve_deployment_identity` (deployment/name, namespace?).\n"
    "- Đã có pod: `inspect_pod_deep`, `k8s_tail_logs`, `k8s_describe_resource` (kind Pod).\n"
    "- Rollout/scale/patch: `k8s_rollout_restart`, `k8s_scale_deployment`, `k8s_patch_resource`.\n"
    "- Service/ingress/endpoint: `k8s_list_services`, `k8s_list_ingress`, `k8s_check_endpoints`.\n"
    "- Node: `k8s_list_nodes`, `k8s_node_conditions`.\n"
)

TOOL_CATALOG_PLACEHOLDER = "__TOOLS_FROM_REGISTRY__"

SLOW_SYSTEM_VI = (
    "**Đầu ra bắt buộc:** đúng **một** khối JSON `{\"tool\":\"...\",\"args\":{...}}` — không markdown, "
    "không ```, không văn giải thích trước/sau. Nếu chỉ nhắn user → `reply` + `args.text`. "
    "Role: **Senior SRE & DB Architect** — SDK-only (kubernetes_asyncio, psutil, httpx→Prometheus, "
    "redis-py with Redis Stack HNSW vector search). **Cấm** subprocess/shell/kubectl lệnh. "
    "Mày là SRE kỹ tính: nếu yêu cầu **thiếu đối tượng** (vd CPU/RAM mà không rõ Host vs Pod vs Namespace), "
    "**không** được gọi tool; hỏi lại một câu ngắn. "
    "Sau khi user trả lời, hệ thống đã **đọc ngữ cảnh** (goal + hội thoại gần) bằng helper LLM — "
    "đừng bắt chữ cứng; bám theo `[CONTEXT: ...]` trong message. "
    "Tuyệt đối không nhả dữ liệu rác hoặc list tổng quát khi chưa được user yêu cầu rõ scope. "
    "Nếu message có prefix `[CONTEXT: User đã chọn mục tiêu = ...]` thì user đã trả lời clarification — "
    "tuân thủ target đó (Host → `system_psutil`/node metrics; Pod → pod_name+namespace; Namespace → list/query theo ns). "
    "Tư duy **Inspect/Deep-dive**, không **List/Ống nhòm** khi user đã có định danh. "
    "Mọi phản hồi sau tool nên theo khung: `[DATA]` số liệu thật + `[DIAGNOSIS]` nhận định ngắn (copy từ tool nếu có). "
    "**Cấm** câu: 'Redis là…', 'Pod là…' — Fail task nếu vi phạm. "
    "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** hỏi lại scope; **không** tool. "
    "User: 'Của Host' → `system_psutil` (+ chart VM node nếu cần). "
    "User: 'Check namespace multi-agent' → `list_namespace_pods` namespace=multi-agent. "
    "User: 'Kiểm tra pod <tên>' → có thể `resolve_pod_identity` (pod_name/hint, namespace?) trước nếu chỉ có tên ngắn/alert; "
    "hoặc gọi thẳng `inspect_pod_deep` pod_name=... (cùng resolve nội bộ khi thiếu namespace — quét cluster qua SDK; god/lab: kubectl khi list); không đoán namespace. "
    "`namespace_pods_top` khi user muốn CPU/RAM pod trong một namespace (kubectl top pods -n ...). "
    "`list_all_pods_sdk` khi user muốn liệt kê pod toàn cluster (get po -A / liệt kê pod; god/lab: kubectl trong worker). "
    "`k8s_rollout_restart` deployment=... (namespace?) — **agent** tự rollout: bắt buộc [CONFIRM_REQUIRED] Telegram; "
    "nếu **user** nhắn rõ restart/rollout trong tin → không hỏi lại. "
    "User: 'Redis dạo này sao?' → `redis_expert_check` + `query_prometheus_metrics` intent=cpu|ram duration=1h (không hỏi PromQL). "
    "[RULES] Có tên Pod/DB/Service → **không** dùng tool List; dùng `inspect_*` / `*_expert_check` / `*_health_audit`. "
    "Time-series CPU/RAM/dự đoán → **bắt buộc** `query_prometheus_metrics` (alias `query_victoria_metrics`; intent=pod/namespace; forecast=true nếu user nói dự đoán) "
    "hoặc `query_historical_metrics`/`viz_vm_range_chart` (vẽ chart; cấm bảng text dài). "
    "**Cấm** bắt user cung cấp PromQL — tool tự sinh query; entity: pod_name, namespace. "
    "**Smart Caching:** Lệnh K8s read-only (node/service/ingress) mặc định bị Cache. Truyền `force_refresh=true` nếu cần nghiệm thu thay đổi mới. "
    "**Cấm** lộ rác kỹ thuật (empty series, 403, args.query) trong câu trả lời user — dùng output đã người hoá từ tool. "
    "Telegram: nếu request có chat_id, `ctx.telegram_chat_id` được gán — **chart tự gửi** (sendPhoto) "
    "trừ khi `send_telegram=false`. "
    + K8S_TOOL_GUIDANCE_VI
    + f"Tools (auto-sync từ TOOL_REGISTRY): {TOOL_CATALOG_PLACEHOLDER}. "
    "Message có `[CONTEXT: infra_topology` hoặc `topology_cache` hoặc `learned_infra` = baseline / tự học — **không** hỏi lại thông tin đã có. "
    "Tên `tool` **chỉ** được là một trong các tên ASCII đã liệt kê ở trên — **cấm** bịa tên (vd. redis-cli, ascii). "
    "**Cấm** trả lời bằng danh sách lệnh kubectl cho user chọn — phải gọi đúng tool JSON; user cần **kết quả** không phải menu. "
    "Nếu chỉ cần trả lời chữ cho user — dùng tool `reply`, args gồm field `text` (một JSON tool hợp lệ). "
    "Khi tool lỗi/thiếu param, hệ thống đưa lỗi lại cho bạn — tối đa nhiều vòng: hãy **đổi tool hoặc args** để lấy dữ liệu; "
    "lần chạy tool thành công được ghi RAG (action_experience / Postgres). "
    "JSON: một khối duy nhất {\\\"tool\\\":..., \\\"args\\\":...}. Không dùng tên tiếng Việt làm tool. "
    + FINAL_FORMAT_VI
)

SRE_JSON_GENERATOR_VI = (
    "Mày là **SRE Command Generator**. CẤM trả lời bằng văn bản tự nhiên ngoài JSON. CẤM giải thích dài. "
    "Mọi lượt PHẢI là đúng **một** khối JSON {\\\"tool\\\":..., \\\"args\\\":{...}} — không markdown, không ```. "
    "Thiếu thông tin → {\\\"tool\\\":\\\"reply\\\",\\\"args\\\":{\\\"text\\\":\\\"một câu hỏi ngắn\\\"}}."
)

SRE_JSON_GENERATOR_UNATTENDED_VI = (
    "Mày là **SRE Command Generator** (cảnh báo unattended — không user chat). "
    "CẤM prose ngoài JSON. Mọi lượt PHẢI là đúng **một** khối JSON "
    "{\\\"tool\\\":..., \\\"args\\\":{...}} — không markdown, không ```. "
    "Thiếu thông tin hoặc không thể xử lý an toàn → "
    "{\\\"tool\\\":\\\"escalate_to_human\\\",\\\"args\\\":{\\\"reason\\\":\\\"ngắn gọn\\\",\\\"detail\\\":\\\"\\\"}}. "
    "`omni_mark_resolved` **chỉ** sau khi đã điều tra và có kết luận — **không** dùng để báo thiếu dữ liệu."
)

AGENTIC_REACT_RULES_VI = (
    "\n\n[AGENTIC — ReAct]\n"
    "- Mỗi lượt: **một** JSON `{\"tool\":\"...\",\"args\":{...}}`.\n"
    "- Sau tool thành công: **không** coi đó là câu trả lời cuối; đọc `[TOOL_RESULT]` và quyết định bước tiếp.\n"
    "- Khi đã khắc phục/đủ kết luận: bắt buộc gọi **`omni_mark_resolved`** với `args.summary` (ngắn, tiếng Việt).\n"
    "- `omni_mark_resolved` **không** thay cho tool điều tra — chỉ đóng phiên sau khi đã có kết quả.\n"
    "- Output tool có thể bị cắt ngắn trong prompt — ưu tiên lặp query/gọi tool khác nếu thiếu dữ liệu.\n"
)

AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_VI = (
    "\n[UNATTENDED_ALERT]\n"
    "- Không có Telegram user — surface tool **không** gồm công cụ gửi câu hỏi tương tác cho người dùng cuối.\n"
    "- `omni_mark_resolved` **chỉ** khi đã điều tra/khắc phục xong và có kết luận.\n"
    "- Thiếu thông tin / không tự xử lý được → **`escalate_to_human`** (`args.reason` bắt buộc, `args.detail` tuỳ chọn).\n"
)


def _slow_system_body_for_unattended_alert(base: str) -> str:
    """Bỏ hướng dẫn dùng tool reply; thêm escalate_to_human trong danh sách tool."""
    s = base
    s = s.replace(
        "Nếu chỉ nhắn user → `reply` + `args.text`. ",
        "Luồng cảnh báo unattended — không có user chat trực tiếp; không dùng tool tương tác hỏi người. ",
    )
    s = s.replace(
        "**không** được gọi tool; hỏi lại một câu ngắn. ",
        "**không** được giả định user trả lời — thử điều tra best-effort hoặc `escalate_to_human`. ",
    )
    s = s.replace(
        "**không** được gọi tool; hỏi lại một câu ngắn qua `reply`. ",
        "**không** được giả định user trả lời — thử điều tra best-effort hoặc `escalate_to_human`. ",
    )
    s = s.replace(
        "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** hỏi lại scope; **không** tool. ",
        "[UNATTENDED] Thiếu scope trong alert — query metrics/k8s best-effort từ labels/instance; "
        "không hỏi ngược; không đủ thông tin → `escalate_to_human`. ",
    )
    s = s.replace(
        "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** `reply` hỏi scope; **không** tool khác. ",
        "[UNATTENDED] Thiếu scope trong alert — query metrics/k8s best-effort từ labels/instance; "
        "không hỏi ngược; không đủ thông tin → `escalate_to_human`. ",
    )
    s = s.replace(
        "Nếu chỉ cần trả lời chữ cho user — dùng tool `reply`, args gồm field `text` (một JSON tool hợp lệ). ",
        "Khi không thể hoàn thành điều tra hoặc thiếu dữ liệu bắt buộc — `escalate_to_human`. ",
    )
    return s

SLOW_SYSTEM_GOD_VI = (
    "**Đầu ra bắt buộc:** đúng **một** khối JSON `{\"tool\":\"...\",\"args\":{...}}` — không markdown, "
    "không ```, không văn giải thích trước/sau. Nếu chỉ nhắn user → `reply` + `args.text`. "
    "Role: **Senior SRE & DB Architect — God mode / lab_unchained:** "
    "được **`execute_shell_command`** với `args.command` (kubectl/shell trên pod worker; policy + audit trong lab_shell). "
    "Vẫn ưu tiên SDK (kubernetes_asyncio, psutil, httpx→Prometheus, redis-py, asyncpg/pgvector) khi đã rõ pod/namespace/host. "
    "Mọi shell **chỉ** qua tool (`execute_shell_command`, `execute_in_sandbox`, `gated_allowlisted_execute`) — không bịa tên tool. "
    "Mày là SRE kỹ tính: nếu yêu cầu **thiếu đối tượng** (vd CPU/RAM mà không rõ Host vs Pod vs Namespace), "
    "**không** được gọi tool; hỏi lại một câu ngắn qua `reply`. "
    "Sau khi user trả lời, hệ thống đã **đọc ngữ cảnh** (goal + hội thoại gần) bằng helper LLM — "
    "đừng bắt chữ cứng; bám theo `[CONTEXT: ...]` trong message. "
    "Tuyệt đối không nhả dữ liệu rác hoặc list tổng quát khi chưa được user yêu cầu rõ scope. "
    "Nếu message có prefix `[CONTEXT: User đã chọn mục tiêu = ...]` thì user đã trả lời clarification — "
    "tuân thủ target đó (Host → `system_psutil`/node metrics; Pod → pod_name+namespace; Namespace → list/query theo ns). "
    "Tư duy **Inspect/Deep-dive**, không **List/Ống nhòm** khi user đã có định danh. "
    "Mọi phản hồi sau tool nên theo khung: `[DATA]` số liệu thật + `[DIAGNOSIS]` nhận định ngắn (copy từ tool nếu có). "
    "**Cấm** câu: 'Redis là…', 'Pod là…' — Fail task nếu vi phạm. "
    "[FEW-SHOT clarify] User: 'Check CPU' (không nói host/pod/ns) → **chỉ** `reply` hỏi scope; **không** tool khác. "
    "[FEW-SHOT shell] User: 'kubectl top pods -A' / top CPU pod toàn cluster bằng CLI → "
    "`{\\\"tool\\\":\\\"execute_shell_command\\\",\\\"args\\\":{\\\"command\\\":\\\"kubectl top pods -A\\\"}}`. "
    "User: 'Của Host' → `system_psutil` (+ chart VM node nếu cần). "
    "User: 'Check namespace multi-agent' → `list_namespace_pods` namespace=multi-agent. "
    "User: 'Kiểm tra pod <tên>' → có thể `resolve_pod_identity` (pod_name/hint, namespace?) trước nếu chỉ có tên ngắn/alert; "
    "hoặc gọi thẳng `inspect_pod_deep` pod_name=... (cùng resolve nội bộ khi thiếu namespace — quét cluster qua SDK; god/lab: kubectl khi list); không đoán namespace. "
    "`namespace_pods_top` khi user muốn CPU/RAM pod trong một namespace (metrics-server qua SDK). "
    "`list_all_pods_sdk` khi user muốn liệt kê pod toàn cluster (get po -A; god/lab: kubectl trong worker). "
    "`k8s_rollout_restart` deployment=... (namespace?) — **agent** tự rollout: bắt buộc [CONFIRM_REQUIRED] Telegram; "
    "nếu **user** nhắn rõ restart/rollout trong tin → không hỏi lại. "
    "User: 'Redis dạo này sao?' → `redis_expert_check` + `query_prometheus_metrics` intent=cpu|ram duration=1h (không hỏi PromQL). "
    "[RULES] Có tên Pod/DB/Service → **không** dùng tool List; dùng `inspect_*` / `*_expert_check` / `*_health_audit`. "
    "Time-series CPU/RAM/dự đoán → **bắt buộc** `query_prometheus_metrics` (alias `query_victoria_metrics`; intent=pod/namespace; forecast=true nếu user nói dự đoán) "
    "hoặc `query_historical_metrics`/`viz_vm_range_chart` (vẽ chart; cấm bảng text dài). "
    "**Cấm** bắt user cung cấp PromQL — tool tự sinh query; entity: pod_name, namespace. "
    "**Smart Caching:** Lệnh K8s read-only (node/service/ingress) mặc định bị Cache. Truyền `force_refresh=true` nếu cần nghiệm thu thay đổi mới. "
    "**Cấm** lộ rác kỹ thuật (empty series, 403, args.query) trong câu trả lời user — dùng output đã người hoá từ tool. "
    "Telegram: nếu request có chat_id, `ctx.telegram_chat_id` được gán — **chart tự gửi** (sendPhoto) "
    "trừ khi `send_telegram=false`. "
    + K8S_TOOL_GUIDANCE_VI
    + f"Tools (auto-sync từ TOOL_REGISTRY): {TOOL_CATALOG_PLACEHOLDER}. "
    "Message có `[CONTEXT: infra_topology` hoặc `topology_cache` hoặc `learned_infra` = baseline / tự học — **không** hỏi lại thông tin đã có. "
    "Tên `tool` **chỉ** được là một trong các tên ASCII đã liệt kê ở trên — **cấm** bịa tên (vd. redis-cli, ascii). "
    "**Cấm** trả lời user bằng menu prose — luôn JSON tool; user cần **kết quả** sau khi tool chạy. "
    "Nếu chỉ cần trả lời chữ cho user — dùng tool `reply`, args gồm field `text` (một JSON tool hợp lệ). "
    "Khi tool lỗi/thiếu param, hệ thống đưa lỗi lại cho bạn — tối đa nhiều vòng: hãy **đổi tool hoặc args** để lấy dữ liệu; "
    "lần chạy tool thành công được ghi RAG (action_experience / Postgres). "
    "JSON: một khối duy nhất {\\\"tool\\\":..., \\\"args\\\":...}. Không dùng tên tiếng Việt làm tool. "
    + FINAL_FORMAT_VI
)


def _slow_path_system_messages_for_ctx(ctx: WorkerHandlerContext) -> list[dict[str, Any]]:
    """Hai block system: Generator JSON + SRE body (god shell hoặc SDK-only). LLM: English."""
    def _tool_catalog_text(unattended_alert: bool) -> str:
        names = sorted(TOOL_REGISTRY.keys())
        if unattended_alert:
            names = [n for n in names if n != "reply"]
        return ", ".join(f"`{n}`" for n in names)

    def _render_with_catalog(base: str, *, unattended_alert: bool) -> str:
        return base.replace(ope.TOOL_CATALOG_PLACEHOLDER, _tool_catalog_text(unattended_alert))

    if shell_fast_path_enabled(ctx.settings):
        return [
            {"role": "system", "content": ope.SRE_JSON_GENERATOR_EN},
            {"role": "system", "content": _render_with_catalog(ope.SLOW_SYSTEM_GOD_EN, unattended_alert=False)},
        ]
    return [
        {"role": "system", "content": ope.SRE_JSON_GENERATOR_EN},
        {"role": "system", "content": _render_with_catalog(ope.SLOW_SYSTEM_EN, unattended_alert=False)},
    ]


def build_agentic_system_messages(
    ctx: WorkerHandlerContext, *, unattended_alert: bool
) -> list[dict[str, Any]]:
    """Agentic system prompts; unattended → surface không chứa tool reply, có escalate_to_human."""
    def _tool_catalog_text(unattended: bool) -> str:
        names = sorted(TOOL_REGISTRY.keys())
        if unattended:
            names = [n for n in names if n != "reply"]
        return ", ".join(f"`{n}`" for n in names)

    def _render_with_catalog(base: str, *, unattended: bool) -> str:
        return base.replace(ope.TOOL_CATALOG_PLACEHOLDER, _tool_catalog_text(unattended))

    extra = (
        ope.AGENTIC_REACT_RULES_EN + ope.AGENTIC_REACT_RULES_UNATTENDED_SUPPLEMENT_EN
        if unattended_alert
        else ope.AGENTIC_REACT_RULES_EN
    )

    def _append_prometheus_identity(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Khi alert đã có pod+namespace — tránh LLM mở bằng list/top cả namespace."""
        if not unattended_alert:
            return msgs
        raw_in = getattr(ctx, "inbound_user_text", None)
        if not isinstance(raw_in, str):
            raw_in = ""
        pod, ns = _parse_alert_pod_namespace_from_preview(raw_in)
        if not pod or not ns:
            return msgs
        out = list(msgs)
        out.append(
            {
                "role": "system",
                "content": (
                    f"[PRIORITY — identified Prometheus alert] pod={pod} namespace={ns}. "
                    "First: `inspect_pod_deep` (pod_name + namespace) or `k8s_describe_resource` (Pod). "
                    "Do **not** start with `k8s_list_pods`, `list_all_pods_sdk`, `namespace_pods_top` — identity is known. "
                    "Probe issues: check events + container; `k8s_tail_logs` if needed."
                ),
            }
        )
        return out

    if unattended_alert:
        # Always SDK-first unattended body (matches [PRIORITY] + discovery rules). God/lab: append
        # short shell supplement — do not inject SLOW_SYSTEM_GOD_* (few-shot shell confuses CPU alerts).
        lab_extra = (
            ope.AGENTIC_LAB_SHELL_SUPPLEMENT_UNATTENDED_EN
            if shell_fast_path_enabled(ctx.settings)
            else ""
        )
        return _append_prometheus_identity(
            [
                {"role": "system", "content": ope.SRE_JSON_GENERATOR_UNATTENDED_EN},
                {
                    "role": "system",
                    "content": _render_with_catalog(
                        ope.SLOW_SYSTEM_UNATTENDED_EN,
                        unattended=True,
                    )
                    + extra
                    + lab_extra,
                },
            ]
        )
    if shell_fast_path_enabled(ctx.settings):
        return [
            {"role": "system", "content": ope.SRE_JSON_GENERATOR_EN},
            {"role": "system", "content": _render_with_catalog(ope.SLOW_SYSTEM_GOD_EN, unattended=False) + extra},
        ]
    return [
        {"role": "system", "content": ope.SRE_JSON_GENERATOR_EN},
        {"role": "system", "content": _render_with_catalog(ope.SLOW_SYSTEM_EN, unattended=False) + extra},
    ]


CONV_FALLBACK_SYSTEM_VI = (
    "Role: Mày là **SRE Lead Agent (Fallback Layer)** trên hệ thống **Apple Silicon M4**.\n"
    "Context: Mày được cung cấp **learned_context** (RAG action_experience + infra từ DeepScout trong message).\n\n"
    "**Kỷ luật phản hồi:**\n"
    "- **CẤM** văn vẻ, chào hỏi, **cấm** hỏi user chọn 1 trong N lệnh hay menu gợi ý — agent phải tự chạy tool/SDK ở luồng chính; đây chỉ là tóm tắt khi tool không gọi được.\n"
    "- **Tóm lược:** chỉ số liệu có trong context — Node / Pods / Namespace **thật** (không có thì nói \"chưa có snapshot\", không bịa).\n"
    "- **Một khối ngắn:** `Tình trạng:` (1 dòng) + `Hành động tiếp theo (tự động):` (1–2 gợi ý **nội bộ** cho engineer, không phải câu hỏi cho user).\n\n"
    "Không thêm dòng `SUGGESTIONS_JSON` trừ khi hệ thống bật chế độ nút Telegram (mặc định tắt)."
)


def _parse_suggestions_json_tail(text: str) -> tuple[str, list[str] | None]:
    """Tách nội dung hiển thị và 3 lệnh cho nút Telegram."""
    if not text or "SUGGESTIONS_JSON:" not in text:
        return text.strip(), None
    idx = text.rfind("SUGGESTIONS_JSON:")
    head = text[:idx].strip()
    tail = text[idx:].strip()
    m = re.match(r"SUGGESTIONS_JSON:\s*(\[.*\])\s*$", tail, re.DOTALL | re.IGNORECASE)
    if not m:
        return text.strip(), None
    try:
        arr = json.loads(m.group(1))
        if not isinstance(arr, list) or len(arr) < 3:
            return text.strip(), None
        cmds = [str(x).strip()[:500] for x in arr[:3]]
        return head, cmds
    except json.JSONDecodeError:
        return text.strip(), None


async def _conversational_fallback(
    ctx: WorkerHandlerContext,
    user_text: str,
    trace: str,
    *,
    reason: str,
    detail: str = "",
    learned_context: str = "",
) -> str:
    """Khi không parse được JSON tool hoặc tool không tồn tại / lỗi — vLLM local (heavy cho SRE/ops)."""
    ctx.fallback_inline_commands = None
    hint = ""
    if detail:
        hint = f"\n\n[Gợi ý nội bộ kỹ thuật: {reason} — {detail[:400]}]"
    blocks: list[str] = []
    try:
        infra_inj = await fetch_infra_injection_for_fallback(ctx, user_text)
        if infra_inj.strip():
            blocks.append(infra_inj.strip())
    except Exception as e:
        logger.debug("[%s] fetch_infra_injection_for_fallback: %s", trace, e)
    if (learned_context or "").strip():
        blocks.append(f"[learned_context / RAG action_experience]\n{learned_context.strip()[:3500]}")
    blocks.append(f"[user_message]\n{(user_text or '')[:8000]}")
    blocks.append(hint.strip())
    user_content = "\n\n".join(b for b in blocks if b.strip())
    # SRE/ops/reasoning/forecast → gemma3:27b; chat chung → tier-1 7B.
    fb_model = (
        ctx.settings.model_heavy_lifter
        if classify_route(user_text or "") != "default"
        else ctx.settings.chat_model
    )
    try:
        resp = await ctx.llm.chat(
            model=fb_model,
                messages=[
                {"role": "system", "content": ope.CONV_FALLBACK_SYSTEM_EN},
                {"role": "user", "content": user_content[:12000]},
            ],
            options=build_llm_options(ctx, temperature=0.1),
        )
        out = ((resp.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        logger.warning("[%s] conversational_fallback llm_error %s", trace, e)
        out = ""
    if not out:
        out = (
            "Tình trạng: chưa có snapshot infra trong context.\n\n"
            "Hành động tiếp theo (tự động): pipeline sẽ ưu tiên SDK (namespace_pods_top / inspect_pod_deep / metrics) "
            "— không cần user chọn lệnh."
        )
    display, cmds = _parse_suggestions_json_tail(out)
    if cmds and len(cmds) == 3 and getattr(ctx.settings, "fallback_inline_buttons_enabled", True):
        ctx.fallback_inline_commands = cmds
        out = display
    else:
        out = display if display else out
    logger.info("[%s] slow_path_conversational_fallback reason=%s", trace, reason)
    return out[:4000]


def _k8s_smart_target_hint(user_text: str) -> str | None:
    """Gợi ý LLM: discovery khi thiếu định danh; khi alert đã có pod+namespace thì inspect-first (không list)."""
    if not (user_text or "").strip():
        return None
    pod, ns = _parse_alert_pod_namespace_from_preview(user_text)
    if pod and ns:
        return (
            f"[K8S — alert scoped] pod={pod} namespace={ns}. "
            "Prefer `inspect_pod_deep` or `k8s_describe_resource` (Pod); **do not** open with "
            "`k8s_list_pods` / `namespace_pods_top` / `list_all_pods_sdk` for this single-pod investigation."
        )
    tl = user_text.lower()
    if not any(k in tl for k in ("pod", "pods", "namespace", "deployment", "rollout", "metric", "cpu", "ram")):
        return None
    return (
        "Routing K8s: use only names/namespace from the user/alert; map pod → `resolve_pod_identity` "
        "(pod_name/hint, namespace?); deployment → `resolve_deployment_identity`. "
        "List cluster only when you need a pod menu → `list_all_pods_sdk` / `k8s_list_pods` (god/lab: kubectl; else SDK). "
        "Do not guess namespace; tool names must be registered tools only."
    )


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


async def resolve_remediation_from_memory(
    ctx: WorkerHandlerContext,
    text: str,
    *,
    trace: str = "unknown",
    collection_name: str = COLLECTION_SOP,
    score_threshold: float | None = None,
) -> tuple[bool, str | None, str | None]:
    """Embed + RAG SOP hit với ``auto_execute`` → chạy tool. Không action_experience.

    Returns ``(ok, output, tool_name)`` — ``tool_name`` is set only when ``ok`` is True.
    """
    thr = score_threshold if score_threshold is not None else ctx.settings.rag_fast_path_score
    logger.info("[%s] remediation_embed collection=%s thr=%s", trace, collection_name, thr)
    emb_resp = await ctx.llm.embed(
        model=ctx.settings.embed_model,
        input=text[:8000],
    )
    vector = _embedding_from_response(emb_resp)
    resp = await ctx.vector_store.query_points(
        collection_name=collection_name,
        query=vector,
        limit=1,
        with_payload=True,
    )
    hits = resp.points or []
    if not hits:
        logger.info(json.dumps({"event": "rag_search", "similarity_score": 0.0}))
        logger.info("[%s] remediation_miss no_sop_hit", trace)
        return False, None, None
    hit = hits[0]
    score = getattr(hit, "score", None)
    score_f = float(score or 0.0)
    logger.info(json.dumps({"event": "rag_search", "similarity_score": round(score_f, 4)}))
    if score_f < float(thr):
        logger.info("[%s] remediation_miss score_below_threshold score=%.4f threshold=%.4f", trace, score_f, float(thr))
        return False, None, None
    if score is not None:
        logger.info(
            "[%s] remediation_sop_hit score=%.4f threshold=%.4f — bypassing LLM for tool execution",
            trace,
            float(score),
            float(thr),
        )
    payload = dict(hit.payload or {})
    if not payload.get("auto_execute"):
        logger.info("[%s] remediation_miss sop_no_auto", trace)
        return False, None, None
    tool_name = str(payload.get("tool") or "")
    if not tool_name or tool_name not in TOOL_REGISTRY:
        logger.info("[%s] remediation_miss bad_tool=%s", trace, tool_name)
        return False, None, None
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    fn = TOOL_REGISTRY[tool_name]
    out = await fn(ctx, args)
    if not get_tool_registry().has(tool_name):
        out = prepare_tool_return_for_llm(ctx, out)
    logger.info("[%s] remediation_ok tool=%s source=sop", trace, tool_name)
    if collection_name == COLLECTION_SOP and score_f > 0.9:
        inc_fastpath_hits()
    return True, out, tool_name


async def try_fast_path(
    ctx: WorkerHandlerContext,
    text: str,
    *,
    trace: str = "unknown",
) -> tuple[bool, str | None]:
    """Embed + RAG SOP; miss → action_experience (routing học từ slow-path). Không semaphore LLM."""
    ok, out, hit_tool = await resolve_remediation_from_memory(
        ctx,
        text,
        trace=trace,
        collection_name=COLLECTION_SOP,
        score_threshold=ctx.settings.rag_fast_path_score,
    )
    if ok:
        log_react_json(
            "v3_fast_path_hit",
            trace=trace,
            source="sop",
            tool=hit_tool or "",
            out_len=len(out or ""),
        )
        return True, out

    if not getattr(ctx.settings, "routing_experience_enabled", True):
        logger.info("[%s] fast_path_miss routing_disabled", trace)
        return False, None
    if not getattr(ctx.settings, "action_experience_enabled", True):
        logger.info("[%s] fast_path_miss action_exp_disabled", trace)
        return False, None

    emb_resp = await ctx.llm.embed(
        model=ctx.settings.embed_model,
        input=text[:8000],
    )
    vector = _embedding_from_response(emb_resp)
    r_resp = await ctx.vector_store.query_points(
        collection_name=COLLECTION_ACTION_EXPERIENCE,
        query=vector,
        limit=8,
        score_threshold=ctx.settings.routing_experience_score_threshold,
        with_payload=True,
    )
    for hit in r_resp.points or []:
        pl = dict(hit.payload or {})
        if pl.get("routing_source") not in ROUTING_SOURCES_FAST_PATH_EXECUTE:
            continue
        if not pl.get("auto_execute"):
            continue
        tool_name = str(pl.get("tool") or "")
        if not tool_name or tool_name not in TOOL_REGISTRY:
            continue
        args = pl.get("args") if isinstance(pl.get("args"), dict) else {}
        fn = TOOL_REGISTRY[tool_name]
        out = await fn(ctx, args)
        if not get_tool_registry().has(tool_name):
            out = prepare_tool_return_for_llm(ctx, out)
        log_react_json(
            "v3_fast_path_hit",
            trace=trace,
            source="routing_experience",
            tool=tool_name,
            out_len=len(out or ""),
        )
        logger.info("[%s] fast_path_ok tool=%s source=routing_experience", trace, tool_name)
        return True, out

    logger.info("[%s] fast_path_miss no_routing_hit", trace)
    return False, None


def _parse_tool_json(content: str) -> ToolCallPayload:
    s = content.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    data = json.loads(s)
    if isinstance(data, dict):
        # Một số model trả "params" thay vì "args" → ToolCallPayload.args rỗng → tool thiếu field.
        if "params" in data and not (isinstance(data.get("args"), dict) and data.get("args")):
            data = {**data, "args": data.get("params") or {}}
            data.pop("params", None)
    return ToolCallPayload.model_validate(data)


async def _repair_json_with_helper(
    ctx: WorkerHandlerContext,
    raw: str,
    *,
    parse_error: str = "",
) -> str:
    """HELPER (model_helper): sửa JSON tool từ model lớn; có thông báo lỗi parse từ Pydantic/json."""
    err = (parse_error or "").strip()
    user_blob = raw[:4000]
    if err:
        user_blob = (
            f"Lỗi parse JSON (sửa cho đúng schema tool):\n{err[:1500]}\n\n"
            f"Nội dung model trả về:\n{raw[:3500]}"
        )
    resp = await ctx.llm.chat(
        model=ctx.settings.model_helper,
        messages=[
            {
                "role": "system",
                "content": (
                    "Chỉ trả về một khối JSON hợp lệ duy nhất dạng "
                    '{"tool":"<ascii>","args":{...}}. Không markdown, không ```, không giải thích.'
                ),
            },
            {"role": "user", "content": user_blob[:8000]},
        ],
        options=build_llm_options(ctx, temperature=0.1),
    )
    return (resp.get("message") or {}).get("content") or ""


async def _compress_history(ctx: WorkerHandlerContext, state: SessionState, trace: str) -> str:
    """Qwen 1.5B — nén summary + recent thành last_summary mới."""
    blob = (state.last_summary or "").strip() + "\n\n" + json.dumps(state.recent_messages, ensure_ascii=False)
    resp = await ctx.llm.chat(
        model=ctx.settings.model_helper,
        messages=[
            {
                "role": "system",
                "content": (
                    "Compressor: tóm tắt lại thành một khối văn ngắn (tối đa 12 dòng), bullet. "
                    "Giữ số liệu, tên pod/host/namespace, quyết định user. Không lời dẫn."
                ),
            },
            {"role": "user", "content": blob[:8000]},
        ],
        options=build_llm_options(ctx, temperature=0.1),
    )
    out = ((resp.get("message") or {}).get("content") or "").strip()
    return out or state.last_summary


async def _deepseek_plan(ctx: WorkerHandlerContext, user_text: str, trace: str) -> str:
    """DeepSeek-r1:8b — lập kế hoạch bước; không thực thi tool."""
    resp = await ctx.llm.chat(
        model=ctx.settings.model_reasoning_engine,
        messages=[
            {
                "role": "system",
                "content": (
                    "Chỉ lập kế hoạch (Plan) đánh số 1. 2. 3. — không gọi tool, không JSON. "
                    "Tiếng Việt, ngắn."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Mục tiêu kỹ thuật:\n{user_text[:8000]}\n\n"
                    "Viết các bước thực hiện bằng SDK (psutil, Prometheus, inspect pod...)."
                ),
            },
        ],
        options=build_llm_options(ctx, temperature=0.1),
    )
    return (resp.get("message") or {}).get("content") or ""


async def _slow_path_abort_no_data(
    ctx: WorkerHandlerContext,
    user_text: str,
    trace: str,
    *,
    attempt_trace: list[AttemptRecord],
    exit_reason: str,
    last_detail: str = "",
) -> str:
    bucket = primary_bucket_for_metrics(attempt_trace)
    try:
        inc_slow_path_exhausted(exit_reason, bucket)
    except Exception as e:
        logger.debug("[%s] inc_slow_path_exhausted skip: %s", trace, e)
    await record_routing_exhausted_no_data(
        ctx,
        user_text,
        trace_id=trace,
        detail=last_detail or (attempt_trace[-1].one_line if attempt_trace else ""),
        attempt_trace=attempt_trace,
        exit_reason=exit_reason,
    )
    return format_slow_path_autopsy(
        max_attempts=ctx.settings.slow_path_max_tool_attempts,
        attempt_trace=attempt_trace,
        exit_reason=exit_reason,
    )


def _should_abort_stale(attempt_trace: list[AttemptRecord], streak_limit: int) -> bool:
    return consecutive_same_signature_streak(attempt_trace) >= streak_limit


async def slow_path_with_llm_and_tools(
    ctx: WorkerHandlerContext,
    user_text: str,
    *,
    trace: str = "unknown",
    session_summary: str = "",
    recent_turns: list[dict[str, str]] | None = None,
    needs_plan: bool = False,
    state: SessionState | None = None,
) -> str:
    """
    Slow-Path: semaphore một lần — (nén nếu turn_count > ngưỡng) → Plan (reasoning) nếu needs_plan
    → **gemma3:27b** thực thi JSON tool khi có [PLAN]; không plan thì model theo `dispatch_task`.
    Không gửi full history: chỉ [SUMMARY] + last 2 msgs + user hiện tại.
    """
    actx = await fetch_action_experience_context(ctx, user_text)
    logger.info("[%s] slow_path_acquire", trace)
    token = await ctx.semaphore.acquire()
    ctx.llm_slot_held = True
    try:
        if state is not None and state.turn_count > ctx.settings.compress_turn_threshold:
            state.last_summary = await _compress_history(ctx, state, trace)
            state.turn_count = 0
            logger.info("[%s] session_compressed", trace)

        execution_plan = ""
        if needs_plan:
            execution_plan = await _deepseek_plan(ctx, user_text, trace)
            logger.info("[%s] deepseek_plan len=%s", trace, len(execution_plan))

        use_executor_7b = bool(execution_plan.strip())

        messages: list[dict[str, Any]] = _slow_path_system_messages_for_ctx(ctx)
        if ctx.settings.baseline_snapshot_enabled:
            baseline_sys = await fetch_baseline_system_prompt(
                ctx.redis, ctx.settings.baseline_system_prompt_max_chars
            )
            if baseline_sys:
                messages.append({"role": "system", "content": baseline_sys})
        if actx:
            messages.append({"role": "system", "content": actx})
        if session_summary.strip():
            messages.append({"role": "system", "content": f"[SUMMARY]\n{session_summary.strip()}"})
        extra = _k8s_smart_target_hint(user_text)
        if extra:
            messages.append({"role": "system", "content": extra})
        if execution_plan.strip():
            messages.append({"role": "system", "content": f"[PLAN]\n{execution_plan.strip()}"})
        if recent_turns:
            for m in recent_turns:
                role = m.get("role")
                content = (m.get("content") or "").strip()
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        max_a = ctx.settings.slow_path_max_tool_attempts
        streak_limit = ctx.settings.slow_path_stale_signature_streak
        json_parse_failures = 0
        attempt_trace: list[AttemptRecord] = []
        anchor_idx = len(messages)

        for attempt in range(max_a):
            if attempt > 0:
                messages[anchor_idx:] = []
                messages.append(
                    {
                        "role": "user",
                        "content": build_slow_path_recovery_user_message(
                            user_text,
                            attempt_trace,
                            shell_allowed=shell_fast_path_enabled(ctx.settings),
                        ),
                    }
                )

            model = (
                ctx.settings.model_heavy_lifter
                if use_executor_7b
                else dispatch_task(
                    model_default=ctx.settings.chat_model,
                    model_reasoning=ctx.settings.model_reasoning_engine,
                    model_heavy=ctx.settings.model_heavy_lifter,
                    user_text=user_text,
                    attempt=attempt,
                    json_parse_failures=json_parse_failures,
                )
            )
            logger.info("[%s] slow_path_chat_attempt n=%s/%s model=%s", trace, attempt + 1, max_a, model)
            # Slow-path request to LLM (RAG score miss path).
            inc_llm_requests()
            resp = await ctx.llm.chat(
                model=model,
                messages=messages,
                options=build_llm_options(ctx, temperature=0.1),
            )
            content = (resp.get("message") or {}).get("content") or ""
            if not content.strip():
                logger.warning("[%s] slow_path empty model output attempt=%s", trace, attempt + 1)
                sig = slow_path_error_signature("empty_model", "")
                attempt_trace.append(
                    AttemptRecord(
                        attempt=attempt + 1,
                        phase="empty_model",
                        error_signature=sig,
                        one_line="empty model output",
                        detail_full="empty_model_output",
                    )
                )
                if _should_abort_stale(attempt_trace, streak_limit):
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="stale_signature",
                        last_detail="empty_model_output",
                    )
                if attempt >= max_a - 1:
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="max_attempts",
                        last_detail="empty_model_output",
                    )
                continue

            call: ToolCallPayload | None = None
            text = content
            last_parse_err: str = ""
            try:
                max_rep = ctx.settings.json_repair_max
                for repair_i in range(max_rep + 1):
                    try:
                        call = _parse_tool_json(text)
                        break
                    except Exception as e:
                        last_parse_err = str(e)
                        if repair_i >= max_rep:
                            raise
                        text = await _repair_json_with_helper(
                            ctx, content, parse_error=last_parse_err
                        )
            except Exception as e2:
                json_parse_failures += 1
                last_tool_error = f"parse JSON: {last_parse_err or e2!s}; helper exhausted: {e2!s}"
                sig = slow_path_error_signature("parse", last_tool_error)
                attempt_trace.append(
                    AttemptRecord(
                        attempt=attempt + 1,
                        phase="parse",
                        error_signature=sig,
                        one_line=truncate_for_prompt(last_tool_error, 180),
                        detail_full=truncate_for_prompt(last_tool_error, 720),
                    )
                )
                if _should_abort_stale(attempt_trace, streak_limit):
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="stale_signature",
                        last_detail=last_tool_error,
                    )
                if attempt >= max_a - 1:
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="max_attempts",
                        last_detail=last_tool_error,
                    )
                continue
            assert call is not None
            fn = TOOL_REGISTRY.get(call.tool)
            if not fn:
                raw_in = (getattr(ctx, "inbound_user_text", None) or "").strip()
                try:
                    rescue = await try_autonomous_sdk_route(ctx, raw_in or user_text)
                except Exception as e:
                    logger.debug("[%s] autonomous_sdk rescue skip: %s", trace, e)
                    rescue = None
                if rescue is not None:
                    logger.info("[%s] unknown_tool rescued by autonomous_sdk", trace)
                    return rescue
                bad_tool = str(call.tool)
                last_fail_detail = f"unknown_tool:{bad_tool}"
                sig = slow_path_error_signature("unknown_tool", "", tool=bad_tool)
                attempt_trace.append(
                    AttemptRecord(
                        attempt=attempt + 1,
                        phase="unknown_tool",
                        error_signature=sig,
                        one_line=f"unknown tool `{bad_tool}`",
                        detail_full=last_fail_detail,
                        tool=bad_tool,
                    )
                )
                logger.warning("[%s] unknown_tool name=%s → trace+retry", trace, bad_tool)
                if _should_abort_stale(attempt_trace, streak_limit):
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="stale_signature",
                        last_detail=last_fail_detail,
                    )
                if attempt >= max_a - 1:
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="max_attempts",
                        last_detail=last_fail_detail,
                    )
                continue
            try:
                out = await fn(ctx, call.args)
                if not get_tool_registry().has(call.tool):
                    out = prepare_tool_return_for_llm(ctx, out)
                logger.info("[%s] slow_path_tool_ok tool=%s", trace, call.tool)
                await record_routing_from_success(
                    ctx,
                    tool=call.tool,
                    args=call.args,
                    trace_id=trace,
                )
                inc_experience_saved()
                return out
            except Exception as e:
                last_tool_error = repr(e)
                sig = slow_path_error_signature("tool_error", last_tool_error, tool=call.tool)
                ak = call.args if isinstance(call.args, dict) else {}
                args_keys = tuple(sorted(ak.keys()))[:24]
                attempt_trace.append(
                    AttemptRecord(
                        attempt=attempt + 1,
                        phase="tool_error",
                        error_signature=sig,
                        one_line=truncate_for_prompt(last_tool_error, 180),
                        detail_full=truncate_for_prompt(last_tool_error, 720),
                        tool=call.tool,
                        args_keys=args_keys,
                    )
                )
                if _should_abort_stale(attempt_trace, streak_limit):
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="stale_signature",
                        last_detail=last_tool_error,
                    )
                if attempt >= max_a - 1:
                    return await _slow_path_abort_no_data(
                        ctx,
                        user_text,
                        trace,
                        attempt_trace=attempt_trace,
                        exit_reason="max_attempts",
                        last_detail=last_tool_error,
                    )
        return await _slow_path_abort_no_data(
            ctx,
            user_text,
            trace,
            attempt_trace=attempt_trace,
            exit_reason="loop_exit",
            last_detail=attempt_trace[-1].one_line if attempt_trace else "loop_exit",
        )
    finally:
        ctx.llm_slot_held = False
        await ctx.semaphore.release(token)


async def handle_inbound_payload(ctx: WorkerHandlerContext, payload: dict[str, Any]) -> str:
    """State machine: session_state Redis → clarification → summary+last2 → Plan → tool. Không full history."""
    trace = str(payload.get("trace_id") or "").strip()
    if not trace:
        trace = str(uuid.uuid4())
        payload["trace_id"] = trace
    ctx.inbound_trace_id = trace
    src = str(payload.get("source") or "")
    eff_preview = _effective_inbound_text_preview(payload)
    tok = push_trace_id(trace)
    t0 = time.perf_counter()
    log_start_request(
        trace,
        phase="inbound_handler",
        source=src or "unknown",
        chat_id=payload.get("chat_id"),
        text_len=len(eff_preview),
    )
    if src == "prometheus" and eff_preview:
        logger.info("[%s] prometheus_inbound_text preview=%s", trace, redact(eff_preview[:2000]))
    err: BaseException | None = None
    out: str | None = None
    try:
        with inbound_trace_span(trace, name="inbound_handler"):
            out = await _handle_inbound_payload_impl(ctx, payload, trace)
        if src == "prometheus" and out is not None:
            logger.info("[%s] prometheus_inbound_out preview=%s", trace, redact((out or "")[:2000]))
        return out
    except BaseException as e:
        err = e
        raise
    finally:
        pop_trace_id(tok)
        ms = (time.perf_counter() - t0) * 1000.0
        log_end_request(
            trace,
            phase="inbound_handler",
            status="error" if err else "ok",
            duration_ms=ms,
            error=(f"{type(err).__name__}: {err}" if err else None),
        )


async def _handle_inbound_payload_impl(
    ctx: WorkerHandlerContext,
    payload: dict[str, Any],
    trace: str,
) -> str:
    """Luồng xử lý tin nhắn — ``trace_id`` đã gắn trên ``payload`` và ``ctx.inbound_trace_id``."""
    src = str(payload.get("source") or "")
    ctx.inbound_source = src
    raw_user_text = _effective_inbound_text_preview(payload)

    ctx.fallback_inline_commands = None
    if not raw_user_text:
        return "Không có nội dung text."

    if not ctx.scout_ready.is_set():
        return "Em đang hoàn tất Deep Scout baseline — đại ca thử lại sau vài giây."

    chat_id_raw = payload.get("chat_id")
    chat_id_int: int | None = int(chat_id_raw) if chat_id_raw is not None else None
    ctx.telegram_chat_id = chat_id_int
    ctx.inbound_user_text = raw_user_text
    ctx.restart_rollout_explicit = bool(RE_RESTART_ROLLOUT_EXPLICIT.search(raw_user_text))
    ctx.pod_discovery_pairs = []
    inc_messages_processed(src or "unknown")
    _hc_record_msg()

    if chat_id_int is not None:
        with child_span("redis_get_write_pending"):
            wp_raw = await ctx.redis.get(redis_key_write_pending(chat_id_int))
        if wp_raw and _user_confirms_rollout_telegram(raw_user_text):
            try:
                from services.audit_ledger.chain_writer import write_audit_block
                from services.audit_ledger.crat_event_types import CRAT_EVENT_MUTATION_ENQUEUED
                from services.audit_ledger.signer import AuditLedgerError
                wdata = json.loads(wp_raw)
                try:
                    await write_audit_block(
                        event_type=CRAT_EVENT_MUTATION_ENQUEUED,
                        trace_id=trace,
                        payload={"trace_id": trace, "action": "write_pending", "source": "telegram_confirm"},
                        redis=ctx.redis,
                        kafka=getattr(ctx, "kafka", None),
                        kafka_topic=getattr(ctx.settings, "kafka_topic_audit_chain", "omni-audit-chain"),
                    )
                except AuditLedgerError as _crat_err:
                    logger.critical("[%s] write_pending_crat_failed err=%s FAIL_CLOSED", trace, _crat_err)
                    return "[DATA] error\n[DIAGNOSIS] Audit write failed — action blocked (CRAT fail-closed)"
                out = await execute_write_pending_from_redis(ctx, wdata)
                with child_span("redis_del_write_pending"):
                    await ctx.redis.delete(redis_key_write_pending(chat_id_int))
                logger.info("[%s] write_pending_confirmed", trace)
                return out
            except Exception as e:
                with child_span("redis_del_write_pending_on_error"):
                    await ctx.redis.delete(redis_key_write_pending(chat_id_int))
                logger.exception("[%s] write_pending_failed", trace)
                return f"[DATA] error\n[DIAGNOSIS] Write pending: {e!s}"

        with child_span("redis_get_rollout_pending"):
            pend_raw = await ctx.redis.get(redis_key_rollout_pending(chat_id_int))
        if pend_raw and _user_confirms_rollout_telegram(raw_user_text):
            try:
                from services.audit_ledger.chain_writer import write_audit_block
                from services.audit_ledger.crat_event_types import CRAT_EVENT_MUTATION_ENQUEUED
                from services.audit_ledger.signer import AuditLedgerError
                data = json.loads(pend_raw)
                try:
                    await write_audit_block(
                        event_type=CRAT_EVENT_MUTATION_ENQUEUED,
                        trace_id=trace,
                        payload={"trace_id": trace, "action": "rollout_restart", "source": "telegram_confirm"},
                        redis=ctx.redis,
                        kafka=getattr(ctx, "kafka", None),
                        kafka_topic=getattr(ctx.settings, "kafka_topic_audit_chain", "omni-audit-chain"),
                    )
                except AuditLedgerError as _crat_err:
                    logger.critical("[%s] rollout_crat_failed err=%s FAIL_CLOSED", trace, _crat_err)
                    return "[DATA] error\n[DIAGNOSIS] Audit write failed — action blocked (CRAT fail-closed)"
                out = await execute_rollout_restart_from_pending(ctx, data)
                with child_span("redis_del_rollout_pending"):
                    await ctx.redis.delete(redis_key_rollout_pending(chat_id_int))
                logger.info("[%s] rollout_confirmed_executed", trace)
                return out
            except Exception as e:
                with child_span("redis_del_rollout_pending_on_error"):
                    await ctx.redis.delete(redis_key_rollout_pending(chat_id_int))
                logger.exception("[%s] rollout_confirm_failed", trace)
                return f"[DATA] error\n[DIAGNOSIS] Rollout sau xác nhận: {e!s}"

    state = SessionState()
    if chat_id_int is not None:
        with child_span("load_session"):
            state = await load_session(ctx.redis, chat_id_int)
        state.turn_count += 1

    needs_plan = False

    if chat_id_int is not None and state.monitoring_target_type == "host" and _wants_host_vm_chart(
        state, raw_user_text
    ):
        dur = _extract_duration(raw_user_text)
        args = {
            "target_type": "host",
            "intent": resolve_intent_from_keywords(raw_user_text),
            "duration": dur,
        }
        with child_span("tool_query_prometheus_metrics", tool_name="query_prometheus_metrics"):
            out = await TOOL_REGISTRY["query_prometheus_metrics"](ctx, args)
        state.recent_messages.append({"role": "user", "content": raw_user_text})
        state.recent_messages.append({"role": "assistant", "content": out})
        state.recent_messages = state.recent_messages[-4:]
        await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] handler_done host_vm_chart", trace)
        return out

    if RE_LIST_ALL_PODS_CHAT.search(raw_user_text):
        with child_span("tool_list_all_pods_sdk", tool_name="list_all_pods_sdk"):
            out = await TOOL_REGISTRY["list_all_pods_sdk"](ctx, {})
        if chat_id_int is not None:
            pairs = getattr(ctx, "pod_discovery_pairs", []) or []
            if pairs:
                state.last_pod_discovery = [{"namespace": a, "name": b} for a, b in pairs[:800]]
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": out})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] handler_done list_all_pods_sdk out_len=%s", trace, len(out))
        return out

    try:
        with child_span("autonomous_sdk_route"):
            auto_sdk = await try_autonomous_sdk_route(ctx, raw_user_text)
    except Exception as e:
        logger.debug("[%s] autonomous_sdk_route skip: %s", trace, e)
        auto_sdk = None
    if auto_sdk is not None:
        if chat_id_int is not None:
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": auto_sdk})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] handler_done autonomous_sdk out_len=%s", trace, len(auto_sdk))
        return auto_sdk

    if chat_id_int is not None and state.pending_action == PENDING_AWAIT_VM_SLOTS:
        merged = merge_vm_slots(state.accumulated_vm_slots, raw_user_text)
        merged = enrich_slots_from_discovery(merged, state.last_pod_discovery)
        llm_ent = await extract_entities_llm(ctx, raw_user_text)
        merged = merge_llm_entities_into_slots(merged, llm_ent)
        parsed = parse_resource_followup(raw_user_text)
        if parsed and parsed[0] == "pod":
            state.monitoring_target_type = "pod"
            merged["target_type"] = "pod"
        elif followup_indicates_host(raw_user_text) or (parsed and parsed[0] == "host"):
            merged["target_type"] = "host"
        state.accumulated_vm_slots = merged

        if followup_indicates_host(raw_user_text) or (parsed and parsed[0] == "host"):
            state.pending_action = ""
            state.accumulated_vm_slots = {}
            state.monitoring_target_type = "host"
            with child_span("tool_system_psutil", tool_name="system_psutil"):
                ps_out = await TOOL_REGISTRY["system_psutil"](ctx, {})
            out = (
                ps_out
                + "\n\n[CONTEXT: đo Host (psutil). Chart/VM sau dùng métric node/host; session target=host]"
            )
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": out})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
            logger.info("[%s] vm_slots_host_psutil", trace)
            return out
        if vm_slots_ready(merged):
            state.pending_action = ""
            state.accumulated_vm_slots = {}
            tt = str(merged.get("target_type") or "").strip().lower()
            if tt == "host":
                state.monitoring_target_type = "host"
            elif tt == "pod":
                state.monitoring_target_type = "pod"
            args = vm_slots_to_tool_args(merged, ctx)
            with child_span("tool_query_prometheus_metrics", tool_name="query_prometheus_metrics"):
                out = await TOOL_REGISTRY["query_prometheus_metrics"](ctx, args)
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": out})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
            logger.info("[%s] handler_done vm_slots_tool out_len=%s", trace, len(out))
            return out
        state.last_goal = state.last_goal or raw_user_text
        nudge = nudge_vm_slots_message(merged)
        state.recent_messages.append({"role": "user", "content": raw_user_text})
        state.recent_messages.append({"role": "assistant", "content": nudge})
        state.recent_messages = state.recent_messages[-4:]
        await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] vm_slots_need_more", trace)
        return nudge

    _hints = _preflight_hints_from_inbound(payload, raw_user_text, src)
    with child_span("rag_gate"):
        gate_out = await evaluate_rag_gate(ctx, raw_user_text, hints=_hints, trace=trace)
    if gate_out.hit and (gate_out.formatted or "").strip():
        out = ope.truncate_plain_text_to_max_words(
            gate_out.formatted.strip(),
            max_words=effective_reply_max_words(ctx.settings),
        )
        if chat_id_int is not None:
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": out})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] handler_done rag_gate_hit out_len=%s", trace, len(out))
        return out

    learned = await preflight_infra_kb(
        ctx,
        raw_user_text,
        hints=_hints,
    )
    ambiguous = (
        is_ambiguous_resource_check(raw_user_text, state, learned=learned)
        and "[CONTEXT:" not in raw_user_text
    )
    if ambiguous and state.pending_action != PENDING_AWAIT_VM_SLOTS:
        slots = extract_vm_slots_from_text(raw_user_text)
        slots = enrich_slots_from_discovery(slots, state.last_pod_discovery)
        nudge = nudge_vm_slots_message(slots)
        if chat_id_int is not None:
            state.last_goal = raw_user_text
            state.pending_action = PENDING_AWAIT_VM_SLOTS
            state.accumulated_vm_slots = slots
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": nudge})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
            logger.info("[%s] vm_slots_started chat_id=%s", trace, chat_id_int)
        else:
            logger.info("[%s] vm_slots_no_chat_id", trace)
        return nudge

    try:
        with child_span("enrich_working_text_with_infra"):
            working_text = await enrich_working_text_with_infra(ctx, raw_user_text, learned=learned)
    except Exception as e:
        logger.debug("[%s] infra_context skip: %s", trace, e)
        working_text = raw_user_text

    recent_slice = state.recent_messages[-2:] if chat_id_int is not None else None

    with child_span("fast_path"):
        fast_ok, fast_out = await try_fast_path(ctx, raw_user_text, trace=trace)
    if fast_ok:
        out = _cap_inbound_user_reply(fast_out or "OK (fast-path).", ctx)
        if chat_id_int is not None:
            state.recent_messages.append({"role": "user", "content": raw_user_text})
            state.recent_messages.append({"role": "assistant", "content": out})
            state.recent_messages = state.recent_messages[-4:]
            await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
        logger.info("[%s] handler_done fast out_len=%s", trace, len(out))
        return out

    if ctx.settings.agentic_slow_path_enabled:
        from workers.agentic_slow_path import agentic_slow_path_with_llm_and_tools

        unattended_alert = chat_id_int is None and src == "prometheus"
        with child_span("agentic_slow_path"):
            out = await agentic_slow_path_with_llm_and_tools(
                ctx,
                working_text,
                trace=trace,
                session_summary=state.last_summary if chat_id_int is not None else "",
                recent_turns=recent_slice,
                needs_plan=needs_plan,
                state=state if chat_id_int is not None else None,
                unattended_alert=unattended_alert,
            )
    else:
        with child_span("classic_slow_path"):
            out = await slow_path_with_llm_and_tools(
                ctx,
                working_text,
                trace=trace,
                session_summary=state.last_summary if chat_id_int is not None else "",
                recent_turns=recent_slice,
                needs_plan=needs_plan,
                state=state if chat_id_int is not None else None,
            )
    out = _cap_inbound_user_reply(out, ctx)
    if chat_id_int is not None:
        state.recent_messages.append({"role": "user", "content": raw_user_text})
        state.recent_messages.append({"role": "assistant", "content": out})
        state.recent_messages = state.recent_messages[-4:]
        await save_session(ctx.redis, chat_id_int, state, ttl_sec=ctx.settings.session_ttl_sec)
    logger.info("[%s] handler_done slow out_len=%s", trace, len(out))
    return out
