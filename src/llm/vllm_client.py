"""Async LLM client — chat + embed via the OpenAI-compatible API.

Backed by Ollama on the macOS host (Apple Silicon GPU), reachable from K8s pods
via OrbStack DNS:
  Chat / Embed endpoint → http://host.orb.internal:11434/v1

Keeps the ``openai.AsyncOpenAI`` transport for architectural decoupling.
Ollama's /v1/chat/completions and /v1/embeddings are OpenAI-standard.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from enum import StrEnum
from typing import Any

import httpx
import openai
from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)

# Ollama doesn't enforce an API key; openai client requires a non-empty string.
_API_KEY = "ollama"


class _RateLimiter:
    """Sliding-window async limiter — caps requests/minute across chat+embed calls.

    Added for NVIDIA NIM's free-tier 40 rpm cap (shared across both endpoints on
    one API key). No-op for Ollama (rpm=None on VLLMClient → limiter unset).
    """

    def __init__(self, rpm: int) -> None:
        self._rpm = max(1, rpm)
        self._lock = asyncio.Lock()
        self._timestamps: collections.deque[float] = collections.deque()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    return
                sleep_for = 60.0 - (now - self._timestamps[0]) + 0.05
                await asyncio.sleep(max(0.05, sleep_for))

# Default context window passed as max_tokens when options.num_ctx is absent.
DEFAULT_MAX_TOKENS = 4096

_STREAM_FOR_SLI = os.getenv("OMNI_VLLM_STREAM_FOR_SLI", "false").strip().lower() in ("1", "true", "yes")
_SLI_METRICS = os.getenv("OMNI_LLM_SLI_METRICS_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _record_llm_client_sli(
    *,
    ttft_seconds: float,
    completion_seconds: float,
    output_tokens: int,
    model: str,
    call_kind: str,
    prompt_tokens: int = 0,
) -> None:
    if not _SLI_METRICS:
        return
    try:
        from workers.metrics_exporter import observe_llm_client_sli

        observe_llm_client_sli(
            ttft_seconds=ttft_seconds,
            completion_seconds=completion_seconds,
            output_tokens=output_tokens,
            model=model,
            call_kind=call_kind,
            prompt_tokens=prompt_tokens,
        )
    except ImportError:
        pass


def _record_call(
    *,
    model: str,
    call_kind: str,
    prompt: str,
    response: str = "",
    duration_ms: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    outcome: str = "ok",
    error: str = "",
    endpoint: str = "",
) -> None:
    """Structured log + call counter + Tempo span for one LLM call (best-effort)."""
    try:
        from pkg.observability.llm_observability import record_llm_call
    except ImportError:
        return
    record_llm_call(
        model=model,
        call_kind=call_kind,
        prompt=prompt,
        response=response,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        outcome=outcome,
        error=error,
        endpoint=endpoint,
    )


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    """Flatten messages for size/digest accounting. Never logged verbatim by default."""
    try:
        from pkg.observability.llm_observability import messages_text

        return messages_text(messages)
    except ImportError:
        return ""


def _kind_label(llm_call_kind: Any, format_json: bool) -> str:
    """Resolve the ``call_kind`` metric/log label.

    Most call sites reach :meth:`VLLMClient.chat` directly without passing
    ``llm_call_kind``, which used to bucket every series under ``"unspecified"``
    and made the label carry no information. Fall back to the response contract
    actually requested, which is the distinction the label is meant to express.
    """
    s = str(llm_call_kind) if llm_call_kind is not None else ""
    if s:
        return s
    return str(LLMCallKind.STRUCTURED) if format_json else str(LLMCallKind.CHAT)


class LLMCallKind(StrEnum):
    """Explicit call semantics for routing / logs (chat vs tool JSON contracts)."""

    CHAT = "chat"
    STRUCTURED = "structured"


def _base_url(url: str) -> str:
    """Normalise URL: strip trailing /v1 then re-append once."""
    return url.rstrip("/").removesuffix("/v1").rstrip("/") + "/v1"


class VLLMClient(BaseModel):
    """Async LLM client (Ollama backend) with Ollama-compatible chat/embed interface."""

    base_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        description="Ollama chat/completions base URL (with or without /v1 suffix).",
    )
    embed_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        description="Ollama embeddings base URL (with or without /v1 suffix).",
    )
    timeout_s: float = Field(default=120.0, ge=1.0)
    provider: str = Field(
        default="ollama",
        description="'ollama' (local, no auth, native /api/chat think-toggle) or "
        "'nim' (NVIDIA NIM — Bearer auth, OpenAI-compat only, rate-limited).",
    )
    api_key: str = Field(default=_API_KEY, description="Bearer token; dummy 'ollama' for local Ollama.")
    rate_limit_rpm: int | None = Field(
        default=None,
        description="Client-side requests/min cap shared by chat+embed (e.g. NIM free tier 40rpm). None = unbounded.",
    )
    embed_extra_body: dict[str, Any] | None = Field(
        default=None,
        description="Extra body merged into /v1/embeddings (e.g. NIM nv-embedqa input_type/truncate).",
    )

    model_config = {"arbitrary_types_allowed": True}

    _chat_client: openai.AsyncOpenAI = PrivateAttr()
    _embed_client: openai.AsyncOpenAI = PrivateAttr()
    _native_client: httpx.AsyncClient = PrivateAttr()
    _rate_limiter: "_RateLimiter | None" = PrivateAttr(default=None)

    def model_post_init(self, _context: Any) -> None:
        # max_retries=0: openai SDK mặc định retry 2 lần (3 lần thử tổng), MỖI lần chờ tới
        # timeout_s — hoàn toàn im lặng với caller. Đo thật 2026-08-04 (drill payment-api):
        # 2 lệnh lỗi mất 256s và 361s dù timeout_s=120 — khớp chính xác ~2x/~3x120s, tức
        # SDK đã âm thầm thử lại bên trong trước khi ném lỗi ra ngoài. Các caller (vd
        # services.analyst.diagnosis_loop) đã tự có retry/circuit-breaker RIÊNG ở tầng
        # ReAct-turn (_MAX_CONSECUTIVE_LLM_ERRORS) dựa trên giả định "mỗi lượt tốn tối đa
        # timeout_s" — retry ẩn của SDK phá giả định đó VÀ khi Ollama đã quá tải, mỗi "1 lượt
        # gọi" logic lại âm thầm biến thành 3 request xếp hàng, tự khuếch đại chính cơn quá
        # tải gây ra lỗi. Tắt hẳn, để caller quyết định có retry hay không.
        chat = openai.AsyncOpenAI(
            api_key=self.api_key,
            base_url=_base_url(self.base_url),
            timeout=self.timeout_s,
            max_retries=0,
        )
        embed = openai.AsyncOpenAI(
            api_key=self.api_key,
            base_url=_base_url(self.embed_url),
            timeout=self.timeout_s,
            max_retries=0,
        )
        self._rate_limiter = _RateLimiter(self.rate_limit_rpm) if self.rate_limit_rpm else None
        try:
            native = httpx.AsyncClient(timeout=self.timeout_s)
        except Exception:
            # Close already-opened clients before re-raising to avoid leaks.
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(chat.close())
                    loop.create_task(embed.close())
                else:
                    loop.run_until_complete(chat.close())
                    loop.run_until_complete(embed.close())
            except Exception:
                pass
            raise
        self._chat_client = chat
        self._embed_client = embed
        self._native_client = native

    async def aclose(self) -> None:
        await self._chat_client.close()
        await self._embed_client.close()
        await self._native_client.aclose()

    # ------------------------------------------------------------------
    # Chat — same signature as OllamaClient.chat()
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        format: str | None = None,  # "json" → response_format=json_object
        keep_alive: str | None = None,  # accepted for call-site compat, unused
        llm_call_kind: str | LLMCallKind | None = None,
    ) -> dict[str, Any]:
        """POST /v1/chat/completions.

        Returns ``{"message": {"role": "assistant", "content": "..."}}`` — same
        shape as OllamaClient so all response-parsing code in handlers is unchanged.

        Pass ``format="json"`` to enable ``response_format={"type": "json_object"}``
        (Ollama + OpenAI both honour this for structured CoT/ReAct output).

        ``llm_call_kind`` is for logs only (CHAT vs STRUCTURED); prefer
        :meth:`chat_plain` / :meth:`chat_structured` at new call sites.

        Success is recorded by the dispatch branches; this wrapper exists so a
        transport failure is observable too — otherwise a model that is down
        looks identical to a model that is merely idle.
        """
        t_start = time.perf_counter()
        try:
            return await self._chat_dispatch(
                model=model,
                messages=messages,
                stream=stream,
                options=options,
                format=format,
                llm_call_kind=llm_call_kind,
            )
        except Exception as exc:
            _record_call(
                model=model,
                call_kind=_kind_label(llm_call_kind, format == "json"),
                prompt=_prompt_text(messages),
                duration_ms=(time.perf_counter() - t_start) * 1000.0,
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def _chat_dispatch(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        format: str | None = None,
        llm_call_kind: str | LLMCallKind | None = None,
    ) -> dict[str, Any]:
        """Route to native /api/chat, non-streamed, or streamed completion."""
        opts = options or {}
        temperature: float = float(opts.get("temperature", 0.0))
        # num_predict caps output tokens; num_ctx is the context window (input+output).
        # Prefer num_predict for max_tokens so thinking-mode models don't exhaust the budget.
        max_tokens: int = int(opts.get("num_predict") or opts.get("num_ctx", DEFAULT_MAX_TOKENS))

        kind_s = str(llm_call_kind) if llm_call_kind is not None else ""
        fmt_json = format == "json"
        if kind_s:
            logger.debug(
                "event=ollama_chat model=%s llm_call_kind=%s format_json=%s",
                model,
                kind_s,
                fmt_json,
            )

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if format == "json":
            kwargs["response_format"] = {"type": "json_object"}
        # Pass think=False by default to disable Qwen3 extended thinking mode.
        # Thinking mode burns num_predict budget on internal reasoning, leaving no
        # tokens for the actual JSON response.  Call sites can opt-in via options={"think": True}.
        # think=None means "caller didn't specify" → use OpenAI compat endpoint (unchanged).
        # think=False means "caller explicitly disabled thinking" → use native /api/chat,
        # because Ollama's /v1/chat/completions ignores the think parameter for Qwen3 models.
        think_val = opts.get("think", None)

        kind_label = _kind_label(llm_call_kind, fmt_json)
        prompt_text = _prompt_text(messages)

        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()

        if think_val is False and self.provider == "ollama":
            return await self._chat_ollama_native(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                num_ctx=int(opts.get("num_ctx", DEFAULT_MAX_TOKENS)),
                format_json=fmt_json,
                kind_label=kind_label,
                prompt_text=prompt_text,
            )

        if not _STREAM_FOR_SLI:
            kwargs["stream"] = False
            _t0 = time.perf_counter()
            completion = await self._chat_client.chat.completions.create(**kwargs)
            _dur = max(0.0, time.perf_counter() - _t0)
            content = (completion.choices[0].message.content or "").strip()
            usage = getattr(completion, "usage", None)
            out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
            in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            if out_tok <= 0:
                out_tok = max(1, len(content) // 4)
            _record_llm_client_sli(
                ttft_seconds=_dur,
                completion_seconds=_dur,
                output_tokens=out_tok,
                model=model,
                call_kind=kind_label,
                prompt_tokens=in_tok,
            )
            _record_call(
                model=model,
                call_kind=kind_label,
                prompt=prompt_text,
                response=content,
                duration_ms=_dur * 1000.0,
                prompt_tokens=in_tok,
                completion_tokens=out_tok,
                endpoint="/v1/chat/completions",
            )
            return {"message": {"role": "assistant", "content": content}}

        kwargs["stream"] = True
        t_req = time.perf_counter()
        stream_resp = await self._chat_client.chat.completions.create(**kwargs)
        ttft: float | None = None
        parts: list[str] = []
        usage_tokens: int | None = None
        prompt_tokens: int | None = None
        async for chunk in stream_resp:
            try:
                ch = chunk.choices[0] if chunk.choices else None
                if ch is None:
                    continue
                delta = getattr(ch, "delta", None)
                piece = getattr(delta, "content", None) if delta is not None else None
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t_req
                    parts.append(piece)
                u = getattr(chunk, "usage", None)
                if u is not None and getattr(u, "completion_tokens", None) is not None:
                    usage_tokens = int(u.completion_tokens or 0)
                if u is not None and getattr(u, "prompt_tokens", None) is not None:
                    prompt_tokens = int(u.prompt_tokens or 0)
            except (IndexError, AttributeError, TypeError):
                continue

        total_s = max(0.0, time.perf_counter() - t_req)
        if ttft is None:
            ttft = total_s
        content = "".join(parts).strip()
        out_tok = int(usage_tokens or 0)
        if out_tok <= 0:
            out_tok = max(1, len(content) // 4)
        _record_llm_client_sli(
            ttft_seconds=float(ttft),
            completion_seconds=total_s,
            output_tokens=out_tok,
            model=model,
            call_kind=kind_label,
            prompt_tokens=int(prompt_tokens or 0),
        )
        _record_call(
            model=model,
            call_kind=kind_label,
            prompt=prompt_text,
            response=content,
            duration_ms=total_s * 1000.0,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=out_tok,
            endpoint="/v1/chat/completions[stream]",
        )
        return {"message": {"role": "assistant", "content": content}}

    async def _chat_ollama_native(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        num_ctx: int,
        format_json: bool,
        kind_label: str,
        prompt_text: str = "",
    ) -> dict[str, Any]:
        """POST /api/chat with think=false — native Ollama API, bypasses OpenAI compat layer."""
        base = self.base_url.rstrip("/").removesuffix("/v1").rstrip("/")
        url = f"{base}/api/chat"
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "think": False,
            "stream": False,
            "options": {"num_predict": max_tokens, "num_ctx": num_ctx, "temperature": temperature},
        }
        if format_json:
            body["format"] = "json"
        _t0 = time.perf_counter()
        resp = await self._native_client.post(url, json=body)
        resp.raise_for_status()
        _dur = max(0.0, time.perf_counter() - _t0)
        data = resp.json()
        content = (data.get("message") or {}).get("content") or ""
        content = content.strip()
        out_tok = int(data.get("eval_count") or 0) or max(1, len(content) // 4)
        in_tok = int(data.get("prompt_eval_count") or 0)
        _record_llm_client_sli(
            ttft_seconds=_dur,
            completion_seconds=_dur,
            output_tokens=out_tok,
            model=model,
            call_kind=kind_label,
            prompt_tokens=in_tok,
        )
        _record_call(
            model=model,
            call_kind=kind_label,
            prompt=prompt_text,
            response=content,
            duration_ms=_dur * 1000.0,
            prompt_tokens=in_tok,
            completion_tokens=out_tok,
            endpoint="/api/chat",
        )
        return {"message": {"role": "assistant", "content": content}}

    async def chat_plain(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """Unstructured completion (no ``response_format``)."""
        return await self.chat(
            model=model,
            messages=messages,
            stream=stream,
            options=options,
            format=None,
            keep_alive=keep_alive,
            llm_call_kind=LLMCallKind.CHAT,
        )

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """JSON object completion (maps to OpenAI ``json_object``)."""
        return await self.chat(
            model=model,
            messages=messages,
            stream=stream,
            options=options,
            format="json",
            keep_alive=keep_alive,
            llm_call_kind=LLMCallKind.STRUCTURED,
        )

    # ------------------------------------------------------------------
    # Embed — same signature as OllamaClient.embed()
    # ------------------------------------------------------------------

    async def embed(
        self,
        *,
        model: str,
        input: str | list[str],
        options: dict[str, Any] | None = None,  # accepted for compat, unused
        keep_alive: str | None = None,  # accepted for compat, unused
    ) -> dict[str, Any]:
        """POST /v1/embeddings.

        Returns ``{"embeddings": [[float, ...]]}`` — same shape as OllamaClient
        so ``_embedding_from_response`` in handlers is unchanged.
        """
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()
        kwargs: dict[str, Any] = {"model": model, "input": input}
        if self.embed_extra_body:
            kwargs["extra_body"] = self.embed_extra_body
        response = await self._embed_client.embeddings.create(**kwargs)
        vectors = [d.embedding for d in response.data]
        return {"embeddings": vectors}
