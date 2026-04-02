"""Gemini Developer API (google-genai) — async, retry 429, spillover Ollama."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from google import genai
from google.genai import types

from llm.ollama_client import OllamaClient
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


def _gemini_key(settings: WorkerSettings) -> str:
    k = (getattr(settings, "gemini_api_key", None) or "").strip()
    if k:
        return k
    import os

    return (os.environ.get("GEMINI_API_KEY") or "").strip()


async def gemini_generate_text(
    *,
    settings: WorkerSettings,
    system_instruction: str,
    user_text: str,
    trace_id: str,
) -> str:
    """
    Gọi Gemini async; backoff + jitter khi 429/5xx.
    """
    key = _gemini_key(settings)
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing (Secret or OMNI_GEMINI_API_KEY)")
    model = (settings.gemini_model or "gemini-2.0-flash").strip()
    client = genai.Client(api_key=key)
    cfg = types.GenerateContentConfig(
        system_instruction=system_instruction[:32000],
        temperature=0.35,
        max_output_tokens=8192,
    )
    last_err: Exception | None = None
    for attempt in range(settings.gemini_max_retries):
        try:
            logger.info(
                "[LAB_MODE] Unchained. Gemini generate trace=%s model=%s attempt=%s",
                trace_id,
                model,
                attempt + 1,
            )
            resp = await client.aio.models.generate_content(
                model=model,
                contents=user_text[:1_000_000],
                config=cfg,
            )
            text = (resp.text or "").strip()
            if text:
                return text
            last_err = RuntimeError("empty gemini response")
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            retryable = "429" in msg or "503" in msg or "resource" in msg or "quota" in msg
            if not retryable and attempt == 0:
                raise
            delay = settings.gemini_retry_base_delay_sec * (2**attempt) + random.uniform(0, 1.0)
            logger.warning("[%s] gemini attempt %s err=%s sleep=%.1fs", trace_id, attempt + 1, e, delay)
            await asyncio.sleep(delay)
    assert last_err is not None
    raise last_err


async def gemini_generate_with_ollama_fallback(
    *,
    settings: WorkerSettings,
    ollama: OllamaClient,
    system_instruction: str,
    user_text: str,
    trace_id: str,
    ollama_model: str,
    ollama_keep_alive: str,
) -> str:
    try:
        return await gemini_generate_text(
            settings=settings,
            system_instruction=system_instruction,
            user_text=user_text,
            trace_id=trace_id,
        )
    except Exception as e:
        logger.warning("[%s] gemini failed, spillover ollama: %s", trace_id, e)
        resp = await ollama.chat(
            model=ollama_model,
            messages=[
                {"role": "system", "content": system_instruction[:8000]},
                {"role": "user", "content": user_text[:12000]},
            ],
            options={"temperature": 0.35},
            keep_alive=ollama_keep_alive,
        )
        return ((resp.get("message") or {}).get("content") or "").strip()
