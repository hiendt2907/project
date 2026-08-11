"""Điểm kiểm dùng chung cho "đã biết cách sửa, không cần LLM" — cả proactive
cluster (Prometheus) lẫn proactive remote-host (baseline z-score/ngưỡng tĩnh,
không có Prometheus phía khách) đều phải qua đây trước khi coi một match từ
`action_experience` là đủ tin để THỰC THI THẲNG một tool mutate.

## Vì sao tồn tại (lỗi thật, production, 2026-08-03)

`_resolve_from_action_experience` (`workers/proactive_observer.py`) từng nhận
`args` verbatim từ payload RAG rồi thực thi ngay — production đã dispatch
`k8s_rollout_restart` với `deployment='<valid_deployment>'` (placeholder chưa
điền, độ giống ~0.7 đủ vượt `score_threshold`). K8s tự chặn vì tên không tồn
tại — MAY MẮN, không phải nhờ có gate nào. `rag/redis_brain.py` có
`_is_noise_payload()` bảo vệ LÀN REACTIVE khỏi đúng loại rác này, nhưng
KHÔNG áp dụng được nguyên xi cho làn proactive: `_is_noise_payload()` từ chối
CATEGORICALLY mọi payload có `routing_source=="proactive_fallback"` — đúng
cho một bên NGOÀI tiêu thụ tri thức của proactive (reactive), nhưng nếu áp
lại cho chính proactive tự đọc ghi chú của nó thì mọi bản ghi
`_save_proactive_learning_record` (chính nó gắn `routing_source:
"proactive_fallback"`) sẽ không bao giờ được nhớ lại nữa — xoá sổ tính năng
tự học. Vì vậy module này viết lại hai lớp kiểm hẹp hơn, đúng cho người ĐỌC
LẠI CHÍNH GHI CHÚ CỦA MÌNH thay vì người ngoài tiêu thụ.

## Hai lớp kiểm, độc lập với điểm similarity

1. `_has_placeholder` — literal template token (`<...>`) trong bất kỳ giá trị
   chuỗi nào của `args`. Cùng quy ước với `_is_noise_payload()`
   (`startswith("<") and endswith(">")`) để không tạo thêm một định nghĩa
   "placeholder" thứ hai lệch nhau trong cùng codebase.
2. `_out_of_scope` — đối chiếu identifier tài nguyên (`unit`/`service`/
   `deployment`/`name`/`container`) với PHẠM VI HOST hiện tại nếu caller
   truyền `host_scope` (tập tên có thật, ví dụ lấy từ discovery snapshot của
   agent). Một memory học được trên host A không được replay mù trên host B
   chỉ vì điểm giống cao — vector similarity đo NGỮ NGHĨA câu hỏi, không đo
   việc tài nguyên đó có tồn tại trên host đang xử lý hay không.

`host_scope=None` (mặc định) chỉ tắt lớp (2), không tắt lớp (1) — dùng khi
caller chưa có cách liệt kê tài nguyên thật của mục tiêu (ví dụ: proactive
cluster hiện chưa có cluster-inventory context, xem kế hoạch Đ7).

Không dừng ở ứng viên top-1 nếu nó bị hai lớp trên từ chối — thử tiếp các
ứng viên xếp sau (tăng ĐỘ CHÍNH XÁC thay vì bỏ cuộc ngay khi top-1 là rác).
"""
from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# Khoá tài nguyên coi là "phải khớp phạm vi host" khi host_scope được truyền.
SCOPED_ARG_KEYS: frozenset[str] = frozenset(
    {"unit", "service", "deployment", "name", "container"}
)


class KnownFixResult(NamedTuple):
    ok: bool
    output: str | None
    tool: str | None
    meta: dict[str, Any]
    rejected_reason: str = ""


class KnownFixCandidate(NamedTuple):
    """Một ứng viên đã qua CẢ HAI lớp kiểm — chưa thực thi. Tách khỏi
    `resolve_known_fix` vì cách THỰC THI khác nhau tuỳ đích: cluster gọi hàm
    Python tại chỗ (`TOOL_REGISTRY[tool](ctx, args)`), remote-agent phải qua
    kênh lệnh bền (`auto_recovery_bridge.dispatch_if_eligible`) — không có API
    trong-process nào để gọi thẳng một VM khách."""

    tool: str
    args: dict[str, Any]
    score: float


def _has_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("<") and value.endswith(">")
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_placeholder(v) for v in value)
    return False


def _out_of_scope(args: dict[str, Any], host_scope: frozenset[str] | None) -> str | None:
    """Trả tên field vi phạm phạm vi host, hoặc None nếu qua hết / không có scope để so."""
    if host_scope is None:
        return None
    for key in SCOPED_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val and val not in host_scope:
            return key
    return None


async def find_known_fix_candidate(
    ctx: Any,
    *,
    query_text: str,
    score_threshold: float,
    host_scope: frozenset[str] | None = None,
    valid_tools: Any = None,
    tenant_id: str | None = None,
) -> tuple[KnownFixCandidate | None, str]:
    """Vector search trong `action_experience`, trả ứng viên ĐẦU TIÊN qua được
    cả hai lớp kiểm — KHÔNG thực thi (xem `KnownFixCandidate`). Trả
    `(None, rejected_reason)` nếu không ứng viên nào qua được.

    `valid_tools`: universe tên tool hợp lệ cho use-case của caller — mặc định
    `TOOL_REGISTRY` (tool trong-process, dùng cho cluster). Remote-agent phải
    truyền `auto_recovery_bridge._SUPPORTED_CAPABILITIES` vì đó là universe
    tên hoàn toàn khác (`systemd.restart_unit`, không phải `k8s_rollout_restart`).

    `tenant_id`: bắt buộc truyền cho làn remote-agent (mỗi tenant một khách
    hàng thật) — thiếu tham số này thì `action_experience` là MỘT pool DÙNG
    CHUNG cho mọi tenant (đã xác nhận đây là hành vi thật trước fix này,
    2026-08-11: fix cho payment-api học được ở tenant A bị recall lại và SUÝT
    tự thực thi cho tenant B, chỉ chặn được nhờ ngưỡng confidence, không phải
    nhờ cách ly). `None`/thiếu → tenant mặc định (`DEFAULT_TENANT_ID`), giữ
    tương thích ngược cho `resolve_known_fix()` (làn K8s cluster nội bộ, chưa
    có khái niệm tenant khách hàng).
    """
    from execution.memory_normalize import canonical_symptom_text
    from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, EMBED_DIM
    from rag.redis_vector_store import scoped_collection_name

    if valid_tools is None:
        from workers.tools import TOOL_REGISTRY as valid_tools  # noqa: N813

    collection_name = scoped_collection_name(COLLECTION_ACTION_EXPERIENCE, tenant_id or "default")

    try:
        strip_pods = bool(getattr(ctx.settings, "memory_canonical_strip_pods", True))
        q = canonical_symptom_text((query_text or "").strip()[:4000], strip_pods=strip_pods)
        emb = await ctx.llm.embed(model=ctx.settings.embed_model, input=q[:4000])
        vec = embedding_from_response(emb)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
        resp = await ctx.vector_store.query_points(
            collection_name=collection_name,
            query=vec,
            limit=3,
            score_threshold=score_threshold,
            with_payload=True,
        )
    except Exception as e:
        logger.debug("known_fix_resolver: search skip: %s", e)
        return None, "search_error"

    if resp.points:
        top_score = float(resp.points[0].score or 0.0)
        logger.info(json.dumps({"event": "rag_search", "similarity_score": round(top_score, 4)}))

    rejected_reason = "no_candidate"
    for pt in resp.points or []:
        pl = dict(pt.payload or {})
        if str(pl.get("exec_outcome") or "").lower() != "success":
            continue
        if not bool(pl.get("auto_execute", True)):
            continue
        tool_name = str(pl.get("tool") or "")
        args = pl.get("args") if isinstance(pl.get("args"), dict) else {}
        if not tool_name or tool_name not in valid_tools:
            continue
        if _has_placeholder(args):
            logger.warning(
                "known_fix_resolver: candidate rejected (placeholder) tool=%s score=%.3f",
                tool_name, float(pt.score or 0.0),
            )
            rejected_reason = "placeholder_args"
            continue
        violated_key = _out_of_scope(args, host_scope)
        if violated_key is not None:
            logger.warning(
                "known_fix_resolver: candidate rejected (out of host scope, key=%s) tool=%s score=%.3f",
                violated_key, tool_name, float(pt.score or 0.0),
            )
            rejected_reason = "out_of_host_scope"
            continue

        return KnownFixCandidate(tool=tool_name, args=args, score=float(pt.score or 0.0)), "ok"

    return None, rejected_reason


async def resolve_known_fix(
    ctx: Any,
    *,
    query_text: str,
    score_threshold: float,
    host_scope: frozenset[str] | None = None,
) -> KnownFixResult:
    """Tìm rồi thực thi TẠI CHỖ (`TOOL_REGISTRY[tool](ctx, args)`) — chỉ đúng
    cho tài nguyên trong cụm có API Python gọi trực tiếp được. Remote-agent
    dùng `find_known_fix_candidate()` + `workers.remote_known_fix` thay vì
    hàm này (xem docstring `KnownFixCandidate`)."""
    from workers.tool_observation import prepare_tool_return_for_llm
    from workers.tool_registry import get_tool_registry
    from workers.tools import TOOL_REGISTRY

    candidate, reason = await find_known_fix_candidate(
        ctx, query_text=query_text, score_threshold=score_threshold,
        host_scope=host_scope, valid_tools=TOOL_REGISTRY,
    )
    if candidate is None:
        return KnownFixResult(False, None, None, {}, rejected_reason=reason)

    fn = TOOL_REGISTRY[candidate.tool]
    out = await fn(ctx, candidate.args)
    if not get_tool_registry().has(candidate.tool):
        out = prepare_tool_return_for_llm(ctx, out)
    return KnownFixResult(
        True, str(out), candidate.tool, {"score": candidate.score, "args": candidate.args}
    )


def embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    return []
