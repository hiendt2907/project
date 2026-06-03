"""Context window discipline before Ollama: trim K8s/log blobs to fit next to system + tool prompts."""

from __future__ import annotations

# Worker default num_ctx — configurable via OMNI_LLM_NUM_CTX (S2.1). Legacy fallback=4096.
_DEFAULT_NUM_CTX = 4096
_RESERVED_FOR_SYSTEM_AND_COMPLETION = 2200


def llm_evidence_char_budget(*, num_ctx: int | None = None, reserved: int | None = None) -> int:
    """Approximate max characters for merged evidence text (logs + probe snippets) per call."""
    ctx = int(num_ctx or _DEFAULT_NUM_CTX)
    res = int(reserved or _RESERVED_FOR_SYSTEM_AND_COMPLETION)
    # ~4 chars/token heuristic is pessimistic; char cap is a safety rail, not exact tokens.
    return max(512, ctx * 2 - res)


def clamp_evidence_text(text: str, *, max_chars: int | None = None, num_ctx: int | None = None) -> str:
    """Truncate with ellipsis marker for LLM consumption."""
    cap = max_chars if max_chars is not None else llm_evidence_char_budget(num_ctx=num_ctx)
    s = (text or "").strip()
    if len(s) <= cap:
        return s
    head = max(0, cap - 80)
    return s[:head] + "\n…[truncated for Ollama context]…\n" + s[-min(40, cap // 8) :]
