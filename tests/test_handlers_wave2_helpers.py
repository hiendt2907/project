from __future__ import annotations

from types import SimpleNamespace

import pytest


def _ctx(**settings):
    defaults = {
        "god_mode": False,
        "lab_unchained": False,
        "omni_concise_reply_max_words": 5,
        "omni_summary_max_words": 5,
    }
    defaults.update(settings)
    return SimpleNamespace(settings=SimpleNamespace(**defaults))


def test_parse_alert_pod_namespace_from_preview_prefers_alert_line():
    from workers.handlers import _parse_alert_pod_namespace_from_preview

    text = "\n".join(
        [
            "noise pod=ignored namespace=ignored",
            "Alert: KubePodCrashLooping pod=api-7d9f namespace=prod | pod=api-7d9f namespace=prod",
        ]
    )

    assert _parse_alert_pod_namespace_from_preview(text) == ("api-7d9f", "prod")


def test_parse_alert_pod_namespace_from_preview_falls_back_to_anywhere():
    from workers.handlers import _parse_alert_pod_namespace_from_preview

    assert _parse_alert_pod_namespace_from_preview("pod=redis-0 namespace=cache") == (
        "redis-0",
        "cache",
    )
    assert _parse_alert_pod_namespace_from_preview("Alert: Missing namespace pod=redis-0") == (
        None,
        None,
    )
    assert _parse_alert_pod_namespace_from_preview("") == (None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("show cpu for 15m", "15m"),
        ("forecast RAM 2 h", "2h"),
        ("no duration here", "1h"),
        ("ignore unsupported 7d", "1h"),
    ],
)
def test_extract_duration(text, expected):
    from workers.handlers import _extract_duration

    assert _extract_duration(text) == expected


def test_preflight_hints_from_inbound_uses_payload_and_alert_identity():
    from workers.handlers import _preflight_hints_from_inbound

    payload = {"namespace": "payload-ns"}
    raw = "Alert: CPUHigh pod=api-5d6 namespace=alert-ns"

    assert _preflight_hints_from_inbound(payload, raw, "telegram") == {
        "namespace": "alert-ns",
        "pod_name": "api-5d6",
    }
    assert _preflight_hints_from_inbound({"namespace": "payload-ns"}, "hello", "api") == {
        "namespace": "payload-ns"
    }
    assert _preflight_hints_from_inbound({}, "hello", "api") is None


def test_parse_suggestions_json_tail_valid_and_invalid_cases():
    from workers.handlers import _parse_suggestions_json_tail

    display, commands = _parse_suggestions_json_tail(
        'Diagnosis ready\nSUGGESTIONS_JSON: ["kubectl get pods", "check logs", "restart pod"]'
    )
    assert display == "Diagnosis ready"
    assert commands == ["kubectl get pods", "check logs", "restart pod"]

    too_few = 'Body\nSUGGESTIONS_JSON: ["one", "two"]'
    assert _parse_suggestions_json_tail(too_few) == (too_few.strip(), None)

    malformed = 'Body\nSUGGESTIONS_JSON: ["one",'
    assert _parse_suggestions_json_tail(malformed) == (malformed.strip(), None)

    long_command = "x" * 700
    _, clipped = _parse_suggestions_json_tail(
        f'SUGGESTIONS_JSON: ["{long_command}", "b", "c"]'
    )
    assert clipped is not None
    assert len(clipped[0]) == 500


def test_parse_tool_json_accepts_plain_fenced_and_params_alias():
    from workers.handlers import _parse_tool_json

    plain = _parse_tool_json('{"tool": "reply", "args": {"text": "ok"}}')
    assert plain.tool == "reply"
    assert plain.args == {"text": "ok"}

    fenced = _parse_tool_json('```json\n{"tool": "echo", "args": {"msg": "hi"}}\n```')
    assert fenced.tool == "echo"
    assert fenced.args == {"msg": "hi"}

    params_alias = _parse_tool_json('{"tool": "echo", "params": {"msg": "from params"}}')
    assert params_alias.args == {"msg": "from params"}


def test_parse_tool_json_rejects_invalid_json():
    from workers.handlers import _parse_tool_json

    with pytest.raises(Exception):
        _parse_tool_json("not json")


def test_should_abort_stale_counts_only_tail_streak():
    from workers.handlers import _should_abort_stale
    from workers.slow_path_trace import AttemptRecord

    trace = [
        AttemptRecord(1, "parse", "parse_json", "bad json"),
        AttemptRecord(2, "tool_error", "tool_error:missing_pod", "missing pod"),
        AttemptRecord(3, "tool_error", "tool_error:missing_pod", "missing pod again"),
    ]

    assert _should_abort_stale(trace, 2) is True
    assert _should_abort_stale(trace, 3) is False
    assert _should_abort_stale([], 1) is False


def test_k8s_smart_target_hint_scoped_generic_and_non_k8s():
    from workers.handlers import _k8s_smart_target_hint

    assert _k8s_smart_target_hint("") is None
    assert _k8s_smart_target_hint("hello world") is None

    scoped = _k8s_smart_target_hint("Alert: CPUHigh pod=api-0 namespace=prod")
    assert scoped is not None
    assert "pod=api-0 namespace=prod" in scoped
    assert "do not" in scoped.lower()

    generic = _k8s_smart_target_hint("show cpu for pods in namespace prod")
    assert generic is not None
    assert "Routing K8s" in generic
    assert "Do not guess namespace" in generic


def test_embedding_from_response_supports_single_and_batch_shapes():
    from workers.handlers import _embedding_from_response

    assert _embedding_from_response({"embedding": (1.0, 2.0)}) == [1.0, 2.0]
    existing = [0.1, 0.2]
    assert _embedding_from_response({"embedding": existing}) is existing
    assert _embedding_from_response({"embeddings": [[3, 4], [5, 6]]}) == [3, 4]

    with pytest.raises(ValueError, match="missing embedding"):
        _embedding_from_response({})


def test_cap_inbound_user_reply_truncates_prose_but_not_valid_json_object():
    from workers.handlers import _cap_inbound_user_reply

    assert _cap_inbound_user_reply("one two three four five six", _ctx()) == "one two three four five"
    assert _cap_inbound_user_reply('{"tool": "reply", "args": {"text": "keep all words"}}', _ctx()) == (
        '{"tool": "reply", "args": {"text": "keep all words"}}'
    )
    assert _cap_inbound_user_reply(None, _ctx()) == ""


def test_effective_inbound_text_preview_prefers_raw_text_and_formats_alertmanager():
    from workers.handlers import _effective_inbound_text_preview

    assert _effective_inbound_text_preview({"text": " raw text "}) == "raw text"
    assert _effective_inbound_text_preview({"message": " message text "}) == "message text"
    assert _effective_inbound_text_preview({"data": {"text": " nested text "}}) == "nested text"

    preview = _effective_inbound_text_preview(
        {
            "data": {
                "alerts": [
                    {
                        "labels": {
                            "alertname": "CPUHigh",
                            "pod": "api-0",
                            "namespace": "prod",
                            "deployment": "api",
                            "instance": "unknown",
                        },
                        "annotations": {"summary": "CPU hot"},
                    }
                ]
            }
        }
    )
    assert "Alert: CPUHigh pod=api-0 namespace=prod deployment=api - CPU hot" in preview
    assert "pod=api-0" in preview
    assert "namespace=prod" in preview


def test_effective_inbound_text_preview_handles_bad_alert_payload():
    from workers.handlers import _effective_inbound_text_preview

    preview = _effective_inbound_text_preview({"payload": {"alerts": [None]}})
    assert "Alert: UnknownAlert" in preview
    assert "identifiers=unspecified" in preview
    assert _effective_inbound_text_preview({}) == ""


def test_build_agentic_system_messages_catalog_and_scoped_alert_hint(monkeypatch):
    from workers import handlers

    monkeypatch.setattr(handlers, "shell_fast_path_enabled", lambda _settings: False)

    interactive = handlers.build_agentic_system_messages(_ctx(), unattended_alert=False)
    assert len(interactive) == 2
    assert interactive[0]["role"] == "system"
    assert "`reply`" in interactive[1]["content"]

    scoped_ctx = _ctx()
    scoped_ctx.inbound_user_text = "Alert: CPUHigh pod=api-0 namespace=prod"
    unattended = handlers.build_agentic_system_messages(scoped_ctx, unattended_alert=True)
    assert len(unattended) == 3
    catalog = unattended[1]["content"].split("Tools (from TOOL_REGISTRY): ", 1)[1].split(". ", 1)[0]
    assert "`reply`" not in catalog
    assert "`escalate_to_human`" in catalog
    assert "pod=api-0 namespace=prod" in unattended[2]["content"]

