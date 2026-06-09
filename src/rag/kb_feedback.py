"""KB feedback loop: write probe-verified outcomes back to RAG knowledge base.

After Omni runs read-only probes to test a hypothesis, it contrasts the result
against each KB hit it used and records the outcome so stale/wrong KB entries
"age out" via score decay and stale marking.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_FEEDBACK_LOG_KEY = "omni:kb:feedback:log"
_LOG_KEEP = 500
_LOG_TTL_SEC = 7 * 24 * 3600
_DEFAULT_SCORE = 60


def _clamp(v: int, lo: int, hi: int) -> int:
    """Clamp ``v`` into the inclusive range ``[lo, hi]``."""
    return max(lo, min(hi, v))


async def apply_kb_feedback(
    redis: Any,
    *,
    trace: str,
    assessments: list[dict],
    score_confirmed_delta: int = 5,
    score_refuted_delta: int = -8,
    stale_threshold: int = 3,
) -> dict:
    """Apply probe-verified assessments back to KB docs and audit the change.

    Each assessment dict: ``{"kb_id", "collection", "verdict", "applicable",
    "reason"}``. Only ``confirmed``/``refuted`` verdicts mutate scores;
    ``unverifiable`` or ``applicable=False`` are ignored. Best-effort: never
    raises. Returns a summary dict.
    """
    summary: dict[str, Any] = {
        "confirmed": 0,
        "refuted": 0,
        "stale_marked": [],
        "missing": 0,
        "skipped": 0,
    }

    for assessment in assessments:
        try:
            verdict = assessment.get("verdict")
            if verdict not in ("confirmed", "refuted"):
                # unverifiable / unknown → ignore silently.
                continue
            if not assessment.get("applicable", True):
                continue

            kb_id = assessment.get("kb_id", "")
            collection = assessment.get("collection", "")
            if not (_ID_RE.match(str(kb_id)) and _ID_RE.match(str(collection))):
                summary["skipped"] += 1
                continue

            key = f"doc:{collection}:{kb_id}"
            raw = await redis.hget(key, "omni_payload")
            if raw is None:
                summary["missing"] += 1
                continue

            payload = json.loads(raw)
            reason = str(assessment.get("reason", ""))

            if verdict == "confirmed":
                payload["confirmed_count"] = int(payload.get("confirmed_count", 0)) + 1
                score = _clamp(
                    int(payload.get("score", _DEFAULT_SCORE)) + score_confirmed_delta,
                    0,
                    100,
                )
                summary["confirmed"] += 1
            else:  # refuted
                payload["contradicted_count"] = (
                    int(payload.get("contradicted_count", 0)) + 1
                )
                score = _clamp(
                    int(payload.get("score", _DEFAULT_SCORE)) + score_refuted_delta,
                    0,
                    100,
                )
                summary["refuted"] += 1
                if payload["contradicted_count"] >= stale_threshold:
                    payload["stale"] = True
                    sig = reason[:80]
                    stale_for = payload.get("stale_for") or []
                    if sig and sig not in stale_for:
                        stale_for.append(sig)
                    payload["stale_for"] = stale_for
                    if kb_id not in summary["stale_marked"]:
                        summary["stale_marked"].append(kb_id)

            payload["score"] = score
            payload["last_assessed_trace"] = trace
            payload["last_assessed_ts"] = int(time.time())

            await redis.hset(
                key, "omni_payload", json.dumps(payload, ensure_ascii=False)
            )

            audit = {
                "ts": int(time.time()),
                "trace": trace,
                "kb_id": kb_id,
                "collection": collection,
                "verdict": verdict,
                "new_score": score,
            }
            await redis.rpush(
                _FEEDBACK_LOG_KEY, json.dumps(audit, ensure_ascii=False)
            )
            await redis.ltrim(_FEEDBACK_LOG_KEY, -_LOG_KEEP, -1)
            await redis.expire(_FEEDBACK_LOG_KEY, _LOG_TTL_SEC)
        except Exception:
            # Best-effort: one bad entry must not abort the whole batch.
            continue

    return summary
