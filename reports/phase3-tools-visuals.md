# Chặng 3 — Bàn tay & Con mắt (Tools & Visuals)

## Đã giao

| Module | Đường dẫn | Mô tả |
|--------|-----------|--------|
| Chart RAM | [src/visualization/chart_bytes.py](src/visualization/chart_bytes.py) | `line_chart_png_bytes()` — matplotlib `Agg`, `fig.savefig(BytesIO)`, `plt.close(fig)`. |
| Telegram | [src/ingest/telegram.py](src/ingest/telegram.py) | `TelegramClient`: `get_updates`, `send_message`, `send_photo_bytes` (multipart từ `BytesIO`, không `/tmp`). `TelegramBotSettings` env `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_BASE`. `summarize_message_update()` chuẩn hoá update. |

## Kiểm thử

```bash
pytest tests/test_chart_bytes.py tests/test_telegram.py -q
# hoặc toàn bộ: pytest tests/ -q
```

Kết quả gần nhất: **21 passed** (toàn repo).

## Nghiệm thu

- [x] Biểu đồ chỉ trả `bytes` PNG, không ghi đĩa; test `test_no_open_write_to_tmp` chặn `open('/tmp/...')`.
- [x] Gửi ảnh qua multipart với buffer RAM; không dùng file tạm trên filesystem.
- [x] Env: `TELEGRAM_BOT_TOKEN` (prefix `TELEGRAM_`).

## Ghi chú vận hành

- Chạy thật: export `TELEGRAM_BOT_TOKEN`, dùng `TelegramClient.from_settings()` rồi `get_updates` / `send_photo_bytes`.
