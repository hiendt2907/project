"""VLLMClient records every chat call — success, failure, and the call_kind label."""

from __future__ import annotations

import logging

import pytest

from llm.vllm_client import LLMCallKind, VLLMClient, _kind_label


# --------------------------------------------------------------- kind label --


def test_kind_label_prefers_the_explicit_call_kind():
    assert _kind_label(LLMCallKind.STRUCTURED, False) == "structured"
    assert _kind_label("custom", True) == "custom"


def test_kind_label_falls_back_to_the_response_contract_not_unspecified():
    """Regression: every series used to collapse into call_kind="unspecified"."""
    assert _kind_label(None, True) == "structured"
    assert _kind_label(None, False) == "chat"
    assert "unspecified" not in (_kind_label(None, True) + _kind_label(None, False))


# ------------------------------------------------------------ record wiring --


@pytest.fixture
def client():
    return VLLMClient(base_url="http://llm.invalid:11434")


@pytest.fixture
def recorded(monkeypatch):
    """Capture record_llm_call payloads at the module the client imports from."""
    calls: list[dict] = []
    import pkg.observability.llm_observability as obs

    monkeypatch.setattr(obs, "record_llm_call", lambda **kw: calls.append(kw))
    return calls


async def test_native_chat_path_records_prompt_response_and_tokens(
    client, recorded, monkeypatch
):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {"content": "root cause: disk full"},
                "eval_count": 11,
                "prompt_eval_count": 500,
            }

    async def _post(_url, json=None):
        return _Resp()

    monkeypatch.setattr(client._native_client, "post", _post)

    out = await client.chat(
        model="qwen2.5-coder:7b",
        messages=[{"role": "user", "content": "why is disk full"}],
        options={"think": False},
        format="json",
    )

    assert out["message"]["content"] == "root cause: disk full"
    assert len(recorded) == 1
    rec = recorded[0]
    assert rec["model"] == "qwen2.5-coder:7b"
    assert rec["call_kind"] == "structured"
    assert rec["outcome"] == "ok"
    assert rec["prompt"] == "why is disk full"
    assert rec["response"] == "root cause: disk full"
    assert rec["prompt_tokens"] == 500
    assert rec["completion_tokens"] == 11
    assert rec["endpoint"] == "/api/chat"
    assert rec["duration_ms"] >= 0


async def test_non_streamed_openai_path_records_the_call(client, recorded, monkeypatch):
    class _Msg:
        content = "nginx is stopped"

    class _Choice:
        message = _Msg()

    class _Usage:
        completion_tokens = 7
        prompt_tokens = 120

    class _Completion:
        choices = [_Choice()]
        usage = _Usage()

    async def _create(**_kw):
        return _Completion()

    monkeypatch.setattr(client._chat_client.chat.completions, "create", _create)

    await client.chat(
        model="m",
        messages=[{"role": "user", "content": "status of nginx"}],
    )

    assert len(recorded) == 1
    rec = recorded[0]
    assert rec["outcome"] == "ok"
    assert rec["call_kind"] == "chat"
    assert rec["response"] == "nginx is stopped"
    assert rec["prompt_tokens"] == 120
    assert rec["completion_tokens"] == 7
    assert rec["endpoint"] == "/v1/chat/completions"


async def test_transport_failure_is_recorded_then_reraised(client, recorded, monkeypatch):
    """A model that is down must be visible, not merely absent from the metrics."""

    async def _create(**_kw):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(client._chat_client.chat.completions, "create", _create)

    with pytest.raises(ConnectionError):
        await client.chat(model="m", messages=[{"role": "user", "content": "hello"}])

    assert len(recorded) == 1
    rec = recorded[0]
    assert rec["outcome"] == "error"
    assert "ConnectionError" in rec["error"]
    assert "connection refused" in rec["error"]
    assert rec["prompt"] == "hello"
    assert rec["call_kind"] == "chat"


async def test_failure_on_structured_call_keeps_the_structured_label(
    client, recorded, monkeypatch
):
    async def _create(**_kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client._chat_client.chat.completions, "create", _create)

    with pytest.raises(TimeoutError):
        await client.chat_structured(model="m", messages=[{"role": "user", "content": "x"}])

    assert recorded[0]["call_kind"] == "structured"
    assert recorded[0]["outcome"] == "error"


async def test_observability_failure_does_not_break_the_chat_call(
    client, monkeypatch, caplog
):
    """Hot-path guarantee: broken telemetry must not fail a diagnosis."""
    import pkg.observability.llm_observability as obs

    def _boom(**_kw):
        raise RuntimeError("sink down")

    monkeypatch.setattr(obs, "_emit_metrics", _boom)

    class _Msg:
        content = "ok"

    class _Choice:
        message = _Msg()

    class _Completion:
        choices = [_Choice()]
        usage = None

    async def _create(**_kw):
        return _Completion()

    monkeypatch.setattr(client._chat_client.chat.completions, "create", _create)

    with caplog.at_level(logging.WARNING):
        out = await client.chat(model="m", messages=[{"role": "user", "content": "q"}])

    assert out["message"]["content"] == "ok"


# ----------------------------------------------------------------- metrics --


def test_observe_llm_call_registers_series_including_errors():
    from workers.metrics_exporter import _ensure_metrics, observe_llm_call

    _ensure_metrics()
    observe_llm_call(
        model="qwen2.5-coder:7b",
        call_kind="structured",
        outcome="error",
        prompt_chars=4096,
        response_chars=0,
    )

    from prometheus_client import REGISTRY

    val = REGISTRY.get_sample_value(
        "omni_llm_calls_total",
        {"model": "qwen2.5-coder:7b", "call_kind": "structured", "outcome": "error"},
    )
    assert val is not None and val >= 1


def test_observe_llm_call_truncates_oversized_label_values():
    from workers.metrics_exporter import _ensure_metrics, observe_llm_call

    _ensure_metrics()
    # Must not raise; long model names are clipped rather than rejected.
    observe_llm_call(
        model="m" * 500,
        call_kind="k" * 500,
        outcome="o" * 500,
        prompt_chars=1,
        response_chars=1,
    )


def test_inc_rag_gate_outcome_registers_series():
    from workers.metrics_exporter import _ensure_metrics, inc_rag_gate_outcome

    _ensure_metrics()
    inc_rag_gate_outcome(outcome="cache_hit", collection="k8s_expert")

    from prometheus_client import REGISTRY

    val = REGISTRY.get_sample_value(
        "omni_rag_gate_outcome_total",
        {"outcome": "cache_hit", "collection": "k8s_expert"},
    )
    assert val is not None and val >= 1
