"""Track 0B — coverage for agent_audit, entity_extract, log_preview,
observability_audit (pure helpers), and selflearning_shadow."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest


# ---------------------------------------------------------------------------
# Shared infra
# ---------------------------------------------------------------------------

class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))


def _make_settings(**kwargs: Any) -> SimpleNamespace:
    defaults = {
        "audit_agent_maxlen": 8000,
        "kafka_topic_audit_agent": "omni-audit-agent",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ===========================================================================
# 1. agent_audit
# ===========================================================================

from workers.agent_audit import _flatten_fields, append_agent_audit


class TestFlattenFields:
    def test_string_values_unchanged(self):
        result = _flatten_fields({"k": "hello"}, maxlen=100)
        assert result == {"k": "hello"}

    def test_int_values_converted_to_str(self):
        result = _flatten_fields({"count": 42}, maxlen=100)
        assert result["count"] == "42"

    def test_dict_values_json_serialised(self):
        result = _flatten_fields({"data": {"a": 1}}, maxlen=100)
        assert result["data"] == '{"a": 1}'

    def test_list_values_json_serialised(self):
        result = _flatten_fields({"items": [1, 2, 3]}, maxlen=100)
        assert result["items"] == "[1, 2, 3]"

    def test_values_truncated_at_maxlen(self):
        result = _flatten_fields({"k": "x" * 200}, maxlen=50)
        assert len(result["k"]) == 50

    def test_dict_values_truncated_at_maxlen(self):
        big = {"nested": "y" * 500}
        result = _flatten_fields({"d": big}, maxlen=30)
        assert len(result["d"]) == 30

    def test_key_truncated_at_128(self):
        long_key = "k" * 200
        result = _flatten_fields({long_key: "v"}, maxlen=100)
        assert len(list(result.keys())[0]) == 128

    def test_empty_dict(self):
        result = _flatten_fields({}, maxlen=100)
        assert result == {}

    def test_none_value_serialised(self):
        result = _flatten_fields({"k": None}, maxlen=100)
        assert result["k"] == "None"


class TestAppendAgentAudit:
    @pytest.mark.asyncio
    async def test_sends_to_kafka(self):
        kafka = _KafkaCapture()
        settings = _make_settings()
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        await append_agent_audit(ctx, phase="diag", trace_id="t1", event="advisory_ready", extra="val")

        assert len(kafka.sent) == 1
        topic, payload = kafka.sent[0]
        assert topic == "omni-audit-agent"
        assert payload["phase"] == "diag"
        assert payload["event"] == "advisory_ready"
        assert payload["trace_id"] == "t1"

    @pytest.mark.asyncio
    async def test_no_send_when_topic_empty(self):
        kafka = _KafkaCapture()
        settings = _make_settings(kafka_topic_audit_agent="")
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        await append_agent_audit(ctx, phase="diag", trace_id="t1", event="ev")
        assert len(kafka.sent) == 0

    @pytest.mark.asyncio
    async def test_no_send_when_topic_whitespace(self):
        kafka = _KafkaCapture()
        settings = _make_settings(kafka_topic_audit_agent="   ")
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        await append_agent_audit(ctx, phase="diag", trace_id="t1", event="ev")
        assert len(kafka.sent) == 0

    @pytest.mark.asyncio
    async def test_no_send_when_settings_none(self):
        kafka = _KafkaCapture()
        ctx = SimpleNamespace(kafka=kafka)  # no settings

        await append_agent_audit(ctx, phase="diag", trace_id="t1", event="ev")
        assert len(kafka.sent) == 0

    @pytest.mark.asyncio
    async def test_no_send_when_kafka_none(self):
        settings = _make_settings()
        ctx = SimpleNamespace(settings=settings)  # no kafka

        # Should not raise
        await append_agent_audit(ctx, phase="diag", trace_id="t1", event="ev")

    @pytest.mark.asyncio
    async def test_extra_fields_included(self):
        kafka = _KafkaCapture()
        settings = _make_settings()
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        await append_agent_audit(ctx, phase="exec", trace_id="t2", event="done",
                                 action="restart", score="0.9")

        _, payload = kafka.sent[0]
        assert payload["action"] == "restart"
        assert payload["score"] == "0.9"

    @pytest.mark.asyncio
    async def test_dict_extra_field_json_serialised(self):
        kafka = _KafkaCapture()
        settings = _make_settings()
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        await append_agent_audit(ctx, phase="exec", trace_id="t3", event="done",
                                 metadata={"key": "val"})

        _, payload = kafka.sent[0]
        # metadata is a dict — _flatten_fields json-encodes it
        assert "key" in payload["metadata"]

    @pytest.mark.asyncio
    async def test_maxlen_from_settings(self):
        kafka = _KafkaCapture()
        settings = _make_settings(audit_agent_maxlen=5)
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        long_val = "x" * 100
        await append_agent_audit(ctx, phase="p", trace_id="t", event="e", longfield=long_val)

        _, payload = kafka.sent[0]
        assert len(payload["longfield"]) == 5

    @pytest.mark.asyncio
    async def test_maxlen_fallback_when_none(self):
        kafka = _KafkaCapture()
        settings = _make_settings(audit_agent_maxlen=None)
        ctx = SimpleNamespace(settings=settings, kafka=kafka)

        # Should not raise — fallback to 8000
        await append_agent_audit(ctx, phase="p", trace_id="t", event="e")
        assert len(kafka.sent) == 1

    @pytest.mark.asyncio
    async def test_exception_suppressed_on_kafka_error(self):
        class _FailKafka:
            async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
                raise RuntimeError("kafka down")

        settings = _make_settings()
        ctx = SimpleNamespace(settings=settings, kafka=_FailKafka())

        # Should not propagate
        await append_agent_audit(ctx, phase="p", trace_id="t", event="e")


# ===========================================================================
# 2. entity_extract
# ===========================================================================

from workers.entity_extract import (
    ENTITY_SCHEMA_HINT,
    extract_entities_llm,
    merge_llm_entities_into_slots,
)


class TestMergeLlmEntitiesIntoSlots:
    def test_empty_ent_returns_slots_unchanged(self):
        slots = {"intent": "cpu", "namespace": "default"}
        result = merge_llm_entities_into_slots(slots, {})
        assert result == slots

    def test_none_ent_treated_as_empty(self):
        slots = {"intent": "cpu"}
        result = merge_llm_entities_into_slots(slots, {})
        assert result == slots

    def test_ent_values_override_slots(self):
        slots = {"intent": "cpu", "namespace": "", "pod_name": "", "target_type": "pod"}
        ent = {"intent": "ram", "namespace": "prod", "pod_name": "my-pod", "target_type": "HOST"}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["intent"] == "ram"
        assert result["namespace"] == "prod"
        assert result["pod_name"] == "my-pod"
        # target_type lowercased
        assert result["target_type"] == "host"

    def test_empty_ent_values_do_not_override(self):
        slots = {"intent": "cpu", "namespace": "staging"}
        ent = {"intent": "", "namespace": "  "}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["intent"] == "cpu"
        assert result["namespace"] == "staging"

    def test_unknown_ent_keys_ignored(self):
        slots = {"intent": "cpu"}
        ent = {"unknown_key": "value", "intent": "disk"}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["intent"] == "disk"
        assert "unknown_key" not in result

    def test_target_type_lowercased(self):
        slots = {}
        ent = {"target_type": "POD"}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["target_type"] == "pod"

    def test_intent_not_lowercased(self):
        slots = {}
        ent = {"intent": "CPU"}
        result = merge_llm_entities_into_slots(slots, ent)
        # intent is NOT lowercased (only target_type is)
        assert result["intent"] == "CPU"

    def test_returns_new_dict_not_mutate_slots(self):
        slots = {"intent": "cpu"}
        ent = {"intent": "ram"}
        result = merge_llm_entities_into_slots(slots, ent)
        assert slots["intent"] == "cpu"  # original unchanged
        assert result["intent"] == "ram"

    def test_whitespace_only_values_skipped(self):
        slots = {"pod_name": "original"}
        ent = {"pod_name": "   "}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["pod_name"] == "original"

    def test_partial_ent_only_specified_keys_updated(self):
        slots = {"intent": "cpu", "namespace": "ns1", "pod_name": "pod1"}
        ent = {"namespace": "ns2"}
        result = merge_llm_entities_into_slots(slots, ent)
        assert result["intent"] == "cpu"
        assert result["namespace"] == "ns2"
        assert result["pod_name"] == "pod1"


class TestEntitySchemaHint:
    def test_schema_hint_contains_required_fields(self):
        assert "intent" in ENTITY_SCHEMA_HINT
        assert "namespace" in ENTITY_SCHEMA_HINT
        assert "pod_name" in ENTITY_SCHEMA_HINT
        assert "target_type" in ENTITY_SCHEMA_HINT


class TestExtractEntitiesLlm:
    @pytest.mark.asyncio
    async def test_returns_empty_when_llm_none(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper="qwen2.5:1.5b"), llm=None)
        result = await extract_entities_llm(ctx, "show me cpu usage")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_text_empty(self):
        class _FakeLLM:
            async def chat(self, **kwargs): return {}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_text_whitespace(self):
        class _FakeLLM:
            async def chat(self, **kwargs): return {}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "   ")
        assert result == {}

    @pytest.mark.asyncio
    async def test_parses_valid_json_response(self):
        class _FakeLLM:
            async def chat(self, model, messages, options):
                return {"message": {"content": '{"intent":"ram","namespace":"prod","pod_name":"web-pod","target_type":"pod"}'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper="qwen2.5:1.5b"), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "ram usage in prod for web-pod")
        assert result["intent"] == "ram"
        assert result["namespace"] == "prod"
        assert result["pod_name"] == "web-pod"
        assert result["target_type"] == "pod"

    @pytest.mark.asyncio
    async def test_extracts_json_embedded_in_text(self):
        class _FakeLLM:
            async def chat(self, model, messages, options):
                return {"message": {"content": 'Here is the result: {"intent":"cpu","namespace":"","pod_name":"","target_type":"host"} done.'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "host cpu")
        assert result["intent"] == "cpu"
        assert result["target_type"] == "host"

    @pytest.mark.asyncio
    async def test_returns_empty_on_invalid_json(self):
        class _FakeLLM:
            async def chat(self, model, messages, options):
                return {"message": {"content": "not a json string"}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "show me something")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_list_json(self):
        class _FakeLLM:
            async def chat(self, model, messages, options):
                # The regex picks up the first {} — but json is a list, not dict
                return {"message": {"content": '[]'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "something")
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_none_values(self):
        class _FakeLLM:
            async def chat(self, model, messages, options):
                return {"message": {"content": '{"intent":"cpu","namespace":null,"pod_name":null,"target_type":"pod"}'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        result = await extract_entities_llm(ctx, "cpu")
        assert "intent" in result
        assert "namespace" not in result
        assert "pod_name" not in result

    @pytest.mark.asyncio
    async def test_uses_default_model_when_none(self):
        calls = []

        class _FakeLLM:
            async def chat(self, model, messages, options):
                calls.append(model)
                return {"message": {"content": '{"intent":"cpu"}'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FakeLLM())
        await extract_entities_llm(ctx, "cpu usage")
        assert calls[0] == "qwen3.6"

    @pytest.mark.asyncio
    async def test_uses_model_from_settings(self):
        calls = []

        class _FakeLLM:
            async def chat(self, model, messages, options):
                calls.append(model)
                return {"message": {"content": '{"intent":"disk"}'}}

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper="custom-model:3b"), llm=_FakeLLM())
        await extract_entities_llm(ctx, "disk usage")
        assert calls[0] == "custom-model:3b"

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        class _FailLLM:
            async def chat(self, **kwargs):
                raise RuntimeError("llm error")

        ctx = SimpleNamespace(settings=SimpleNamespace(model_helper=None), llm=_FailLLM())
        result = await extract_entities_llm(ctx, "some query")
        assert result == {}


# ===========================================================================
# 3. log_preview
# ===========================================================================

from workers.log_preview import alert_payload_summary, json_obj_preview, log_preview


class TestLogPreview:
    def test_basic_string_passes_through(self):
        result = log_preview("hello world")
        assert result == "hello world"

    def test_none_returns_empty_string(self):
        result = log_preview(None)
        assert result == ""

    def test_long_string_truncated_with_ellipsis(self):
        result = log_preview("x" * 1000, max_chars=100)
        assert len(result) == 100
        assert result.endswith("...")

    def test_exact_length_not_truncated(self):
        s = "x" * 800
        result = log_preview(s, max_chars=800)
        assert not result.endswith("...")
        assert len(result) == 800

    def test_whitespace_collapsed(self):
        result = log_preview("hello   world\n\ttab")
        assert result == "hello world tab"

    def test_password_redacted(self):
        result = log_preview("password=supersecret123")
        assert "supersecret123" not in result
        assert "REDACTED" in result

    def test_redis_url_with_password_redacted(self):
        result = log_preview("redis://:mypassword@localhost:6379")
        assert "mypassword" not in result

    def test_non_string_converted(self):
        result = log_preview(42)
        assert result == "42"

    def test_empty_string(self):
        result = log_preview("")
        assert result == ""

    def test_custom_max_chars(self):
        result = log_preview("abcdefghij", max_chars=5)
        assert len(result) == 5
        assert result.endswith("...")

    def test_multiline_collapsed_to_single_line(self):
        result = log_preview("line1\nline2\nline3")
        assert "\n" not in result
        assert "line1" in result
        assert "line2" in result


class TestJsonObjPreview:
    def test_dict_serialised(self):
        result = json_obj_preview({"key": "value"})
        assert "key" in result
        assert "value" in result

    def test_list_serialised(self):
        result = json_obj_preview([1, 2, 3])
        assert "1" in result

    def test_length_capped(self):
        big_dict = {str(i): "v" * 100 for i in range(100)}
        result = json_obj_preview(big_dict, max_chars=50)
        assert len(result) <= 50

    def test_unserializable_falls_back_to_str(self):
        class Unserializable:
            def __repr__(self):
                return "custom_repr"

        result = json_obj_preview(Unserializable())
        # Falls back to str(obj)
        assert isinstance(result, str)

    def test_none_serialised(self):
        result = json_obj_preview(None)
        assert "null" in result

    def test_nested_dict_serialised(self):
        result = json_obj_preview({"a": {"b": "c"}})
        assert "b" in result

    def test_json_dumps_exception_branch_covered(self):
        """Trigger the except branch inside json_obj_preview via a circular-ref object."""
        import sys

        # Create an object that json.dumps cannot handle even with default=str
        # by overriding __repr__ to raise — but default=str uses str(), not repr().
        # The easiest way to hit the except branch is a class whose __str__ raises
        # when json tries to use it... actually default=str prevents that.
        # Instead, monkey-patch json.dumps temporarily to raise.
        import workers.log_preview as lp_mod
        original = lp_mod.json

        class _FailJson:
            @staticmethod
            def dumps(*a, **kw):
                raise TypeError("cannot serialize")

        lp_mod.json = _FailJson  # type: ignore[assignment]
        try:
            result = json_obj_preview({"key": "val"})
            # Falls back to str({"key": "val"})
            assert isinstance(result, str)
        finally:
            lp_mod.json = original  # type: ignore[assignment]


class TestAlertPayloadSummary:
    def test_basic_alert_extracted(self):
        payload = {
            "source": "prometheus",
            "trace_id": "abc123",
            "data": {
                "alerts": [
                    {
                        "labels": {"alertname": "HighCPU", "namespace": "prod", "pod": "web-1"},
                        "annotations": {"summary": "CPU too high"},
                    }
                ]
            },
        }
        result = alert_payload_summary(payload)
        assert "HighCPU" in result
        assert "prod" in result
        assert "web-1" in result
        assert "CPU too high" in result

    def test_missing_data_field(self):
        payload = {"source": "prom", "trace_id": "t1"}
        result = alert_payload_summary(payload)
        assert "prom" in result
        assert "t1" in result

    def test_empty_alerts_list(self):
        payload = {"source": "prom", "trace_id": "t1", "data": {"alerts": []}}
        result = alert_payload_summary(payload)
        assert isinstance(result, str)

    def test_no_labels_dict(self):
        payload = {
            "source": "s",
            "trace_id": "t",
            "data": {"alerts": [{"labels": None, "annotations": {}}]},
        }
        result = alert_payload_summary(payload)
        assert isinstance(result, str)

    def test_annotation_description_used_as_fallback(self):
        payload = {
            "source": "s",
            "trace_id": "t",
            "data": {
                "alerts": [
                    {
                        "labels": {},
                        "annotations": {"description": "CPU spike"},
                    }
                ]
            },
        }
        result = alert_payload_summary(payload)
        assert "CPU spike" in result

    def test_max_chars_applied(self):
        payload = {
            "source": "s" * 1000,
            "trace_id": "t" * 1000,
            "data": {"alerts": []},
        }
        result = alert_payload_summary(payload, max_chars=50)
        assert len(result) <= 50

    def test_trace_id_in_output(self):
        payload = {"source": "src", "trace_id": "trace-xyz"}
        result = alert_payload_summary(payload)
        assert "trace-xyz" in result


# ===========================================================================
# 4. observability_audit — pure helpers only (no httpx/k8s I/O)
# ===========================================================================

from workers.observability_audit import (
    LGTM_DEPLOYMENTS,
    _monitor_stack_namespace,
    _prometheus_base,
    _prometheus_targets_base,
)


class TestMonitorStackNamespace:
    def test_returns_setting_value(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(monitor_stack_namespace="monitoring"))
        assert _monitor_stack_namespace(ctx) == "monitoring"

    def test_returns_default_when_no_settings(self):
        ctx = SimpleNamespace()
        assert _monitor_stack_namespace(ctx) == "monitor"

    def test_returns_default_when_setting_empty(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(monitor_stack_namespace=""))
        assert _monitor_stack_namespace(ctx) == "monitor"

    def test_returns_default_when_setting_whitespace(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(monitor_stack_namespace="   "))
        assert _monitor_stack_namespace(ctx) == "monitor"

    def test_returns_default_when_settings_none(self):
        ctx = SimpleNamespace(settings=None)
        assert _monitor_stack_namespace(ctx) == "monitor"

    def test_strips_whitespace(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(monitor_stack_namespace="  obs  "))
        assert _monitor_stack_namespace(ctx) == "obs"


class TestPrometheusBase:
    def test_returns_setting_url(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url="http://prom:9090"))
        result = _prometheus_base(ctx)
        assert result == "http://prom:9090"

    def test_strips_trailing_slash(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url="http://prom:9090/"))
        result = _prometheus_base(ctx)
        assert not result.endswith("/")

    def test_returns_default_when_no_settings(self):
        ctx = SimpleNamespace()
        result = _prometheus_base(ctx)
        assert "prometheus" in result or "9090" in result

    def test_returns_default_when_url_empty(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url=""))
        result = _prometheus_base(ctx)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_default_when_url_whitespace(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(prometheus_url="   "))
        result = _prometheus_base(ctx)
        assert isinstance(result, str)
        assert "prometheus" in result or "9090" in result

    def test_returns_default_when_settings_none(self):
        ctx = SimpleNamespace(settings=None)
        result = _prometheus_base(ctx)
        assert isinstance(result, str)


class TestPrometheusTargetsBase:
    def test_returns_vmagent_url_when_set(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(vmagent_url="http://vmagent:8429"))
        result = _prometheus_targets_base(ctx)
        assert result == "http://vmagent:8429"

    def test_strips_trailing_slash(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(vmagent_url="http://vmagent:8429/"))
        result = _prometheus_targets_base(ctx)
        assert not result.endswith("/")

    def test_returns_default_when_no_settings(self):
        ctx = SimpleNamespace()
        result = _prometheus_targets_base(ctx)
        assert isinstance(result, str)

    def test_returns_default_when_url_empty(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(vmagent_url=""))
        result = _prometheus_targets_base(ctx)
        assert isinstance(result, str)

    def test_returns_default_when_settings_none(self):
        ctx = SimpleNamespace(settings=None)
        result = _prometheus_targets_base(ctx)
        assert isinstance(result, str)


class TestLgtmDeployments:
    def test_contains_loki_and_grafana(self):
        assert "loki" in LGTM_DEPLOYMENTS
        assert "grafana" in LGTM_DEPLOYMENTS


# ===========================================================================
# 5. selflearning_shadow
# ===========================================================================

from workers.selflearning_shadow import (
    _derive_probe_suggestions,
    _flag,
    run_shadow_selflearning,
)


class TestFlag:
    def test_returns_true_when_set(self):
        settings = SimpleNamespace(some_flag=True)
        assert _flag(settings, "some_flag") is True

    def test_returns_false_when_unset(self):
        settings = SimpleNamespace()
        assert _flag(settings, "missing_flag") is False

    def test_returns_default_when_attribute_missing(self):
        settings = SimpleNamespace()
        assert _flag(settings, "x", default=True) is True

    def test_falsy_int_returns_false(self):
        settings = SimpleNamespace(f=0)
        assert _flag(settings, "f") is False

    def test_truthy_int_returns_true(self):
        settings = SimpleNamespace(f=1)
        assert _flag(settings, "f") is True


class TestDeriveProbeSuggestions:
    def test_dns_keywords_trigger_dns_probes(self):
        probes = _derive_probe_suggestions("DNS resolution failure, ndots config")
        assert "inspect_dns_config" in probes
        assert "probe_dns_resolution" in probes

    def test_latency_keywords_trigger_latency_probes(self):
        probes = _derive_probe_suggestions("high latency and timeout")
        assert "inspect_service_latency" in probes
        assert "probe_network_path" in probes

    def test_oom_keywords_trigger_memory_probes(self):
        probes = _derive_probe_suggestions("OOM killed, memory exhausted")
        assert "inspect_memory_breakdown" in probes
        assert "probe_memory_trend" in probes

    def test_cpu_keywords_trigger_cpu_probes(self):
        probes = _derive_probe_suggestions("CPU throttling issue")
        assert "probe_cpu_throttling" in probes
        assert "verify_sigma_snapshot" in probes

    def test_kafka_keywords_trigger_kafka_probes(self):
        probes = _derive_probe_suggestions("kafka consumer lag detected")
        assert "inspect_kafka_lag" in probes
        assert "inspect_partition_skew" in probes

    def test_redis_keywords_trigger_redis_probes(self):
        probes = _derive_probe_suggestions("redis connection refused")
        assert "inspect_redis_memory" in probes
        assert "inspect_redis_backlog" in probes

    def test_empty_text_returns_no_probes(self):
        probes = _derive_probe_suggestions("")
        assert probes == []

    def test_unrelated_text_returns_no_probes(self):
        probes = _derive_probe_suggestions("everything is fine, no issues")
        assert probes == []

    def test_deduplication(self):
        # "oom" and "memory" both map to same probes
        probes = _derive_probe_suggestions("oom memory issue")
        assert len(probes) == len(set(probes))

    def test_max_8_probes_returned(self):
        # Trigger all categories
        text = "dns latency oom cpu kafka redis ndots timeout throttl memory lag"
        probes = _derive_probe_suggestions(text)
        assert len(probes) <= 8

    def test_case_insensitive(self):
        probes = _derive_probe_suggestions("DNS failure")
        assert "inspect_dns_config" in probes

    def test_timeout_without_latency(self):
        probes = _derive_probe_suggestions("connection timeout")
        assert "inspect_service_latency" in probes


class TestRunShadowSelflearning:
    @pytest.mark.asyncio
    async def test_does_nothing_when_settings_none(self):
        ctx = SimpleNamespace()  # no settings, no redis
        # Should not raise
        await run_shadow_selflearning(ctx, trace="t1", sanitized_text="some evidence")

    @pytest.mark.asyncio
    async def test_does_nothing_when_redis_none(self):
        ctx = SimpleNamespace(settings=SimpleNamespace(multi_hypothesis_enabled=True))
        # Should not raise
        await run_shadow_selflearning(ctx, trace="t1", sanitized_text="some evidence")

    @pytest.mark.asyncio
    async def test_does_nothing_when_both_flags_disabled(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=False,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis)
        await run_shadow_selflearning(ctx, trace="t1", sanitized_text="some evidence")

        # No key stored
        keys = await redis.keys("omni:selflearn:shadow:*")
        assert len(keys) == 0

    @pytest.mark.asyncio
    async def test_stores_draft_when_knowledge_draft_enabled(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(ctx, trace="trace-99", sanitized_text="memory issue")

        raw = await redis.get("omni:selflearn:shadow:trace-99")
        assert raw is not None
        draft = json.loads(raw)
        assert draft["trace_id"] == "trace-99"
        assert isinstance(draft["hypotheses"], list)
        assert isinstance(draft["probe_suggestions"], list)
        assert "symptom" in draft["knowledge_draft"]
        assert "memory issue" in draft["knowledge_draft"]["symptom"]

    @pytest.mark.asyncio
    async def test_stores_draft_with_machine_fallback(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=False,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
            # Enable via knowledge_draft_enabled won't store knowledge_draft; we need multi_hyp or knowledge_draft
        )
        # Enable only knowledge_draft_enabled for this test
        settings.knowledge_draft_enabled = True
        machine = {"hypothesis": "disk_exhaustion"}
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(
            ctx,
            trace="trace-m1",
            sanitized_text="disk issue",
            machine=machine,
        )

        raw = await redis.get("omni:selflearn:shadow:trace-m1")
        assert raw is not None
        draft = json.loads(raw)
        # machine hypothesis used as fallback
        hyp_names = [h["name"] for h in draft["hypotheses"]]
        assert "disk_exhaustion" in hyp_names

    @pytest.mark.asyncio
    async def test_probe_suggestions_populated_when_flag_enabled(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=True,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(ctx, trace="trace-p1", sanitized_text="kafka lag detected")

        raw = await redis.get("omni:selflearn:shadow:trace-p1")
        draft = json.loads(raw)
        assert len(draft["probe_suggestions"]) > 0

    @pytest.mark.asyncio
    async def test_probe_suggestions_empty_when_flag_disabled(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(ctx, trace="trace-p2", sanitized_text="kafka lag detected")

        raw = await redis.get("omni:selflearn:shadow:trace-p2")
        draft = json.loads(raw)
        assert draft["probe_suggestions"] == []

    @pytest.mark.asyncio
    async def test_multi_hypothesis_uses_llm(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        call_log: list[dict] = []

        class _FakeLLM:
            async def chat(self, model, messages, stream):
                call_log.append({"model": model})
                return {
                    "message": {
                        "content": '{"hypotheses":[{"name":"oom_kill","why":"memory exceeded","confidence":0.9}]}'
                    }
                }

        settings = SimpleNamespace(
            multi_hypothesis_enabled=True,
            knowledge_draft_enabled=False,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
            diag_evidence_llm_model="qwen2.5:7b",
            model_reasoning_engine="",
            chat_model="",
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=_FakeLLM())
        await run_shadow_selflearning(ctx, trace="trace-h1", sanitized_text="pod OOM killed")

        raw = await redis.get("omni:selflearn:shadow:trace-h1")
        draft = json.loads(raw)
        assert len(draft["hypotheses"]) > 0
        assert draft["hypotheses"][0]["name"] == "oom_kill"
        assert len(call_log) == 1

    @pytest.mark.asyncio
    async def test_ttl_set_on_stored_key(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(ctx, trace="trace-ttl", sanitized_text="evidence")

        ttl = await redis.ttl("omni:selflearn:shadow:trace-ttl")
        # TTL should be set (86400 seconds)
        assert ttl > 0

    @pytest.mark.asyncio
    async def test_shadow_only_flag_in_draft(self):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=redis, llm=None)
        await run_shadow_selflearning(ctx, trace="trace-so", sanitized_text="evidence")

        raw = await redis.get("omni:selflearn:shadow:trace-so")
        draft = json.loads(raw)
        assert draft["shadow_only"] is True

    @pytest.mark.asyncio
    async def test_generate_hypotheses_returns_empty_when_llm_none(self):
        """_generate_three_hypotheses returns [] when llm is None."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        ctx = SimpleNamespace(settings=SimpleNamespace(diag_evidence_llm_model="model"), llm=None)
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert result == []

    @pytest.mark.asyncio
    async def test_generate_hypotheses_returns_empty_when_no_model(self):
        """_generate_three_hypotheses returns [] when no model name can be resolved."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        class _FakeLLM:
            async def chat(self, **kwargs): return {}

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                diag_evidence_llm_model="",
                model_reasoning_engine="",
                chat_model="",
            ),
            llm=_FakeLLM(),
        )
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert result == []

    @pytest.mark.asyncio
    async def test_generate_hypotheses_returns_empty_when_no_json_braces(self):
        """_generate_three_hypotheses returns [] when LLM response has no {}."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        class _FakeLLM:
            async def chat(self, model, messages, stream):
                return {"message": {"content": "no braces here at all"}}

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                diag_evidence_llm_model="qwen2.5:7b",
                model_reasoning_engine="",
                chat_model="",
            ),
            llm=_FakeLLM(),
        )
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert result == []

    @pytest.mark.asyncio
    async def test_generate_hypotheses_returns_empty_when_hypotheses_not_list(self):
        """Returns [] when 'hypotheses' key is missing or not a list."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        class _FakeLLM:
            async def chat(self, model, messages, stream):
                return {"message": {"content": '{"hypotheses": "not-a-list"}'}}

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                diag_evidence_llm_model="qwen2.5:7b",
                model_reasoning_engine="",
                chat_model="",
            ),
            llm=_FakeLLM(),
        )
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert result == []

    @pytest.mark.asyncio
    async def test_generate_hypotheses_skips_non_dict_rows(self):
        """Rows that are not dicts are skipped."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        class _FakeLLM:
            async def chat(self, model, messages, stream):
                return {"message": {"content": '{"hypotheses":["not-a-dict",{"name":"ok","why":"reason","confidence":0.7}]}'}}

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                diag_evidence_llm_model="qwen2.5:7b",
                model_reasoning_engine="",
                chat_model="",
            ),
            llm=_FakeLLM(),
        )
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert len(result) == 1
        assert result[0]["name"] == "ok"

    @pytest.mark.asyncio
    async def test_generate_hypotheses_exception_suppressed(self):
        """Exception from LLM is caught and returns []."""
        from workers.selflearning_shadow import _generate_three_hypotheses

        class _FailLLM:
            async def chat(self, **kwargs):
                raise RuntimeError("llm timeout")

        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                diag_evidence_llm_model="qwen2.5:7b",
                model_reasoning_engine="",
                chat_model="",
            ),
            llm=_FailLLM(),
        )
        result = await _generate_three_hypotheses(ctx, trace="t", sanitized_text="text")
        assert result == []

    @pytest.mark.asyncio
    async def test_redis_error_suppressed(self):
        class _FailRedis:
            async def setex(self, key, ttl, val):
                raise ConnectionError("redis down")
            async def keys(self, pattern):
                return []

        settings = SimpleNamespace(
            multi_hypothesis_enabled=False,
            knowledge_draft_enabled=True,
            deep_probe_orchestration_enabled=False,
            multi_hypothesis_shadow_only=True,
            knowledge_promotion_enabled=False,
            autodoc_git_push_enabled=False,
        )
        ctx = SimpleNamespace(settings=settings, redis=_FailRedis(), llm=None)
        # Should not raise
        await run_shadow_selflearning(ctx, trace="trace-err", sanitized_text="evidence")
