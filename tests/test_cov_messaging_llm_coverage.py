"""Coverage for ``src/messaging`` and ``src/llm`` without unittest.mock (fakes + local HTTP only)."""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from aiohttp import web
from aiokafka.errors import KafkaConnectionError

from llm.vllm_client import VLLMClient
from messaging.kafka_bus import (
    KafkaBus,
    create_producer,
    decode_kafka_value_to_fields,
    is_valid_kafka_topic,
    kafka_msg_id,
)


class _RecordingProducer:
    """Minimal stand-in for ``AIOKafkaProducer`` (duck-typed ``send_and_wait`` / ``stop``)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_and_wait(
        self,
        topic: str,
        *,
        value: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
        key: bytes | None = None,
    ) -> None:
        self.calls.append({"topic": topic, "value": value, "headers": headers, "key": key})

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
async def openai_compat_server() -> str:
    """Minimal OpenAI-compatible HTTP surface for ``AsyncOpenAI``."""

    async def chat_completions(request: web.Request) -> web.Response:
        payload = await request.json()
        assert payload.get("stream") is False
        content = "ok"
        if payload.get("response_format", {}).get("type") == "json_object":
            content = '{"a": 1}'
        return web.json_response(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": payload.get("model", "m"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def embeddings(request: web.Request) -> web.Response:
        body = await request.json()
        model = body.get("model", "emb")
        inp = body.get("input")
        if isinstance(inp, list):
            vecs = [[float(i), 0.5] for i in range(len(inp))]
        else:
            vecs = [[0.1, 0.2, 0.3]]
        data = [{"object": "embedding", "index": i, "embedding": vec} for i, vec in enumerate(vecs)]
        return web.json_response(
            {
                "object": "list",
                "data": data,
                "model": model,
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_post("/v1/embeddings", embeddings)

    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    site = web.TCPSite(runner, host, port)
    await site.start()
    base = f"http://{host}:{port}"
    try:
        yield base
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_create_producer_raises_when_broker_unreachable() -> None:
    with pytest.raises(KafkaConnectionError):
        await create_producer("127.0.0.1:1")


def test_kafka_msg_id_and_topic_validation() -> None:
    assert kafka_msg_id("omni-alerts", 2, 99) == "kafka-omni-alerts-2-99"
    assert is_valid_kafka_topic("omni-diagnostic-evidence") is True
    assert is_valid_kafka_topic("") is False
    assert is_valid_kafka_topic("bad topic") is False
    assert is_valid_kafka_topic("bad/topic") is False


def test_decode_kafka_value_to_fields_basic_and_nested() -> None:
    raw = json.dumps(
        {
            "kind": "evidence",
            "meta": {"x": 1},
            "tags": ["a", "b"],
            "empty": None,
            "text": "héllo",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    fields = decode_kafka_value_to_fields(raw)
    assert fields["kind"] == "evidence"
    assert json.loads(fields["meta"]) == {"x": 1}
    assert json.loads(fields["tags"]) == ["a", "b"]
    assert fields["empty"] == ""
    assert fields["text"] == "héllo"


def test_decode_kafka_value_to_fields_trace_header_overrides() -> None:
    body = {"kind": "x", "trace_id": "from-body"}
    raw = json.dumps(body).encode("utf-8")
    hdrs = [("trace_id", b"from-header"), ("other", b"ignored")]
    out = decode_kafka_value_to_fields(raw, headers=hdrs)
    assert out["trace_id"] == "from-header"


def test_decode_kafka_value_to_fields_trace_header_non_utf8() -> None:
    body = {"trace_id": "body"}
    raw = json.dumps(body).encode("utf-8")
    bad = b"\xff\xfe"
    out = decode_kafka_value_to_fields(raw, headers=[("trace_id", bad)])
    assert out["trace_id"] == bad.decode("utf-8", errors="replace")


def test_decode_kafka_value_to_fields_trace_header_non_bytes_fallback() -> None:
    body = {"trace_id": "body"}
    raw = json.dumps(body).encode("utf-8")
    out = decode_kafka_value_to_fields(raw, headers=[("trace_id", 42)])  # type: ignore[list-item]
    assert out["trace_id"] == "42"


def test_decode_kafka_value_long_key_truncation() -> None:
    long_key = "k" * 200
    raw = json.dumps({long_key: "v"}).encode("utf-8")
    out = decode_kafka_value_to_fields(raw)
    assert len(list(out.keys())[0]) == 128
    assert out["k" * 128] == "v"


@pytest.mark.asyncio
async def test_kafka_bus_skip_invalid_topic() -> None:
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    await bus.send_dict("", {"trace_id": "t"})
    await bus.send_dict("bad topic", {"trace_id": "t"})
    assert fake.calls == []


@pytest.mark.asyncio
async def test_kafka_bus_send_dict_trace_and_key() -> None:
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    env = {"trace_id": " top ", "payload": 1}
    await bus.send_dict("omni-actions", env, key=b"k1")
    assert len(fake.calls) == 1
    c = fake.calls[0]
    assert c["topic"] == "omni-actions"
    assert c["key"] == b"k1"
    decoded = json.loads(c["value"].decode("utf-8"))
    assert decoded["trace_id"] == " top "
    hdrs = c["headers"] or []
    assert ("trace_id", b"top") in hdrs


@pytest.mark.asyncio
async def test_kafka_bus_send_dict_trace_from_inner_data_json() -> None:
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    inner = {"trace_id": "inner-z", "x": 2}
    await bus.send_dict("omni-audit-chain", {"data": json.dumps(inner)})
    hdrs = fake.calls[0]["headers"] or []
    assert ("trace_id", b"inner-z") in hdrs


@pytest.mark.asyncio
async def test_kafka_bus_send_dict_inner_data_invalid_json_no_trace_header() -> None:
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    await bus.send_dict("omni-actions", {"data": "{not-json"})
    hdrs = fake.calls[0]["headers"]
    assert hdrs is None or hdrs == []


@pytest.mark.asyncio
async def test_kafka_bus_send_dict_no_trace_no_string_data() -> None:
    """Envelope without trace_id and without JSON-string ``data`` → empty trace (no headers)."""
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    await bus.send_dict("omni-actions", {"payload": 1})
    assert fake.calls[0]["headers"] is None


@pytest.mark.asyncio
async def test_kafka_bus_send_envelope_inner_and_close() -> None:
    fake = _RecordingProducer()
    bus = KafkaBus(fake)  # type: ignore[arg-type]
    await bus.send_envelope_inner(
        "omni-actions",
        {"cmd": "noop"},
        extra={"trace_id": "extra-1"},
    )
    outer = json.loads(fake.calls[0]["value"].decode("utf-8"))
    assert outer["trace_id"] == "extra-1"
    inner = json.loads(outer["data"])
    assert inner == {"cmd": "noop"}
    await bus.close()
    assert getattr(fake, "stopped", False) is True


@pytest.mark.asyncio
async def test_vllm_client_chat_embed_aclose(openai_compat_server: str) -> None:
    base = openai_compat_server.rstrip("/") + "/v1"
    client = VLLMClient(base_url=base, embed_url=base, timeout_s=30.0)
    try:
        out = await client.chat(
            model="qwen2.5:7b",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.2, "num_ctx": 512},
            keep_alive="5m",
        )
        assert out["message"]["role"] == "assistant"
        assert out["message"]["content"] == "ok"

        json_out = await client.chat(
            model="qwen2.5:7b",
            messages=[{"role": "user", "content": "json pls"}],
            format="json",
        )
        assert json.loads(json_out["message"]["content"]) == {"a": 1}

        emb = await client.embed(model="nomic-embed-text:latest", input="one", options={}, keep_alive=None)
        assert emb["embeddings"][0][:2] == [0.1, 0.2]

        emb_list = await client.embed(model="nomic-embed-text:latest", input=["a", "b"])
        assert len(emb_list["embeddings"]) == 2
        assert emb_list["embeddings"][0] == [0.0, 0.5]
        assert emb_list["embeddings"][1] == [1.0, 0.5]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_vllm_client_base_url_normalization(openai_compat_server: str) -> None:
    root = openai_compat_server.rstrip("/")
    # Constructor accepts host without /v1; model_post_init normalizes to .../v1
    client = VLLMClient(base_url=root, embed_url=root + "/v1", timeout_s=30.0)
    try:
        out = await client.chat(model="m", messages=[{"role": "user", "content": "x"}])
        assert out["message"]["content"] == "ok"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_vllm_client_separate_embed_base(openai_compat_server: str) -> None:
    """Chat and embed hit the same stub (distinct base_url fields both supported)."""

    root = openai_compat_server
    client = VLLMClient(base_url=root + "/v1", embed_url=root, timeout_s=30.0)
    try:
        e = await client.embed(model="e", input="z")
        assert len(e["embeddings"][0]) == 3
    finally:
        await client.aclose()
