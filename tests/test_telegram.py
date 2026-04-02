"""Telegram client — mock API; chứng minh sendPhoto dùng bytes/BytesIO."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest

from ingest.telegram import (
    TelegramMessageSummary,
    summarize_message_update,
)


@pytest.mark.asyncio
async def test_send_photo_bytes_posts_multipart_not_file_path() -> None:
    from ingest.telegram import TelegramClient

    client = TelegramClient(bot_token="dummy", api_base="https://api.telegram.org")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"ok": True}
    client._http.post = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]

    png = b"\x89PNG\r\n\x1a\nfake"
    await client.send_photo_bytes(12345, png, caption="Chú thích")

    client._http.post.assert_awaited_once()
    call = client._http.post.call_args
    assert call[0][0] == "/sendPhoto"
    files = call[1]["files"]
    assert "photo" in files
    name, bio, mime = files["photo"]
    assert name.endswith(".png")
    assert mime == "image/png"
    assert isinstance(bio, BytesIO)
    assert bio.getvalue() == png


def test_summarize_message_update() -> None:
    raw = {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "chat": {"id": 99},
            "from": {"id": 1},
            "text": "xin chào",
        },
    }
    s = summarize_message_update(raw)
    assert isinstance(s, TelegramMessageSummary)
    assert s.chat_id == 99
    assert s.text == "xin chào"


def test_summarize_non_message_returns_none() -> None:
    assert summarize_message_update({"update_id": 1}) is None
