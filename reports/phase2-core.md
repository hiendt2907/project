# Chặng 2 — Bộ não & Trí nhớ (Core modules)

## Đã giao

| Module | Đường dẫn | Mô tả |
|--------|-----------|--------|
| Ollama async | [src/llm/ollama_client.py](src/llm/ollama_client.py) | `POST /api/chat`, `POST /api/embed`; mọi `options` đều ép `num_ctx=4096` (kể cả khi caller truyền sai). |
| Qdrant | [src/rag/qdrant_store.py](src/rag/qdrant_store.py) | `QdrantSettings` (env `QDRANT_URL`, `QDRANT_TIMEOUT_S`), `make_qdrant_client()`, `ensure_collections()`, `log_error_to_ledger()`. |
| Error ledger | [src/rag/error_ledger.py](src/rag/error_ledger.py) | `ErrorLedger`: `ensure_ready()`, `record_error()`, `record_exception()`; mặc định **nuốt** lỗi Qdrant (`swallow_errors=True`) để worker không chết. |
| 3-sigma | [src/anomaly/three_sigma.py](src/anomaly/three_sigma.py) | `LPUSH` + `LTRIM` + `EXPIRE` trong **một** `pipeline.execute()` mỗi lần `observe()`. |
| Pytest | [tests/test_three_sigma.py](tests/test_three_sigma.py), [tests/test_ollama_client.py](tests/test_ollama_client.py), [tests/test_qdrant_store.py](tests/test_qdrant_store.py), [tests/test_error_ledger.py](tests/test_error_ledger.py) | `fakeredis` cho Redis; mock httpx / AsyncMock Qdrant. |
| Config | [pytest.ini](pytest.ini) | `pythonpath=src`, `asyncio_mode=auto`. |

## Kiểm thử

```bash
cd /path/to/project
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -q
```

Kết quả gần nhất: **14 passed** (chạy `pytest tests/ -q`).

## Nghiệm thu

- [x] `num_ctx` luôn 4096 trên chat và embed.
- [x] Collection `itops_sop_ledger` (và ledger lỗi) được định nghĩa trong code.
- [x] 3-sigma: mọi lần ghi có TTL; test chứng minh TTL > 0 và số key có giới hạn theo số metric khác nhau (không phình vô hạn trong `fakeredis`).

## Ghi lỗi vào Qdrant (ErrorLedger)

```python
from rag.error_ledger import ErrorLedger

ledger = ErrorLedger.from_settings()  # đọc QDRANT_URL / QDRANT_TIMEOUT_S
try:
    await ledger.record_error(
        title="Tiêu đề",
        detail="Mô tả / stack",
        phase="2",
        component="omni_worker",
    )
finally:
    await ledger.aclose()
```

Hoặc với client có sẵn: `ErrorLedger(async_client, owns_client=False)`.

`record_exception(exc, phase=..., component=...)` ghi full traceback.

Env: `QDRANT_URL` (mặc định in-cluster cùng NS: `http://qdrant:6333`), `QDRANT_TIMEOUT_S`.

## Ledger lỗi (runtime)

| Thời điểm | Thành phần | Mô tả | Cách xử lý |
|-----------|------------|-------|------------|
| _(trống)_ | — | Chưa ghi point từ cluster (chỉ unit test) | Dùng snippet trên khi tích hợp Omni-Worker |
