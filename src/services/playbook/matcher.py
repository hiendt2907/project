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
                if labels.get("siem_source") != "finguard":
                    continue
                return await self.match(
                    playbook_id=str(labels.get("siem_playbook_id") or ""),
                    siem_category=str(labels.get("siem_category") or ""),
                    severity=str(labels.get("severity") or ""),
                )
            except Exception:
                continue
        return None
