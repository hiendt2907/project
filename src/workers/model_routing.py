"""Model routing: Tier-1 default vs reasoning vs heavy vs JSON helper."""

from __future__ import annotations

import re
from typing import Literal

RouteKind = Literal["default", "reasoning", "heavy"]


def classify_route(user_text: str) -> RouteKind:
    """
    Thứ tự: **heavy** (forecast/arch) → **reasoning** (tại sao/RCA) → **heavy** (ops/infra) → default.
    (Tránh từ khóa ops như `pod` che mất câu hỏi *tại sao* — reasoning được kiểm tra trước khối ops.)
    """
    t = user_text.lower()

    if re.search(
        r"dự\s*báo|forecast|forecasting|phương\s*án\s*kiến\s*trúc|kiến\s*trúc\s*hệ\s*thống|"
        r"lên\s*phương\s*án|architecture\s*plan|system\s*design",
        t,
        re.I,
    ):
        return "heavy"

    if re.search(
        r"tại\s*sao|vì\s*sao|\bwhy\b|phân\s*tích\s*lỗi|root\s*cause|nguyên\s*nhân|"
        r"analyze\s*error|post-?mortem",
        t,
        re.I,
    ):
        return "reasoning"

    # Ops / SRE / infra — luôn qua model_heavy (gemma3:27b) để JSON tool ổn định hơn tier-1 7B.
    if re.search(
        r"kiểm tra|check\b|health|metrics|redis|pgvector|postgres|pod|cpu|ram|disk|"
        r"chart|promql|k8s|kubectl|namespace|slowlog|oom|telegram|biểu đồ|"
        r"lệnh|command|debug|log|sự\s*cố|triển\s*khai|deploy|ingress|service|node|cluster",
        t,
        re.I,
    ):
        return "heavy"

    return "default"


def dispatch_task(
    *,
    model_default: str,
    model_reasoning: str,
    model_heavy: str,
    user_text: str,
    attempt: int,
    json_parse_failures: int,
) -> str:
    """
    Chọn model cho một vòng chat.

    - Sau **2 lần** parse JSON thất bại (helper cũng fail): dùng HEAVY (gemma3:27b) — tránh thinking-tags
      của reasoning model làm hỏng JSON.
    - Ngược lại: heavy / reasoning / default theo `classify_route`.
    """
    if json_parse_failures >= 2:
        return model_heavy

    route = classify_route(user_text)
    if route == "heavy":
        return model_heavy
    if route == "reasoning":
        return model_reasoning
    return model_default
