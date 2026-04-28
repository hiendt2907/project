"""Telegram Bot API (async httpx) — nhận update, gửi text/ảnh từ bytes (không /tmp)."""

from __future__ import annotations

import io
from typing import Any

import httpx
from pydantic import BaseModel, Field, PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramBotSettings(BaseSettings):
    """Env: `TELEGRAM_BOT_TOKEN` (bắt buộc khi gọi API thật)."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="ignore")

    bot_token: str = Field(default="", description="Bot token từ @BotFather")
    api_base: str = Field(default="https://api.telegram.org")


class TelegramMessageSummary(BaseModel):
    """Tóm tắt tin nhắn text để đưa vào pipeline chuẩn hoá."""

    update_id: int
    chat_id: int
    message_id: int
    text: str | None = None
    from_user_id: int | None = None


class TelegramClient(BaseModel):
    """Multipart ảnh dùng BytesIO — không mở path trên filesystem."""

    bot_token: str = Field(min_length=1)
    api_base: str = Field(default="https://api.telegram.org")

    model_config = {"arbitrary_types_allowed": True}

    _http: httpx.AsyncClient = PrivateAttr()

    def model_post_init(self, _context: Any) -> None:
        base = f"{self.api_base.rstrip('/')}/bot{self.bot_token}"
        self._http = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120.0))

    @classmethod
    def from_settings(cls, settings: TelegramBotSettings | None = None) -> TelegramClient:
        s = settings or TelegramBotSettings()
        if not s.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN / TelegramBotSettings.bot_token is required")
        return cls(bot_token=s.bot_token, api_base=s.api_base)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
        limit: int = 100,
    ) -> dict[str, Any]:
        """getUpdates (long-poll)."""
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        r = await self._http.get("/getUpdates", params=params)
        r.raise_for_status()
        return r.json()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        if parse_mode is not None:
            body["parse_mode"] = parse_mode
        r = await self._http.post("/sendMessage", json=body)
        r.raise_for_status()
        return r.json()

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
            payload["show_alert"] = show_alert
        r = await self._http.post("/answerCallbackQuery", json=payload)
        r.raise_for_status()
        return r.json()

    async def send_photo_bytes(
        self,
        chat_id: int,
        png: bytes,
        *,
        caption: str | None = None,
        filename: str = "chart.png",
    ) -> dict[str, Any]:
        """
        sendPhoto với nội dung file trong RAM (BytesIO).
        Không dùng NamedTemporaryFile hay ghi /tmp.
        """
        bio = io.BytesIO(png)
        files = {"photo": (filename, bio, "image/png")}
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption is not None:
            data["caption"] = caption
        r = await self._http.post("/sendPhoto", data=data, files=files)
        r.raise_for_status()
        return r.json()


def summarize_message_update(raw: dict[str, Any]) -> TelegramMessageSummary | None:
    """Trích một message text từ một phần tử result của getUpdates."""
    msg = raw.get("message") or raw.get("edited_message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    from_user = msg.get("from") or {}
    return TelegramMessageSummary(
        update_id=int(raw["update_id"]),
        chat_id=int(chat["id"]),
        message_id=int(msg["message_id"]),
        text=msg.get("text"),
        from_user_id=int(from_user["id"]) if from_user.get("id") is not None else None,
    )
