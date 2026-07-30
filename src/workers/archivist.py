"""Incident archivist: write REDACTED post-mortem to disk + recall similar playbooks from pgvector.

Safety contract:
- Post-mortems contain arg KEYS only; no secret values, no raw credentials.
- Vector payloads use strip_ephemeral_from_args which redacts _SENSITIVE_ARG_KEYS (value, password, ...).
- Recall injection is advisory only — never injects secret values into the LLM prompt.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from types import SimpleNamespace
from datetime import UTC, datetime
from typing import Any

from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, COLLECTION_SOP
from rag.redis_vector_store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

_POST_MORTEM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs",
    "post-mortems",
)


def _writable_postmortem_dir() -> str:
    """Prefer OMNI_POSTMORTEM_DIR, then repo docs path; fall back to /tmp when container FS is read-only."""
    env = (os.environ.get("OMNI_POSTMORTEM_DIR") or "").strip()
    candidates: list[str] = []
    if env:
        candidates.append(env)
    candidates.append(_POST_MORTEM_DIR)
    candidates.append(os.path.join(tempfile.gettempdir(), "omni-postmortems"))
    for base in candidates:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            return base
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), "omni-postmortems")

# Minimum cosine similarity to surface a recalled playbook as advisory.
_RECALL_SCORE_THRESHOLD = 0.70
# Above this score the recall is injected as a strong priority prefix (no secrets).
_RECALL_STRONG_THRESHOLD = 0.85
_RECALL_TOP_K = 3


def _postmortem_path(trace_id: str, base_dir: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", trace_id)
    return os.path.join(base_dir, f"{safe}.md")


def write_incident_postmortem(
    trace_id: str,
    *,
    tool_name: str,
    arg_keys: list[str],
    alertname: str,
    namespace: str,
    workload: str,
    outcome: str = "VERIFIED_SUCCESS",
) -> str:
    """Write a REDACTED markdown post-mortem file. Returns the file path written."""
    base_dir = _writable_postmortem_dir()
    path = _postmortem_path(trace_id, base_dir)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# Incident Post-Mortem — {trace_id}",
        "",
        f"**Date:** {now}",
        f"**Outcome:** {outcome}",
        "",
        "## Summary",
        "",
        f"- **Alert:** `{alertname}`",
        f"- **Namespace:** `{namespace}`",
        f"- **Workload:** `{workload}`",
        f"- **Remediation tool:** `{tool_name}`",
        f"- **Arg keys used:** {', '.join(f'`{k}`' for k in sorted(arg_keys)) or '(none)'}",
        "",
        "## Notes",
        "",
        "Arg values are intentionally omitted from this record to prevent credential leakage.",
        "Retrieve current Secret/ConfigMap values from the cluster at remediation time.",
        "",
    ]
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        logger.info(
            "event=archivist_postmortem_written trace=%s tool=%s path=%s",
            trace_id,
            tool_name,
            path,
        )
    except Exception as e:
        logger.warning("event=archivist_postmortem_fail trace=%s err=%s", trace_id, e)
    return path


class RecallResult:
    """Structured recall output: advisory text plus strength indicator."""

    __slots__ = ("advisory", "strong", "top_score", "top_tool", "top_arg_keys", "top_point_id")

    def __init__(
        self,
        advisory: str,
        strong: bool,
        top_score: float,
        top_tool: str,
        top_arg_keys: list[str],
        top_point_id: str = "",
    ) -> None:
        self.advisory = advisory
        self.strong = strong
        self.top_score = top_score
        self.top_tool = top_tool
        self.top_arg_keys = top_arg_keys
        self.top_point_id = top_point_id  # S2.4: track for negative feedback


async def recall_playbook_advisory(
    ctx: Any,
    *,
    query_text: str,
    trace: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> RecallResult | None:
    """Query COLLECTION_ACTION_EXPERIENCE for similar past incidents.

    Returns a RecallResult (advisory + strength flag) when similarity >= threshold,
    or None when no relevant prior playbook exists.
    The advisory text contains arg KEYS only — no secret values.

    ``tenant_id`` isolates the search to one customer's own remediation
    history (onboarding-ops-agent plan, step 1) — defaults to ``"default"``
    for the lab cluster's own self-operation experience.
    """
    vs = getattr(ctx, "vector_store", None)
    llm = getattr(ctx, "llm", None)
    ws = getattr(ctx, "settings", None)
    if vs is None or llm is None or ws is None:
        return None

    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")

    async def _search(collection: str, tid: str) -> Any:
        try:
            return await vs.similarity_search(
                query_text[:3000],
                collection,
                llm=llm,
                embed_model=embed_model,
                limit=_RECALL_TOP_K,
                score_threshold=_RECALL_SCORE_THRESHOLD,
                tenant_id=tid,
            )
        except Exception as e:
            logger.debug(
                "event=archivist_recall_skip trace=%s coll=%s tenant=%s err=%s",
                trace, collection, tid, e,
            )
            return None

    # D1+D2 (2026-07-31): tra NHIỀU nguồn, không chỉ action_experience của đúng tenant.
    #  - Cô lập tenant khiến index của khách LUÔN rỗng (kinh nghiệm chỉ nạp vào `default`)
    #    ⇒ RAG không bao giờ hit. Fallback về `default` khi index tenant không có gì.
    #  - Kho SOP (itops_sop_ledger, ~1093 mục) chưa từng được remote path tra — thêm vào.
    # SOP/experience là tri thức của CHÍNH Omni (không phải dữ liệu khách) ⇒ chia sẻ an
    # toàn qua các tenant; INV_DATA_RESIDENCY không bị đụng.
    sources: list[tuple[str, str]] = [(COLLECTION_ACTION_EXPERIENCE, tenant_id)]
    if tenant_id != DEFAULT_TENANT_ID:
        sources.append((COLLECTION_ACTION_EXPERIENCE, DEFAULT_TENANT_ID))
    sources.append((COLLECTION_SOP, DEFAULT_TENANT_ID))

    all_points: list[Any] = []
    for _coll, _tid in sources:
        _res = await _search(_coll, _tid)
        if _res is not None and getattr(_res, "points", None):
            all_points.extend(_res.points)

    if not all_points:
        return None
    result = SimpleNamespace(points=all_points)

    # S2.4: load negative recall set and apply score decay.
    redis = getattr(ctx, "redis", None)
    negative_set: set[str] = set()
    negative_counts: dict[str, int] = {}
    if redis is not None:
        try:
            raw_neg = await redis.smembers("omni:recall:negative_set")
            negative_set = {
                (m.decode() if isinstance(m, bytes) else m) for m in (raw_neg or [])
            }
        except Exception:
            pass

    # Filter hard-negative points and apply decay to soft-negative ones.
    filtered_points = []
    for pt in result.points:
        pid = str(pt.id or "")
        if pid in negative_set:
            logger.debug("event=recall_negative_filtered trace=%s point_id=%s", trace, pid)
            continue
        neg_count = 0
        if redis is not None and pid:
            try:
                raw_cnt = await redis.get(f"omni:recall:negative:{pid}")
                neg_count = int(raw_cnt or 0)
                negative_counts[pid] = neg_count
            except Exception:
                pass
        if neg_count > 0:
            decay = max(0.5, 1.0 - 0.15 * neg_count)
            pt.score = (pt.score or 0) * decay
        filtered_points.append(pt)

    if not filtered_points:
        return None

    # Re-sort after potential score decay.
    filtered_points.sort(key=lambda p: float(p.score or 0), reverse=True)
    top = filtered_points[0]
    top_p = top.payload or {}
    top_tool = str(top_p.get("tool") or top_p.get("resolution_tool") or "").strip()
    top_keys: list[str] = top_p.get("arg_keys") or sorted(
        str(k) for k in (top_p.get("args_playbook") or {}).keys()
    )
    top_score = round(float(top.score or 0), 3)
    strong = top_score >= _RECALL_STRONG_THRESHOLD
    top_point_id = str(top.id or "")

    # Plan step 4 — code-hard Live > RAG precedence. If the top chunk's
    # cluster_version disagrees with the live cluster, demote the recall to a
    # weak hint and force read-only re-verify (DEPRECATED_RISK).
    deprecated_warning = ""
    if bool(getattr(ws, "omni_rag_freshness_enabled", False)):
        from datetime import UTC, datetime

        from rag.rag_freshness import (
            FRESHNESS_DEPRECATED_RISK,
            assess_recall_freshness,
        )

        verdict = assess_recall_freshness(
            top_p,
            live_cluster_version=(str(getattr(ws, "omni_cluster_version", "")) or None),
            now_iso=datetime.now(UTC).isoformat(),
            max_age_sec=int(getattr(ws, "omni_rag_freshness_max_age_sec", 2_592_000)),
        )
        if verdict.label == FRESHNESS_DEPRECATED_RISK:
            strong = False
            deprecated_warning = (
                f"[DEPRECATED_RISK] {verdict.reason} "
                "Treat this playbook as a hypothesis only; re-verify cluster state read-only "
                "before acting."
            )
            logger.info(
                "event=recall_deprecated_risk trace=%s point_id=%s reason=%s",
                trace,
                top_point_id,
                verdict.reason,
            )

    advisory_lines = [
        "Verified incident playbooks from memory (advisory — do NOT copy secret values):",
    ]
    if deprecated_warning:
        advisory_lines.insert(0, deprecated_warning)
    for pt in filtered_points:
        p = pt.payload or {}
        tool = str(p.get("tool") or p.get("resolution_tool") or "").strip()
        keys = p.get("arg_keys") or sorted(str(k) for k in (p.get("args_playbook") or {}).keys())
        wf = str(p.get("workload_fingerprint") or "").strip()
        score = round(float(pt.score or 0), 3)
        line = (
            f"- similarity={score} tool={tool or '?'}"
            f" arg_keys=[{', '.join(str(k) for k in keys)}]"
            f"{f' workload={wf}' if wf else ''}"
        )
        advisory_lines.append(line)

    advisory = "\n".join(advisory_lines)
    logger.info(
        "event=archivist_recall_hit trace=%s hits=%s top_score=%s top_tool=%s strong=%s",
        trace,
        len(result.points),
        top_score,
        top_tool,
        strong,
    )
    return RecallResult(
        advisory=advisory,
        strong=strong,
        top_score=top_score,
        top_tool=top_tool,
        top_arg_keys=top_keys,
        top_point_id=top_point_id,
    )


async def recall_knowledge_context(
    ctx: Any,
    *,
    query_text: str,
    tenant_id: str = DEFAULT_TENANT_ID,
    max_items: int = 3,
    max_chars: int = 900,
) -> str:
    """Digest tri thức liên quan để NHÉT vào prompt chẩn đoán (D3, 2026-07-31).

    Khác `recall_playbook_advisory` (cổng ĐỊNH TUYẾN, ngưỡng 0.75): hàm này lấy vài
    mẩu SOP/kinh nghiệm gần nhất BẤT KỂ ngưỡng, chỉ để LLM có kiến thức tham khảo khi
    chẩn đoán — trước đây prompt hoàn toàn không có RAG nên LLM chẩn lại từ số 0 mỗi lần.
    Chỉ trả metadata/text_content (không giá trị bí mật). Rỗng ⇒ trả "".
    """
    vs = getattr(ctx, "vector_store", None)
    llm = getattr(ctx, "llm", None)
    ws = getattr(ctx, "settings", None)
    if vs is None or llm is None or ws is None or not query_text.strip():
        return ""
    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")

    sources: list[tuple[str, str]] = [(COLLECTION_ACTION_EXPERIENCE, tenant_id)]
    if tenant_id != DEFAULT_TENANT_ID:
        sources.append((COLLECTION_ACTION_EXPERIENCE, DEFAULT_TENANT_ID))
    sources.append((COLLECTION_SOP, DEFAULT_TENANT_ID))

    seen: set[str] = set()
    lines: list[str] = []
    for coll, tid in sources:
        try:
            res = await vs.similarity_search(
                query_text[:3000], coll, llm=llm, embed_model=embed_model,
                limit=max_items, score_threshold=0.0, tenant_id=tid,
            )
        except Exception as e:
            logger.debug("event=knowledge_ctx_skip coll=%s err=%s", coll, e)
            continue
        for pt in (getattr(res, "points", None) or []):
            payload = getattr(pt, "payload", None) or {}
            text = str(
                payload.get("text_content") or payload.get("text")
                or payload.get("summary") or ""
            ).strip().replace("\n", " ")
            if not text or text[:80] in seen:
                continue
            seen.add(text[:80])
            score = round(float(getattr(pt, "score", 0) or 0), 2)
            lines.append(f"- (score={score}) {text[:220]}")
            if len(lines) >= max_items:
                break
        if len(lines) >= max_items:
            break

    if not lines:
        return ""
    return "\n".join(lines)[:max_chars]


def build_strong_recall_prefix(recall: RecallResult) -> str:
    """Return a priority-prefix block for high-confidence recalls (no secret values).

    Injected like broken_prefix — before the Fact Table — so the LLM sees it in round 1.
    """
    keys_str = ", ".join(str(k) for k in recall.top_arg_keys)
    return (
        f"PLAYBOOK RECALL (similarity={recall.top_score} — high confidence):\n"
        f"A prior incident with identical symptom fingerprint was resolved using "
        f"tool={recall.top_tool} with arg_keys=[{keys_str}].\n"
        "- Recommended first action: call this tool directly with values from cluster state.\n"
        "- Do NOT spend readonly steps on unrelated discovery if symptom is already clear.\n"
        "- Arg values must be read from the cluster (kubectl / describe / pod logs), "
        "NOT from this advisory — never hardcode values from memory.\n\n"
    )
