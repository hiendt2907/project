# RAG gate — quan sát & chỉnh ngưỡng

**Code:** `[src/pkg/rag/gate.py](../../src/pkg/rag/gate.py)` (`evaluate_rag_gate`), settings trong `[src/workers/settings.py](../../src/workers/settings.py)`.

## Log grep (lab)


| Mục               | Pattern / field                                                               |
| ----------------- | ----------------------------------------------------------------------------- |
| Hit               | `event=rag_gate_hit` hoặc `event=rag_truth_citations` trong evidence consumer |
| Dưới ngưỡng       | `detail` có `below_threshold`                                                 |
| Không chắc (tier) | `knowledge_uncertain` khi bật `rag_tier_uncertain_gate_enabled`               |
| Post-filter rỗng  | `post_filter_empty`                                                           |
| Query quá ngắn    | `query_too_short`                                                             |


## Env chính

- `OMNI_RAG_GATE_SCORE_THRESHOLD` — mặc định ~0.42; hạ từng bước khi đã có corpus + golden test.
- `OMNI_RAG_TIER_UNCERTAIN_GATE_ENABLED` + `OMNI_RAG_TIER_KNOWLEDGE_UNCERTAIN_THRESHOLD` — chặn hit “mơ hồ”.
- `OMNI_RAG_HOT_CACHE_ENABLED` — giảm embed lặp lại cùng query.

## Gợi ý

Điều chỉnh threshold **sau** khi có tỉ lệ hit/miss theo `detail.reason` trên một cửa sổ trace; không hạ ngưỡng mù quáng (hallucination trên miss).