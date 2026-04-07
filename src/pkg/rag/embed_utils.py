"""Chuẩn hóa độ dài text trước khi gọi Ollama /api/embed — tránh 400 payload quá lớn."""

from __future__ import annotations

# Heuristic: ~4 ký tự / token (EN); nomic-embed context lớn nhưng Ollama vẫn có thể 400 nếu input cực dài.
_CHARS_PER_TOKEN = 4


def truncate_for_embedding(text: str, max_tokens: int = 512) -> str:
    """
    Cắt ngắn trước embed. Mặc định ~512 token (~2048 ký tự), tối thiểu 256 ký tự để vẫn có tín hiệu.
    """
    cap = max(256, int(max_tokens) * _CHARS_PER_TOKEN)
    t = (text or "").strip()
    if len(t) <= cap:
        return t
    return t[:cap].rstrip() + "\n[…truncated for embedding]"
