"""Wave W1: coverage for gateway trace_context, messaging/kafka_bus, llm/gemini_client."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# --- gateway.trace_context ---


def test_trace_context_push_pop_and_current() -> None:
    from gateway import trace_context as tc

    assert tc.current_gateway_trace_id() == ""
    tok = tc.push_gateway_trace_id("req-trace-1")
    assert tc.current_gateway_trace_id() == "req-trace-1"
    tc.pop_gateway_trace_id(tok)
    assert tc.current_gateway_trace_id() == ""


def test_trace_filter_prefixes_message() -> None:
    from gateway import trace_context as tc

    filt = tc.OmniGatewayTraceFilter()
    tok = tc.push_gateway_trace_id("tid-aa")
    try:
        rec = logging.LogRecord("n", logging.INFO, __file__, 1, "plain message", (), None)
        assert filt.filter(rec) is True
        assert "trace_id=tid-aa" in rec.getMessage()
    finally:
        tc.pop_gateway_trace_id(tok)


def test_trace_filter_skips_when_trace_already_in_message() -> None:
    from gateway import trace_context as tc

    filt = tc.OmniGatewayTraceFilter()
    tok = tc.push_gateway_trace_id("tid-bb")
    try:
        rec = logging.LogRecord(
            "n",
            logging.INFO,
            __file__,
            1,
            "prefix [trace_id=tid-bb] suffix",
            (),
            None,
        )
        assert filt.filter(rec) is True
        assert rec.getMessage() == "prefix [trace_id=tid-bb] suffix"
    finally:
        tc.pop_gateway_trace_id(tok)


def test_trace_filter_skips_when_done_flag_set() -> None:
    from gateway import trace_context as tc

    filt = tc.OmniGatewayTraceFilter()
    tok = tc.push_gateway_trace_id("tid-dd")
    try:
        rec = logging.LogRecord("n", logging.INFO, __file__, 1, "untouched", (), None)
        rec._omni_gateway_trace_done = True  # type: ignore[attr-defined]
        assert filt.filter(rec) is True
        assert rec.getMessage() == "untouched"
    finally:
        tc.pop_gateway_trace_id(tok)


def test_trace_filter_getmessage_error_returns_true() -> None:
    from gateway import trace_context as tc

    filt = tc.OmniGatewayTraceFilter()
    tok = tc.push_gateway_trace_id("tid-cc")
    try:
        rec = MagicMock()
        rec._omni_gateway_trace_done = False
        rec.getMessage.side_effect = ValueError("bad fmt")
        assert filt.filter(rec) is True
    finally:
        tc.pop_gateway_trace_id(tok)


def test_install_gateway_trace_logging_registers_filter() -> None:
    from gateway import trace_context as tc

    loggers: dict[str, MagicMock] = {}

    def _get(name: str) -> MagicMock:
        if name not in loggers:
            m = MagicMock()
            m.handlers = []
            m.filters = []

            def _add(f: logging.Filter) -> None:
                m.filters.append(f)

            m.addFilter = MagicMock(side_effect=_add)
            m.setLevel = MagicMock()
            m.addHandler = MagicMock()
            loggers[name] = m
        return loggers[name]

    with patch.object(tc.logging, "getLogger", side_effect=_get):
        tc.install_gateway_trace_logging()

    assert any(isinstance(f, tc.OmniGatewayTraceFilter) for f in loggers[""].filters)
    assert any(isinstance(f, tc.OmniGatewayTraceFilter) for f in loggers["uvicorn"].filters)
    loggers["gateway"].addHandler.assert_called()


# --- messaging.kafka_bus ---


def test_is_valid_kafka_topic() -> None:
    from messaging.kafka_bus import is_valid_kafka_topic

    assert is_valid_kafka_topic("") is False
    assert is_valid_kafka_topic("bad topic!") is False
    assert is_valid_kafka_topic("omni-alerts") is True
    assert is_valid_kafka_topic("  valid.topic-1_2  ") is True


def test_kafka_msg_id() -> None:
    from messaging.kafka_bus import kafka_msg_id

    assert kafka_msg_id("t", 0, 42) == "kafka-t-0-42"


def test_decode_kafka_value_to_fields_scalar_dict_list_none() -> None:
    from messaging.kafka_bus import decode_kafka_value_to_fields

    long_k = "k" * 200
    raw = json.dumps(
        {
            "a": 1,
            "b": {"nested": True},
            "c": [1, 2],
            "d": None,
            long_k: "truncated-key",
        },
        ensure_ascii=False,
    ).encode()
    out = decode_kafka_value_to_fields(raw)
    assert out["a"] == "1"
    assert '"nested"' in out["b"] or "nested" in out["b"]
    assert out["d"] == ""
    assert long_k[:128] in out
    assert out[long_k[:128]] == "truncated-key"


def test_decode_kafka_value_trace_header_and_decode_fallback() -> None:
    from messaging.kafka_bus import decode_kafka_value_to_fields

    raw = json.dumps({"foo": "bar"}).encode()

    class _BadBytes:
        def decode(self, *_a, **_kw):
            raise RuntimeError("no decode")

        def __str__(self) -> str:
            return "trace-from-str"

    out = decode_kafka_value_to_fields(raw, headers=[("trace_id", _BadBytes())])
    assert out["trace_id"] == "trace-from-str"


@pytest.mark.asyncio
async def test_create_producer_calls_start() -> None:
    from messaging import kafka_bus as kb

    mock_p = MagicMock()
    mock_p.start = AsyncMock()
    with patch.object(kb, "AIOKafkaProducer", return_value=mock_p) as ctor:
        p = await kb.create_producer(" 127.0.0.1:9092 ")
    ctor.assert_called_once()
    assert ctor.call_args.kwargs["bootstrap_servers"] == "127.0.0.1:9092"
    assert p is mock_p
    mock_p.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_kafka_bus_invalid_topic_skips_send() -> None:
    from messaging.kafka_bus import KafkaBus

    prod = AsyncMock()
    bus = KafkaBus(prod)
    await bus.send_dict("bad topic!", {"trace_id": "t"})
    prod.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_kafka_bus_send_dict_headers_and_inner_trace() -> None:
    from messaging.kafka_bus import KafkaBus

    prod = AsyncMock()
    bus = KafkaBus(prod)
    await bus.send_dict("omni-alerts", {"trace_id": "root", "k": "v"})
    prod.send_and_wait.assert_awaited()
    hdrs = prod.send_and_wait.call_args.kwargs.get("headers")
    assert hdrs and hdrs[0][0] == "trace_id"

    prod.reset_mock()
    inner = json.dumps({"trace_id": "from-inner", "x": 1})
    await bus.send_dict("omni-alerts", {"data": inner})
    hdrs2 = prod.send_and_wait.call_args.kwargs.get("headers")
    assert hdrs2 and b"from-inner" in hdrs2[0][1]

    prod.reset_mock()
    await bus.send_dict("omni-alerts", {"data": "not-json"})
    kw = prod.send_and_wait.call_args.kwargs
    assert kw.get("headers") in (None, [])

    prod.reset_mock()
    await bus.send_dict("omni-alerts", {"data": '{"trace_id": "broken"'})
    prod.send_and_wait.assert_awaited()


@pytest.mark.asyncio
async def test_kafka_bus_send_envelope_inner_and_close() -> None:
    from messaging.kafka_bus import KafkaBus

    prod = AsyncMock()
    bus = KafkaBus(prod)
    await bus.send_envelope_inner("omni-topic", {"a": 1}, extra={"trace_id": "e1"})
    prod.send_and_wait.assert_awaited()
    await bus.close()
    prod.stop.assert_awaited_once()


# --- llm.gemini_client ---


@pytest.mark.asyncio
async def test_gemini_generate_text_missing_key_raises() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(gemini_api_key="", gemini_model="m", gemini_max_retries=1)
    with patch.object(gc, "_gemini_key", return_value=""):
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            await gc.gemini_generate_text(
                settings=settings,
                system_instruction="sys",
                user_text="hi",
                trace_id="tr",
            )


@pytest.mark.asyncio
async def test_gemini_generate_text_success() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(
        gemini_api_key="secret",
        gemini_model="gemini-2.0-flash",
        gemini_max_retries=2,
        gemini_retry_base_delay_sec=0.01,
    )
    resp = MagicMock()
    resp.text = "  answer  "
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=resp)
    with patch.object(gc, "genai") as genai_mod:
        genai_mod.Client.return_value = mock_client
        out = await gc.gemini_generate_text(
            settings=settings,
            system_instruction="sys",
            user_text="user",
            trace_id="tr-1",
        )
    assert out == "answer"
    genai_mod.Client.assert_called_once_with(api_key="secret")


@pytest.mark.asyncio
async def test_gemini_generate_text_retries_empty_then_ok() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(
        gemini_api_key="k",
        gemini_model="m",
        gemini_max_retries=3,
        gemini_retry_base_delay_sec=0.001,
    )
    bad = MagicMock()
    bad.text = ""
    good = MagicMock()
    good.text = "ok"
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(side_effect=[bad, good])
    with patch.object(gc, "genai") as genai_mod:
        genai_mod.Client.return_value = mock_client
        with patch("llm.gemini_client.asyncio.sleep", new=AsyncMock()):
            out = await gc.gemini_generate_text(
                settings=settings,
                system_instruction="s",
                user_text="u",
                trace_id="tr-2",
            )
    assert out == "ok"


@pytest.mark.asyncio
async def test_gemini_generate_text_retries_on_429_then_success() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(
        gemini_api_key="k",
        gemini_model="m",
        gemini_max_retries=3,
        gemini_retry_base_delay_sec=0.001,
    )
    ok = MagicMock()
    ok.text = "after-retry"
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[Exception("Error 429 rate limited"), ok]
    )
    with patch.object(gc, "genai") as genai_mod:
        genai_mod.Client.return_value = mock_client
        with patch("llm.gemini_client.asyncio.sleep", new=AsyncMock()):
            out = await gc.gemini_generate_text(
                settings=settings,
                system_instruction="s",
                user_text="u",
                trace_id="tr-retry",
            )
    assert out == "after-retry"


@pytest.mark.asyncio
async def test_gemini_generate_text_exhausts_empty_responses() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(
        gemini_api_key="k",
        gemini_model="m",
        gemini_max_retries=2,
        gemini_retry_base_delay_sec=0.001,
    )
    empty = MagicMock()
    empty.text = ""
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=empty)
    with patch.object(gc, "genai") as genai_mod:
        genai_mod.Client.return_value = mock_client
        with patch("llm.gemini_client.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(RuntimeError, match="empty gemini response"):
                await gc.gemini_generate_text(
                    settings=settings,
                    system_instruction="s",
                    user_text="u",
                    trace_id="tr-empty",
                )


@pytest.mark.asyncio
async def test_gemini_generate_text_non_retryable_raises_immediately() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(
        gemini_api_key="k",
        gemini_model="m",
        gemini_max_retries=4,
        gemini_retry_base_delay_sec=0.001,
    )
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(side_effect=ValueError("invalid argument"))
    with patch.object(gc, "genai") as genai_mod:
        genai_mod.Client.return_value = mock_client
        with pytest.raises(ValueError, match="invalid argument"):
            await gc.gemini_generate_text(
                settings=settings,
                system_instruction="s",
                user_text="u",
                trace_id="tr-3",
            )


@pytest.mark.asyncio
async def test_gemini_generate_with_llm_fallback_on_gemini_fail() -> None:
    from llm import gemini_client as gc

    settings = SimpleNamespace(gemini_api_key="k", gemini_model="m", gemini_max_retries=1)
    llm = AsyncMock()
    llm.chat = AsyncMock(
        return_value={"message": {"role": "assistant", "content": "  from-vllm "}}
    )
    with patch.object(gc, "gemini_generate_text", new=AsyncMock(side_effect=RuntimeError("down"))):
        out = await gc.gemini_generate_with_llm_fallback(
            settings=settings,
            llm=llm,
            system_instruction="sys",
            user_text="user",
            trace_id="tr-4",
            llm_model="qwen",
        )
    assert out == "from-vllm"
    llm.chat.assert_awaited()


def test_gemini_key_prefers_settings_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import gemini_client as gc

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    s1 = SimpleNamespace(gemini_api_key="  inline-key  ")
    assert gc._gemini_key(s1) == "inline-key"

    s2 = SimpleNamespace()
    assert gc._gemini_key(s2) == "from-env"
