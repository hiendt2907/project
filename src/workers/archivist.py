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
from datetime import UTC, datetime
from typing import Any

from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE

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

    __slots__ = ("advisory", "strong", "top_score", "top_tool", "top_arg_keys")

    def __init__(
        self,
        advisory: str,
        strong: bool,
        top_score: float,
        top_tool: str,
        top_arg_keys: list[str],
    ) -> None:
        self.advisory = advisory
        self.strong = strong
        self.top_score = top_score
        self.top_tool = top_tool
        self.top_arg_keys = top_arg_keys


async def recall_playbook_advisory(
    ctx: Any,
    *,
    query_text: str,
    trace: str,
) -> RecallResult | None:
    """Query COLLECTION_ACTION_EXPERIENCE for similar past incidents.

    Returns a RecallResult (advisory + strength flag) when similarity >= threshold,
    or None when no relevant prior playbook exists.
    The advisory text contains arg KEYS only — no secret values.
    """
    vs = getattr(ctx, "vector_store", None)
    llm = getattr(ctx, "llm", None)
    ws = getattr(ctx, "settings", None)
    if vs is None or llm is None or ws is None:
        return None

    embed_model = str(getattr(ws, "embed_model", "nomic-embed-text") or "nomic-embed-text")
    try:
        result = await vs.similarity_search(
            query_text[:3000],
            COLLECTION_ACTION_EXPERIENCE,
            llm=llm,
            embed_model=embed_model,
            limit=_RECALL_TOP_K,
            score_threshold=_RECALL_SCORE_THRESHOLD,
        )
    except Exception as e:
        logger.debug("event=archivist_recall_skip trace=%s err=%s", trace, e)
        return None

    if not result.points:
        return None

    top = result.points[0]
    top_p = top.payload or {}
    top_tool = str(top_p.get("tool") or top_p.get("resolution_tool") or "").strip()
    top_keys: list[str] = top_p.get("arg_keys") or sorted(
        str(k) for k in (top_p.get("args_playbook") or {}).keys()
    )
    top_score = round(float(top.score or 0), 3)
    strong = top_score >= _RECALL_STRONG_THRESHOLD

    advisory_lines = [
        "Verified incident playbooks from memory (advisory — do NOT copy secret values):",
    ]
    for pt in result.points:
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
    )


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
