"""PlaybookMatcher — maps SIEM incident labels to a pre-approved Playbook.

Priority:
  1. Explicit playbook_id from alert label (set by siem_bridge.py)
  2. Category + severity lookup in PlaybookStore
  3. None (no matching playbook → fall through to generic analyst flow)
"""

from __future__ import annotations

import logging
from typing import Any

from .models import Playbook
from .store import PlaybookStore

logger = logging.getLogger(__name__)


class PlaybookMatcher:
    def __init__(self, store: PlaybookStore) -> None:
        self._store = store

    async def match(
        self,
        *,
        playbook_id: str = "",
        siem_category: str = "",
        severity: str = "",
    ) -> Playbook | None:
        """Return the matching Playbook or None."""
        pid = (playbook_id or "").strip()
        if pid:
            pb = await self._store.get(pid)
            if pb:
                logger.info("event=playbook_matched_by_id playbook_id=%s", pid)
                return pb
            logger.warning("event=playbook_id_not_found playbook_id=%s", pid)

        cat = (siem_category or "").strip()
        sev = (severity or "").strip()
        if cat:
            pb = await self._store.find_by_category_severity(cat, sev)
            if pb:
                logger.info(
                    "event=playbook_matched_by_category category=%s severity=%s playbook_id=%s",
                    cat,
                    sev,
                    pb.playbook_id,
                )
                return pb
        return None

    async def match_spec(
        self,
        *,
        lane: str = "",
        fault_text: str = "",
        severity: str = "",
    ) -> Any | None:
        """Deterministic PlaybookSpec match (L4 playbook-first, chạy TRƯỚC LLM).

        Match khi: lane ∈ trigger.lanes (nếu khai) AND ≥1 fault_keyword xuất hiện
        trong fault_text (lowercase contains) AND severity khớp filter (nếu khai).
        Nhiều match → chọn playbook có NHIỀU keyword khớp nhất (specific thắng generic).
        """
        text = (fault_text or "").lower()
        ln = (lane or "").strip().upper()
        sev = (severity or "").strip().lower()
        best = None
        best_hits = 0
        for spec in await self._store.list_specs():
            trig = spec.trigger
            if trig.lanes and ln and ln not in [x.upper() for x in trig.lanes]:
                continue
            if trig.severity_filter and sev and trig.severity_filter.lower() != sev:
                continue
            hits = sum(1 for kw in trig.fault_keywords if kw.lower() in text)
            if trig.fault_keywords and hits == 0:
                continue
            if hits > best_hits or best is None:
                best, best_hits = spec, hits
        if best is not None:
            logger.info(
                "event=playbook_spec_matched playbook_id=%s hits=%d lane=%s", best.playbook_id, best_hits, ln,
            )
        return best

    async def match_from_batch(self, batch: list[dict[str, Any]]) -> Playbook | None:
        """Convenience: extract SIEM labels from canonical_query_snippet and match."""
        import json

        for b in batch:
            snip = str(b.get("canonical_query_snippet") or "").strip()
            if not snip.startswith("{"):
                continue
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else {}
                if not isinstance(labels, dict):
                    continue
                # Đ49 B3/S0.3 — trước đây chỉ match khi siem_source=="finguard" (external
                # FinGuard). SIEM nay là dữ liệu nội bộ (Smart SIEM merge), canonical value
                # là "omni_siem" — nhưng gate không còn cần so đúng 1 chuỗi, chỉ cần đây LÀ
                # một batch item gốc SIEM (có siem_source nào đó), tránh phụ thuộc lại một
                # literal cụ thể như bug cũ.
                if not labels.get("siem_source"):
                    continue
                return await self.match(
                    playbook_id=str(labels.get("siem_playbook_id") or ""),
                    siem_category=str(labels.get("siem_category") or ""),
                    severity=str(labels.get("severity") or ""),
                )
            except Exception:
                continue
        return None
