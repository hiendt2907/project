"""Effective inbound text preview (Prometheus alerts) + log-length behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from workers.handlers import (
    _effective_inbound_text_preview,
    _k8s_smart_target_hint,
    _parse_alert_pod_namespace_from_preview,
    _parse_tool_json,
    build_agentic_system_messages,
)
from workers.settings import WorkerSettings


def test_effective_preview_from_alerts() -> None:
    payload = {
        "source": "prometheus",
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "HighCPU", "instance": "10.0.0.1:9100"},
                    "annotations": {"summary": "CPU > 90%"},
                }
            ]
        },
    }
    t = _effective_inbound_text_preview(payload)
    assert "HighCPU" in t
    assert "10.0.0.1:9100" in t
    assert "CPU > 90%" in t
    assert len(t) > 0


def test_effective_preview_no_literal_on_unknown_when_instance_missing() -> None:
    """Không tạo chuỗi 'on unknown' — trước đây default instance khiến LLM bắt nhầm pod tên."""
    payload = {
        "source": "prometheus",
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "ProbeTest"},
                    "annotations": {"summary": "no labels"},
                }
            ]
        },
    }
    t = _effective_inbound_text_preview(payload)
    assert "on unknown" not in t.lower()
    assert "ProbeTest" in t


def test_effective_preview_includes_ollama_anchor_en() -> None:
    payload = {
        "source": "prometheus",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "HighCPU",
                        "namespace": "multi-agent",
                        "pod": "app-1",
                        "deployment": "app",
                    },
                    "annotations": {"summary": "cpu high"},
                }
            ]
        },
    }
    t = _effective_inbound_text_preview(payload)
    assert "[OLLAMA_ANCHOR_EN]" in t
    assert "FACTS:" in t
    assert "TRIGGER:" in t
    assert "HINT:" in t


def test_effective_preview_pod_first_line_when_pod_label_set() -> None:
    payload = {
        "source": "prometheus",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "NginxTestProbe",
                        "namespace": "multi-agent",
                        "pod": "nginx-test-xyz123",
                        "deployment": "nginx-test",
                    },
                    "annotations": {"summary": "pod not ready"},
                }
            ]
        },
    }
    t = _effective_inbound_text_preview(payload)
    assert "pod=nginx-test-xyz123" in t
    assert "namespace=multi-agent" in t
    assert "NginxTestProbe" in t
    assert "on unknown" not in t.lower()


def test_effective_preview_includes_namespace_and_pod_labels() -> None:
    payload = {
        "source": "prometheus",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": "OmniRuntimeE2EDebug",
                        "namespace": "multi-agent",
                        "pod": "web-abc",
                    },
                    "annotations": {"summary": "debug"},
                }
            ]
        },
    }
    t = _effective_inbound_text_preview(payload)
    assert "namespace=multi-agent" in t
    assert "pod=web-abc" in t
    assert "OmniRuntimeE2EDebug" in t


def test_parse_tool_json_maps_params_to_args() -> None:
    raw = '{"tool":"resolve_pod_identity","params":{"pod_name":"x","namespace":"ns1"}}'
    call = _parse_tool_json(raw)
    assert call.tool == "resolve_pod_identity"
    assert call.args.get("pod_name") == "x"
    assert call.args.get("namespace") == "ns1"


def test_build_agentic_unattended_omits_reply_tool_listing() -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()

    msgs = build_agentic_system_messages(ctx, unattended_alert=True)
    blob = "\n".join(str(m.get("content", "")) for m in msgs)
    assert "escalate_to_human" in blob
    assert "postgres_ping`, `reply`" not in blob
    assert "escalate_to_human" in msgs[0]["content"] or "UNATTENDED" in msgs[0]["content"].upper()


def test_build_agentic_unattended_god_sdk_first_no_god_fewshot() -> None:
    """God/lab unattended must not inject SLOW_SYSTEM_GOD few-shot (shell-first for kubectl top)."""
    ctx = MagicMock()
    ctx.settings = WorkerSettings(god_mode=True)
    msgs = build_agentic_system_messages(ctx, unattended_alert=True)
    blob = "\n".join(str(m.get("content", "")) for m in msgs)
    assert "LAB_SHELL" in blob or "last resort" in blob.lower()
    assert "kubectl top pods -A" not in blob
    assert "SDK-only" in blob or "No** raw shell" in blob


def test_parse_alert_pod_namespace_from_preview() -> None:
    t = "Alert: NginxTestProbe pod=p-1 namespace=multi-agent deployment=nginx-test - probe failed"
    pod, ns = _parse_alert_pod_namespace_from_preview(t)
    assert pod == "p-1"
    assert ns == "multi-agent"


def test_k8s_smart_target_hint_prefers_inspect_when_pod_and_namespace_in_alert() -> None:
    text = (
        "[CONTEXT: x]\n\n[USER_MESSAGE]\n"
        "Alert: NginxTestProbe pod=nginx-test-abc namespace=multi-agent - readiness"
    )
    h = _k8s_smart_target_hint(text)
    assert h is not None
    assert "inspect_pod_deep" in h or "scoped" in h
    assert "k8s_list_pods" in h


def test_build_agentic_unattended_adds_identity_system_when_pod_ns_in_inbound() -> None:
    ctx = MagicMock()
    ctx.settings = WorkerSettings()
    ctx.inbound_user_text = (
        "Alert: NginxTestProbe pod=nginx-test-xyz namespace=multi-agent deployment=nginx-test - x"
    )
    msgs = build_agentic_system_messages(ctx, unattended_alert=True)
    assert len(msgs) >= 3
    last = msgs[-1]["content"]
    assert "PRIORITY" in last or "identified Prometheus" in last or "inspect_pod_deep" in last
    assert "nginx-test-xyz" in last
