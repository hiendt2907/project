"""Evidence outbox — disk spool chống mất evidence khi mất mạng (Sprint NV-SRE IT-7).

Nguyên tắc no-duplicate: batch CHỈ vào outbox khi emit fail TOÀN PHẦN
(transport trả None sau khi cạn retry). Batch gateway đã ACK không bao giờ
được spool, nên replay không tạo duplicate. Flush theo thứ tự cũ→mới và dừng
ngay ở batch đầu tiên fail (mạng vẫn down) để giữ ordering.

Spool/flush đều best-effort: lỗi disk KHÔNG được làm crash vòng telemetry.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BATCHES = 2000  # ~33h ở collect_interval 60s — dư cho drill 10 phút
_FLUSH_LIMIT_PER_CALL = 200  # chặn một lần flush chiếm trọn chu kỳ collect

EmitFn = Callable[[list[dict]], Awaitable[Any]]


class EvidenceOutbox:
    def __init__(self, root: Path | str, max_batches: int = _DEFAULT_MAX_BATCHES) -> None:
        self._root = Path(root)
        self._max_batches = max_batches
        self._dropped_batches = 0

    def pending(self) -> list[Path]:
        """File batch đang chờ, cũ nhất trước (tên file = time_ns nên sort được)."""
        if not self._root.is_dir():
            return []
        return sorted(self._root.glob("*.json"))

    def has_pending(self) -> bool:
        return bool(self.pending())

    def health(self) -> dict[str, float | int | bool]:
        """Operational backpressure signal; dropping is visible, never silent."""
        pending = len(self.pending())
        pressure = min(1.0, pending / self._max_batches) if self._max_batches else 1.0
        return {
            "pending_batches": pending,
            "max_batches": self._max_batches,
            "pressure": pressure,
            "backpressure": pressure >= 0.8,
            "dropped_batches": self._dropped_batches,
        }

    def spool(self, evidence_list: list[dict]) -> Path | None:
        """Ghi batch ra disk (atomic write-then-rename). Trả None nếu disk lỗi."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.json"
            tmp = self._root / (name + ".tmp")
            tmp.write_text(json.dumps(evidence_list, ensure_ascii=False))
            final = self._root / name
            tmp.rename(final)
            self._prune()
            logger.warning(
                "[outbox] spooled batch (%d items) — pending=%d",
                len(evidence_list), len(self.pending()),
            )
            return final
        except OSError as exc:
            logger.error("[outbox] spool failed (evidence dropped): %s", exc)
            return None

    async def flush(self, emit_fn: EmitFn, limit: int = _FLUSH_LIMIT_PER_CALL) -> dict:
        """Replay batch cũ→mới. Dừng ở batch đầu tiên emit trả None (mạng down)."""
        flushed = corrupted = 0
        for path in self.pending()[:limit]:
            try:
                evidence = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                logger.error("[outbox] corrupted batch %s dropped: %s", path.name, exc)
                path.unlink(missing_ok=True)
                corrupted += 1
                continue
            result = await emit_fn(evidence)
            if result is None:
                break  # mạng vẫn down — giữ batch, thử lại chu kỳ sau
            path.unlink(missing_ok=True)
            flushed += 1
        if flushed:
            logger.info("[outbox] flushed %d batch(es), pending=%d", flushed, len(self.pending()))
        return {"flushed": flushed, "corrupted": corrupted}

    def _prune(self) -> None:
        pending = self.pending()
        excess = len(pending) - self._max_batches
        for path in pending[:max(0, excess)]:
            path.unlink(missing_ok=True)
            self._dropped_batches += 1
            logger.error("[outbox] cap %d exceeded — dropped oldest %s", self._max_batches, path.name)
