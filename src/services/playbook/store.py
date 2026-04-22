"""PlaybookStore — Redis JSON + RedisSearch backed repository for pre-approved remediation playbooks.

Keys:    pb:{playbook_id}  (JSON)
Index:   idx:playbooks     (FT, JSON, prefix pb:)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis
from redis.commands.search.field import NumericField, TagField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from .models import Playbook, PlaybookStep

logger = logging.getLogger(__name__)

_INDEX_NAME = "idx:playbooks"
_KEY_PREFIX = "pb:"


class PlaybookStore:
    """Read/write playbooks from Redis JSON with FT search index."""

    def __init__(self, r: redis.Redis) -> None:
        self._r = r

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """Create the FT index if it does not already exist."""
        try:
            await self._r.ft(_INDEX_NAME).info()
            logger.debug("event=ft_index_exists index=%s", _INDEX_NAME)
        except Exception:
            # Index does not exist — create it.
            schema = (
                TagField("$.siem_categories[*]", as_name="siem_categories"),
                TagField("$.severity_filter", as_name="severity_filter"),
                NumericField("$.created_at_ts", as_name="created_at_ts", sortable=True),
            )
            definition = IndexDefinition(
                prefix=[_KEY_PREFIX],
                index_type=IndexType.JSON,
            )
            await self._r.ft(_INDEX_NAME).create_index(schema, definition=definition)
            logger.info("event=ft_index_created index=%s", _INDEX_NAME)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert(self, playbook: Playbook) -> None:
        """Insert or replace a playbook (idempotent)."""
        import time

        doc = {
            "playbook_id": playbook.playbook_id,
            "version": playbook.version,
            "name": playbook.name,
            "severity_filter": playbook.severity_filter,
            "approved_by": playbook.approved_by,
            "siem_categories": list(playbook.siem_categories),
            "created_at_ts": time.time(),
            "steps": [
                {
                    "step_order": s.step_order,
                    "action_type": s.action_type,
                    "target": s.target,
                    "params": s.params,
                    "timeout_sec": s.timeout_sec,
                    "requires_hitl": s.requires_hitl,
                }
                for s in playbook.steps
            ],
        }
        key = f"{_KEY_PREFIX}{playbook.playbook_id}"
        await self._r.json().set(key, "$", doc)
        logger.info("event=playbook_upserted playbook_id=%s", playbook.playbook_id)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, playbook_id: str) -> Playbook | None:
        key = f"{_KEY_PREFIX}{playbook_id}"
        raw = await self._r.json().get(key, "$")
        if not raw:
            return None
        # JSON.GET with path "$" returns a list with one element
        docs = raw if isinstance(raw, list) else [raw]
        if not docs:
            return None
        return _doc_to_playbook(docs[0])

    async def find_by_category_severity(
        self,
        category: str,
        severity: str,
    ) -> Playbook | None:
        """Return the most-recently upserted playbook matching category and severity."""
        # Escape special RedisSearch TAG characters in caller-supplied strings
        safe_category = _escape_tag(category)
        safe_severity = _escape_tag(severity)

        query_str = (
            f"@siem_categories:{{{safe_category}}} "
            f"(@severity_filter:{{{safe_severity}}}|@severity_filter:{{}})"
        )
        q = (
            Query(query_str)
            .sort_by("created_at_ts", asc=False)
            .paging(0, 1)
        )
        results = await self._r.ft(_INDEX_NAME).search(q)
        if not results.docs:
            return None
        raw_json = results.docs[0].json
        doc = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        # search returns the document directly (not wrapped in a list like JSON.GET $)
        if isinstance(doc, list):
            doc = doc[0]
        return _doc_to_playbook(doc)

    async def list_all(self) -> list[Playbook]:
        q = (
            Query("*")
            .sort_by("created_at_ts", asc=False)
            .paging(0, 1000)
        )
        results = await self._r.ft(_INDEX_NAME).search(q)
        playbooks: list[Playbook] = []
        for doc in results.docs:
            raw_json = doc.json
            d = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if isinstance(d, list):
                d = d[0]
            playbooks.append(_doc_to_playbook(d))
        return playbooks


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _doc_to_playbook(doc: dict[str, Any]) -> Playbook:
    steps = tuple(
        PlaybookStep(
            step_order=int(s["step_order"]),
            action_type=str(s["action_type"]),
            target=str(s.get("target") or ""),
            params=s["params"] if isinstance(s["params"], dict) else json.loads(s["params"] or "{}"),
            timeout_sec=int(s.get("timeout_sec") or 60),
            requires_hitl=bool(s.get("requires_hitl", False)),
        )
        for s in (doc.get("steps") or [])
    )
    raw_cats = doc.get("siem_categories") or []
    siem_categories = tuple(raw_cats) if isinstance(raw_cats, list) else tuple()
    return Playbook(
        playbook_id=str(doc["playbook_id"]),
        version=str(doc.get("version") or "1"),
        name=str(doc.get("name") or ""),
        severity_filter=str(doc.get("severity_filter") or ""),
        approved_by=str(doc.get("approved_by") or ""),
        steps=steps,
        siem_categories=siem_categories,
    )


_TAG_ESCAPE_CHARS = r',.<>{}[]"\'/:;!@#$%^&*()-+=~| '


def _escape_tag(value: str) -> str:
    """Escape special characters for a RedisSearch TAG query value."""
    escaped = []
    for ch in value:
        if ch in _TAG_ESCAPE_CHARS:
            escaped.append(f"\\{ch}")
        else:
            escaped.append(ch)
    return "".join(escaped)
