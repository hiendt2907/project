"""LLM observability: redaction (INV_DATA_RESIDENCY), structured log, fail-soft."""

from __future__ import annotations

import logging

import pytest

from pkg.observability import llm_observability as obs


# ---------------------------------------------------------------- redaction --


def test_preview_disabled_by_default_so_no_customer_text_is_logged(monkeypatch):
    monkeypatch.delenv(obs._ENV_PREVIEW_CHARS, raising=False)

    assert obs.preview_chars() == 0
    assert obs.redact_for_log("customer hostname cust-db mysql error") == ""


def test_preview_budget_is_hard_capped_regardless_of_env(monkeypatch):
    monkeypatch.setenv(obs._ENV_PREVIEW_CHARS, "999999")

    assert obs.preview_chars() == obs.MAX_PREVIEW_CHARS
    # Realistic prose: long unbroken runs are separately (and deliberately)
    # masked by the base64-blob rule, which would hide the truncation itself.
    out = obs.redact_for_log("disk usage is high on host cust-db " * 500)
    # +1 for the ellipsis marker appended on truncation.
    assert len(out) == obs.MAX_PREVIEW_CHARS + 1


def test_non_integer_preview_env_disables_preview_instead_of_raising(monkeypatch):
    monkeypatch.setenv(obs._ENV_PREVIEW_CHARS, "not-a-number")

    assert obs.preview_chars() == 0


def test_negative_preview_env_clamps_to_zero(monkeypatch):
    monkeypatch.setenv(obs._ENV_PREVIEW_CHARS, "-5")

    assert obs.preview_chars() == 0


@pytest.mark.parametrize(
    "secret",
    [
        "api_key: sk-live-abcdefghijklmnop",
        "password=hunter2supersecret",
        "Authorization: Bearer abcdefghijklmnopqrst",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_secret_shaped_tokens_are_masked_in_preview(monkeypatch, secret):
    monkeypatch.setenv(obs._ENV_PREVIEW_CHARS, "512")

    out = obs.redact_for_log(f"prefix {secret} suffix")

    assert "[REDACTED]" in out
    assert "hunter2supersecret" not in out
    assert "sk-live-abcdefghijklmnop" not in out


def test_digest_is_stable_short_and_not_the_content():
    text = "sensitive customer prompt"

    d = obs.digest(text)

    assert d == obs.digest(text)
    assert len(d) == 12
    assert text not in d
    assert obs.digest("") == ""
    assert obs.digest(None) == ""


def test_digest_differs_for_different_prompts():
    assert obs.digest("prompt-a") != obs.digest("prompt-b")


# ------------------------------------------------------------ messages_text --


def test_messages_text_flattens_role_contents_in_order():
    msgs = [
        {"role": "system", "content": "you are an SRE"},
        {"role": "user", "content": "disk is full"},
    ]

    assert obs.messages_text(msgs) == "you are an SRE\ndisk is full"


def test_messages_text_tolerates_empty_and_malformed_entries():
    assert obs.messages_text(None) == ""
    assert obs.messages_text([]) == ""
    assert obs.messages_text(["not-a-dict", {"content": None}]) == ""


# --------------------------------------------------------- record_llm_call --


def test_record_llm_call_logs_sizes_and_digests_but_not_prompt_text(monkeypatch, caplog):
    monkeypatch.delenv(obs._ENV_PREVIEW_CHARS, raising=False)
    secret_prompt = "customer db password is hunter2 on host cust-db"

    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_llm_call(
            model="qwen2.5-coder:7b",
            call_kind="structured",
            prompt=secret_prompt,
            response="root cause: disk full",
            duration_ms=1234.5,
            prompt_tokens=800,
            completion_tokens=42,
            trace_id="trace-abc",
        )

    line = caplog.text
    assert "event=llm_call" in line
    assert "trace=trace-abc" in line
    assert "model=qwen2.5-coder:7b" in line
    assert "call_kind=structured" in line
    assert "outcome=ok" in line
    assert "duration_ms=1234.5" in line
    assert f"prompt_chars={len(secret_prompt)}" in line
    assert "prompt_tokens=800" in line
    assert "completion_tokens=42" in line
    assert f"prompt_sha={obs.digest(secret_prompt)}" in line
    # INV_DATA_RESIDENCY: no verbatim prompt/response content.
    assert "hunter2" not in line
    assert "cust-db" not in line
    assert "prompt_preview" not in line


def test_record_llm_call_includes_preview_when_explicitly_enabled(monkeypatch, caplog):
    monkeypatch.setenv(obs._ENV_PREVIEW_CHARS, "200")

    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_llm_call(
            model="m",
            call_kind="chat",
            prompt="why is nginx down",
            response="it is not running",
            trace_id="t1",
        )

    assert "prompt_preview=" in caplog.text
    assert "why is nginx down" in caplog.text
    assert "response_preview=" in caplog.text


def test_record_llm_call_error_outcome_logs_at_warning(monkeypatch, caplog):
    monkeypatch.delenv(obs._ENV_PREVIEW_CHARS, raising=False)

    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_llm_call(
            model="m",
            call_kind="chat",
            prompt="p",
            outcome="error",
            error="ConnectError: refused",
            trace_id="t2",
        )

    assert "outcome=error" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_record_llm_call_is_a_noop_when_disabled(monkeypatch, caplog):
    monkeypatch.setenv(obs._ENV_ENABLED, "false")

    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_llm_call(model="m", call_kind="chat", prompt="p", trace_id="t")

    assert "event=llm_call" not in caplog.text


def test_record_llm_call_never_raises_when_a_sink_explodes(monkeypatch, caplog):
    def _boom(**_kw):
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(obs, "_emit_metrics", _boom)

    with caplog.at_level(logging.WARNING, logger=obs.__name__):
        obs.record_llm_call(model="m", call_kind="chat", prompt="p", trace_id="t")

    # Fail-soft, but audibly: the failure is logged, not swallowed.
    assert "record_llm_call failed" in caplog.text


def test_record_llm_call_falls_back_to_dash_when_no_trace_bound(monkeypatch, caplog):
    monkeypatch.setattr(obs, "current_trace_id", lambda: "")

    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_llm_call(model="m", call_kind="chat", prompt="p")

    assert "trace=-" in caplog.text


# ---------------------------------------------------------- record_rag_gate --


def test_record_rag_gate_logs_outcome_and_collection(caplog):
    with caplog.at_level(logging.INFO, logger=obs.__name__):
        obs.record_rag_gate("cache_hit", collection="k8s_expert", trace_id="t3")

    assert "event=rag_gate" in caplog.text
    assert "outcome=cache_hit" in caplog.text
    assert "collection=k8s_expert" in caplog.text


def test_record_rag_gate_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(obs, "current_trace_id", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    with caplog.at_level(logging.WARNING, logger=obs.__name__):
        obs.record_rag_gate("miss")

    assert "record_rag_gate failed" in caplog.text


# ----------------------------------------------------------- trace ctx read --


def test_current_trace_id_reads_the_worker_context_var():
    from workers.request_trace import pop_trace_id, push_trace_id

    tok = push_trace_id("ctx-trace-9")
    try:
        assert obs.current_trace_id() == "ctx-trace-9"
    finally:
        pop_trace_id(tok)

    # Unset ContextVar reports "" (not the literal "unknown").
    assert obs.current_trace_id() == ""


# --------------------------------------------------------------- otel span --


def test_emit_span_is_a_noop_without_a_trace_id():
    # Must not raise and must not attempt any OTEL work.
    obs._emit_span(
        trace_id="",
        model="m",
        call_kind="chat",
        outcome="ok",
        duration_ms=1.0,
        prompt_chars=1,
        response_chars=1,
        prompt_tokens=0,
        completion_tokens=0,
        error="",
    )


def test_trace_id_int_matches_otel_stage_span_scheme():
    """LLM spans must land in the same Tempo trace as pipeline stage spans."""
    from pkg.observability.otel_stage_span import _trace_id_int_from_string

    for tid in ("6f1e6b3c9a7d4e2b8c5f0a1d2e3b4c5d", "not-a-uuid", "trace-123"):
        assert obs._trace_id_int(tid) == _trace_id_int_from_string(tid)
