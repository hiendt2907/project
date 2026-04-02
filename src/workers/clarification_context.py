"""Hiểu NGỮ CẢNH câu trả lời sau clarification — không phụ thuộc bắt từng từ cố định."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MonitoringFollowupLLM(BaseModel):
    """Kết quả phân loại từ helper (JSON)."""

    target: Literal["host", "pod", "namespace", "unclear"] = Field(
        description="Phạm vi user chọn: host | pod | namespace | unclear"
    )
    pod_name: str | None = Field(default=None, description="Tên hoặc prefix pod nếu có")
    namespace: str | None = Field(default=None, description="Namespace K8s nếu user nhắc")


def _strip_json_fence(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


async def interpret_monitoring_followup_llm(
    ctx: Any,
    *,
    last_user_goal: str,
    bot_question: str,
    user_reply: str,
    recent_dialog_snippet: str = "",
) -> MonitoringFollowupLLM | None:
    """
    Dùng model_helper (~1.5B) đọc **ngữ cảnh** (mục tiêu ban đầu + hội thoại gần + câu trả lời).
    Không dùng danh sách từ khóa cứng.
    """
    ollama = getattr(ctx, "ollama", None)
    settings = getattr(ctx, "settings", None)
    if ollama is None or settings is None:
        return None
    model = getattr(settings, "model_helper", None) or "qwen2.5:1.5b"
    keep = getattr(settings, "ollama_keep_alive", "5m")

    snippet = (recent_dialog_snippet or "").strip()
    if len(snippet) > 2000:
        snippet = snippet[-2000:]

    user_block = (
        f"Câu / ý ban đầu của user (giám sát CPU/RAM…):\n{last_user_goal.strip()}\n\n"
        f"Bot vừa hỏi để chọn phạm vi:\n{bot_question.strip()}\n\n"
        f"Câu user trả lời **bây giờ**:\n{user_reply.strip()}\n"
    )
    if snippet:
        user_block += f"\nNgữ cảnh hội thoại gần (tin nhắn trước, tóm tắt):\n{snippet}\n"

    user_block += (
        "\nNhiệm vụ: hiểu user đang chọn **Host/node**, **một Pod**, hay **cả Namespace**; "
        "có nhắc **tên pod** hoặc **namespace** thì trích ra. "
        "Không cần khớp chữ cố định — suy từ ý nghĩa."
    )

    try:
        resp = await ollama.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Chỉ trả về một khối JSON hợp lệ, không markdown, không giải thích. "
                        'Schema: {"target":"host"|"pod"|"namespace"|"unclear",'
                        '"pod_name":null hoặc string, "namespace":null hoặc string}. '
                        "target=unclear khi không đủ thông tin để biết phạm vi."
                    ),
                },
                {"role": "user", "content": user_block[:12000]},
            ],
            options={"temperature": 0.0},
            keep_alive=keep,
        )
        raw = (resp.get("message") or {}).get("content") or ""
        cleaned = _strip_json_fence(raw)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return None
        return MonitoringFollowupLLM.model_validate(data)
    except Exception as e:
        logger.warning("interpret_monitoring_followup_llm failed: %s", e)
        return None


def format_session_snippet_for_llm(
    *,
    last_summary: str,
    recent_messages: list[dict[str, str]],
) -> str:
    """Gộp summary + vài tin gần để helper đọc ngữ cảnh."""
    lines: list[str] = []
    s = (last_summary or "").strip()
    if s:
        lines.append(f"[summary] {s[:900]}")
    for m in recent_messages[-6:]:
        role = m.get("role", "?")
        content = (m.get("content") or "")[:500]
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else ""


def followup_llm_to_merge_params(
    m: MonitoringFollowupLLM,
) -> tuple[str, str | None, str | None] | None:
    """
    (target_kind, pod_detail, namespace_hint) cho merge_clarification_context.
    unclear → None.
    """
    if m.target == "unclear":
        return None
    if m.target == "host":
        return ("host", None, m.namespace)
    if m.target == "namespace":
        return ("namespace", None, m.namespace)
    if m.target == "pod":
        pn = (m.pod_name or "").strip() or None
        return ("pod", pn, m.namespace)
    return None
