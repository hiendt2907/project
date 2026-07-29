"""Coverage for prober, init, execution — real logic and local HTTP; no unittest.mock."""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from execution.experience import (
    SandboxLessonInput,
    fetch_action_experience_context,
    record_sandbox_lesson,
    routing_experience_point_id,
    synthesize_lesson_text,
    truncate_lesson_to_budget,
    upsert_action_experience,
)
from execution.manager import SandboxExecResult, SandboxManager, sandbox_result_to_user_text
from execution.memory_normalize import (
    canonical_symptom_text,
    extract_workload_fingerprint,
    stable_playbook_pattern_key,
    strip_ephemeral_from_args,
)
from execution.policy import (
    PolicyVerdict,
    check_promotion_tool,
    check_sandbox_command,
    normalize_command,
)
from execution import promotion as promotion_mod
from execution.pod_env_clone import _safe_env_from_container
from init import deep_scout as ds
from init import deep_scout_autonomous as dsa
from prober.temporal_evidence import TemporalEvidenceBlock, TemporalMetric
from rag.redis_vector_store import EMBED_DIM
from workers.routing_policy import ROUTING_SOURCE_SLOW_PATH
from workers.settings import WorkerSettings


# --- Local HTTP server (asyncio) for Prometheus-style JSON ---


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()
    return int(port)


async def _http_server_once(
    port: int,
    *,
    body: bytes,
    status_line: bytes = b"HTTP/1.1 200 OK\r\n",
) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        hdr = status_line + b"Content-Type: application/json\r\n"
        hdr += f"Content-Length: {len(body)}\r\n\r\n".encode()
        writer.write(hdr + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_server(handler, "127.0.0.1", port)


def _instant_body_for_promql(promql: str) -> dict[str, Any]:
    q = promql or ""
    if "count(container_cpu_usage_seconds_total)" in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1_700_000_000, "42"]}]}}
    if "container_cpu_usage_seconds_total" in q and "topk" in q:
        return {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"namespace": "demo-ns"}, "value": [1_700_000_000, "0.12"]},
                ]
            },
        }
    if "container_memory_working_set_bytes" in q:
        return {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"namespace": "demo-ns"}, "value": [1_700_000_000, "4096"]},
                ]
            },
        }
    if q.strip() == "count(up)":
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1_700_000_000, "3"]}]}}
    if "node_cpu_seconds_total" in q and "idle" in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1_700_000_000, "0.77"]}]}}
    return {"status": "success", "data": {"result": []}}


async def _prometheus_instant_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    raw = await reader.read(65536)
    try:
        first = raw.decode("utf-8", errors="replace").split("\r\n", 1)[0]
        path = first.split()[1]
    except (IndexError, ValueError):
        path = ""
    parsed = urlparse("http://local" + path)
    promql = parse_qs(parsed.query).get("query", [""])[0]
    body = json.dumps(_instant_body_for_promql(promql)).encode()
    hdr = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    hdr += f"Content-Length: {len(body)}\r\n\r\n".encode()
    writer.write(hdr + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _prometheus_instant_server(port: int) -> asyncio.AbstractServer:
    return await asyncio.start_server(_prometheus_instant_handler, "127.0.0.1", port)


@pytest.fixture
def prometheus_query_range_success():
    payload = {
        "status": "success",
        "data": {
            "result": [
                {
                    "values": [
                        [1_700_000_000.0, "10.0"],
                        [1_700_000_060.0, "20.0"],
                    ]
                }
            ]
        },
    }
    return json.dumps(payload).encode()


@pytest.fixture
def prometheus_query_range_empty_result():
    payload = {"status": "success", "data": {"result": []}}
    return json.dumps(payload).encode()


@pytest.fixture
def prometheus_query_range_error_status():
    payload = {"status": "error", "error": "bad query"}
    return json.dumps(payload).encode()


def test_temporal_block_forecast_linearly_hours_cap():
    b = TemporalEvidenceBlock("cap")
    b.add_metric("m", [(0.0, 0.0), (120.0, 10.0)])
    fc = b.forecast_linearly(hours=1)
    assert set(fc.keys()) == {60}


@pytest.mark.asyncio
async def test_temporal_metric_and_block_forecasts(prometheus_query_range_success: bytes):
    m = TemporalMetric("cpu", [(100.0, 1.0), (160.0, 2.0)])
    assert m.current_value() == 2.0
    assert m.rate_of_change() is not None
    assert m.forecast_at(60) is not None
    assert m.sample_points == 2
    d = m.to_dict()
    assert d["name"] == "cpu"

    b = TemporalEvidenceBlock("p", namespace="ns", pod="pod-1", deployment="dep")
    b.add_metric("cpu", [(100.0, 10.0), (160.0, 40.0)])
    b.set_current_state({"phase": "Running"})
    b.alert_message = "OOMKilled risk"
    b.probe_status = "WARN"
    fc = b.forecast_linearly(hours=24)
    assert 60 in fc or 3600 in fc
    txt = b.to_prompt_block()
    assert "TEMPORAL_EVIDENCE" in txt and "LINEAR_FORECASTS" in txt
    blob = b.to_dict()
    assert blob["probe_name"] == "p" and "metrics" in blob


@pytest.mark.asyncio
async def test_temporal_metric_edge_rates():
    empty = TemporalMetric("x", [])
    assert empty.current_value() is None
    assert empty.rate_of_change() is None
    one = TemporalMetric("y", [(1.0, 5.0)])
    assert one.rate_of_change() is None
    flat = TemporalMetric("z", [(0.0, 1.0), (0.0, 2.0)])
    assert flat.rate_of_change() is None


@pytest.mark.asyncio
async def test_fetch_from_prometheus_success(prometheus_query_range_success: bytes):
    port = _free_port()
    srv = await _http_server_once(port, body=prometheus_query_range_success)
    async with srv:
        url = f"http://127.0.0.1:{port}"
        block = await TemporalEvidenceBlock.fetch_from_prometheus(
            url, "up", "cpu_load", hours_back=1, step="60s", timeout=5.0
        )
    assert block is not None
    assert "cpu_load" in block.metrics


@pytest.mark.asyncio
async def test_fetch_from_prometheus_no_series(prometheus_query_range_empty_result: bytes):
    port = _free_port()
    srv = await _http_server_once(port, body=prometheus_query_range_empty_result)
    async with srv:
        url = f"http://127.0.0.1:{port}"
        block = await TemporalEvidenceBlock.fetch_from_prometheus(
            url, "up", "m", hours_back=1, timeout=5.0
        )
    assert block is None


@pytest.mark.asyncio
async def test_fetch_from_prometheus_bad_status(prometheus_query_range_error_status: bytes):
    port = _free_port()
    srv = await _http_server_once(port, body=prometheus_query_range_error_status)
    async with srv:
        url = f"http://127.0.0.1:{port}"
        block = await TemporalEvidenceBlock.fetch_from_prometheus(
            url, "bad", "m", hours_back=1, timeout=5.0
        )
    assert block is None


@pytest.mark.asyncio
async def test_fetch_from_prometheus_invalid_samples_skipped():
    payload = {
        "status": "success",
        "data": {"result": [{"values": [["not_ts", "x"]]}]},
    }
    body = json.dumps(payload).encode()
    port = _free_port()
    srv = await _http_server_once(port, body=body)
    async with srv:
        url = f"http://127.0.0.1:{port}"
        block = await TemporalEvidenceBlock.fetch_from_prometheus(
            url, "up", "m", hours_back=1, timeout=5.0
        )
    assert block is None


@pytest.mark.asyncio
async def test_fetch_from_prometheus_connection_refused():
    port = _free_port()
    block = await TemporalEvidenceBlock.fetch_from_prometheus(
        f"http://127.0.0.1:{port}", "up", "m", hours_back=1, timeout=1.0
    )
    assert block is None


def test_deep_scout_redact_and_sensitive_keys():
    assert ds._is_sensitive_config_key("MY_PASSWORD") is True
    assert ds._is_sensitive_config_key("kubernetes.io/token") is True
    assert ds._is_sensitive_config_key("app.conf") is False
    raw = {"app": "ok", "api_key": "secret", "nested": "skip"}
    red = ds._redact_configmap_entries(raw)
    assert red["app"] == "ok"
    assert red["api_key"] == "<REDACTED>"


def test_deep_scout_embedding_from_response():
    vec = [0.5] * 8
    assert ds._embedding_from_response({"embedding": vec}) == vec
    assert ds._embedding_from_response({"embeddings": [vec]}) == vec
    with pytest.raises(ValueError):
        ds._embedding_from_response({})


def test_deep_scout_point_id_stable():
    a = ds._point_id_stable("host")
    b = ds._point_id_stable("host")
    c = ds._point_id_stable("other")
    assert a == b != c


@pytest.mark.asyncio
async def test_deep_scout_layer_metrics_baseline_local():
    port = _free_port()
    srv = await _prometheus_instant_server(port)
    async with srv:
        ws = WorkerSettings(prometheus_url=f"http://127.0.0.1:{port}")
        data, lines = await ds._layer_metrics_baseline(ws)
    assert "prometheus_url" in data
    assert "queries" in data
    assert "count_up" in data["queries"] or "error" in data
    assert "Prometheus" in lines or "baseline" in lines


@pytest.mark.asyncio
async def test_autonomous_vm_namespace_baselines_local():
    port = _free_port()
    srv = await _prometheus_instant_server(port)
    async with srv:
        ws = WorkerSettings(prometheus_url=f"http://127.0.0.1:{port}")
        ok, out = await dsa._vm_namespace_baselines(ws)
    assert ok is True
    assert "demo-ns" in out.get("namespaces_cpu", {}) or "demo-ns" in out.get("namespaces_mem", {})


def test_autonomous_embedding_and_pod_helpers():
    emb = [0.1] * 4
    assert dsa._embedding_from_response({"embedding": emb}) == emb
    pid = dsa._point_id_autonomous("Pod", "ns1", "nginx")
    assert pid == dsa._point_id_autonomous("Pod", "ns1", "nginx")

    port = SimpleNamespace(
        container_port=8080,
        name="http",
        protocol="TCP",
    )
    c1 = SimpleNamespace(name="app", image="nginx:1", ports=[port])
    spec = SimpleNamespace(containers=[c1], init_containers=[])
    p = SimpleNamespace(spec=spec)
    ports = dsa._pod_ports(p)
    assert ports and ports[0]["port"] == 8080
    conts = dsa._pod_containers(p)
    assert conts[0]["name"] == "app"


def test_memory_normalize_and_fingerprint():
    assert strip_ephemeral_from_args(None) == {}
    assert strip_ephemeral_from_args({"pod_name": "x", "ok": 1})["pod_name"] == "<ephemeral>"
    nested = {"outer": {"password": "x"}}
    sn = strip_ephemeral_from_args(nested)
    assert sn["outer"]["password"] == "<redacted>"
    with_list = strip_ephemeral_from_args({"items": [{"pod": "p1"}, "raw"]})
    assert with_list["items"][0]["pod"] == "<ephemeral>"
    assert with_list["items"][1] == "raw"
    t = canonical_symptom_text("  Pod abc-1234567890-abcde  ")
    assert "<pod>" in t or "pod" in t
    fp = extract_workload_fingerprint("namespace: prod deployment: checkout")
    assert fp.startswith("hash:") is False
    fp2 = extract_workload_fingerprint("no hints here at all")
    assert fp2.startswith("hash:")
    key = stable_playbook_pattern_key("t", "symptom", {"a": 1})
    assert len(key) == 24
    rid = routing_experience_point_id("CPU spike", "tool", {"x": 1})
    assert len(rid) > 10


def test_policy_normalize_and_gates():
    assert normalize_command("  a   b  ") == "a b"
    r = check_sandbox_command("", env_mode="prod")
    assert r.verdict == PolicyVerdict.DENIED
    r2 = check_sandbox_command("echo hi", env_mode="dev")
    assert r2.verdict == PolicyVerdict.ALLOWED_AUTO
    r3 = check_sandbox_command("rm -rf /", env_mode="prod", lab_unchained=False)
    assert r3.verdict == PolicyVerdict.DENIED
    r4 = check_sandbox_command("echo ok", lab_unchained=True)
    assert r4.verdict == PolicyVerdict.ALLOWED_AUTO
    r5 = check_sandbox_command("rm -rf /nope", lab_unchained=True, env_mode="prod")
    assert r5.verdict == PolicyVerdict.ALLOWED_AUTO
    r6 = check_sandbox_command("echo sandbox-clean", env_mode="prod", lab_unchained=False)
    assert r6.verdict == PolicyVerdict.ALLOWED_AUTO and r6.reason == "sandbox_ok"
    t = check_promotion_tool("k8s_rollout_restart", env_mode="dev")
    assert t.verdict == PolicyVerdict.ALLOWED_AUTO
    t2 = check_promotion_tool("unknown_tool")
    assert t2.verdict == PolicyVerdict.DENIED
    t3 = check_promotion_tool(
        "k8s_scale_deployment",
        cluster_full_access=True,
        env_mode="prod",
        lab_unchained=False,
    )
    assert t3.verdict == PolicyVerdict.ALLOWED_AUTO
    t4 = check_promotion_tool(
        "kubectl_apply_whatever",
        cluster_full_access=True,
        env_mode="prod",
        lab_unchained=False,
    )
    assert t4.verdict == PolicyVerdict.DENIED
    t5 = check_promotion_tool("", env_mode="prod")
    assert t5.verdict == PolicyVerdict.DENIED
    t6 = check_promotion_tool("k8s_rollout_restart", lab_unchained=True, env_mode="prod")
    assert t6.verdict == PolicyVerdict.ALLOWED_AUTO
    t7 = check_promotion_tool(
        "k8s_rollout_restart",
        env_mode="prod",
        lab_unchained=False,
        cluster_full_access=False,
    )
    assert t7.verdict == PolicyVerdict.ALLOWED_AUTO and t7.reason == "promotion_ok"


def test_promotion_strip_and_parse_json():
    raw = "<think>noise</think>  ```json\n{\"pass\": true, \"confidence\": 0.8, \"rationale\": \"x\"}\n```"
    parsed = promotion_mod._parse_validation_json(raw)
    assert parsed["pass"] is True
    plain = '{"pass": false, "confidence": 0.1, "rationale": "nope"}'
    assert promotion_mod._parse_validation_json(plain)["pass"] is False


@pytest.mark.asyncio
async def test_promotion_execute_write_pending_unknown_kind():
    class Ctx:
        inbound_trace_id = "tr-1"

    out = await promotion_mod.execute_write_pending_from_redis(Ctx(), {"kind": "other", "trace_id": "tr-1"})
    assert "[DATA] error" in out


def test_pod_env_clone_safe_env_filters():
    e1 = SimpleNamespace(name="LOG_LEVEL", value="info", value_from=None)
    e2 = SimpleNamespace(name="API_TOKEN", value="leak", value_from=None)
    e3 = SimpleNamespace(name="FROM_SECRET", value=None, value_from=object())
    c = SimpleNamespace(env=[e1, e2, e3])
    out = _safe_env_from_container(c)
    names = {x["name"] for x in out}
    assert "LOG_LEVEL" in names and "API_TOKEN" not in names


def test_truncate_lesson_and_routing_point_id():
    assert truncate_lesson_to_budget("hi", 10) == "hi"
    long = "a" * 100
    clipped = truncate_lesson_to_budget(long, 20)
    assert len(clipped) == 20 and clipped.endswith("...")


class _RecordingKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_dict(self, topic: str, payload: dict[str, Any]) -> None:
        self.sent.append((topic, payload))


@pytest.mark.asyncio
async def test_sandbox_manager_disabled_and_policy_paths():
    ws = WorkerSettings()
    mgr = SandboxManager(ws)
    assert mgr.enabled is False
    k = _RecordingKafka()
    res = await mgr.execute_shell_structured(
        kafka=k,
        command="echo hi",
        session_id="s",
        trace_id="trace-one",
    )
    assert res.exit_code == -1 and "disabled" in res.stderr
    assert k.sent

    res2 = await mgr.execute_shell_structured(
        kafka=k,
        command="",
        session_id="s",
        trace_id="trace-two",
    )
    assert res2.exit_code == -1

    ws_on = WorkerSettings(opensandbox_enabled=True)
    mgr_on = SandboxManager(ws_on)
    long_cmd = "x" * 9000
    res3 = await mgr_on.execute_shell_structured(
        kafka=k,
        command=long_cmd,
        session_id="s",
        trace_id="trace-three",
    )
    assert res3.policy_reason == "command_too_long"

    ws_lab = WorkerSettings(
        env_mode="prod",
        lab_unchained=False,
        opensandbox_enabled=True,
    )
    mgr_deny = SandboxManager(ws_lab)
    k2 = _RecordingKafka()
    res4 = await mgr_deny.execute_shell_structured(
        kafka=k2,
        command="rm -rf /tmp/x",
        session_id="s",
        trace_id="trace-four",
    )
    assert res4.exit_code == -2
    assert "strict_denylist" in res4.policy_reason or res4.policy_verdict == PolicyVerdict.DENIED.value


def test_sandbox_result_to_user_text_branches():
    d = SandboxExecResult(
        trace_id="trace-t",
        session_id="s",
        command="c",
        run_id="r",
        exit_code=-1,
        stdout="",
        stderr="disabled",
        http_status=None,
        policy_verdict="disabled",
    )
    assert "OpenSandbox tắt" in sandbox_result_to_user_text(d)
    pol = SandboxExecResult(
        trace_id="trace-t",
        session_id="s",
        command="c",
        run_id="r",
        exit_code=-2,
        stdout="",
        stderr="x",
        http_status=None,
        policy_verdict="denied",
        policy_reason="bad",
    )
    assert "Policy từ chối" in sandbox_result_to_user_text(pol)
    n404 = SandboxExecResult(
        trace_id="trace-t",
        session_id="s",
        command="c",
        run_id="r",
        exit_code=-1,
        stdout="",
        stderr="404",
        http_status=404,
        policy_verdict="allowed_auto",
    )
    assert "404" in sandbox_result_to_user_text(n404)


class _SlotSemaphore:
    async def acquire(self, timeout_s: float = 120.0) -> str:
        return "slot-token"

    async def release(self, token: str) -> None:
        assert token == "slot-token"


class _StubLLMChatEmbed:
    def __init__(self, chat_content: str = "Bài học ngắn.") -> None:
        self._chat = chat_content

    async def chat(self, **_kw: Any) -> dict[str, Any]:
        return {"message": {"content": self._chat}}

    async def embed(self, **_kw: Any) -> dict[str, Any]:
        return {"embedding": [0.01] * EMBED_DIM}


class _FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    async def upsert(self, **kw: Any) -> None:
        self.upserts.append(kw)

    async def query_points(self, **kw: Any) -> Any:
        self.queries.append(kw)

        class Pt:
            score = 0.91
            payload = {"lesson": "prior routing lesson", "routing_source": ROUTING_SOURCE_SLOW_PATH}

        class Resp:
            points = [Pt()]

        return Resp()


@pytest.mark.asyncio
async def test_synthesize_lesson_and_upsert_experience():
    ws = WorkerSettings()
    llm = _StubLLMChatEmbed()
    inp_ok = SandboxLessonInput(
        trace_id="trace-a",
        run_id="b",
        command="uptime",
        exit_code=0,
        stdout="ok",
        stderr="",
        user_snippet="check",
        policy_blocked=False,
        policy_reason="",
    )
    text = await synthesize_lesson_text(llm, ws, inp_ok, log_clip=200)
    assert "Bài học" in text or "lesson" in text.lower() or len(text) > 0

    inp_block = SandboxLessonInput(
        trace_id="trace-a",
        run_id="b",
        command="x",
        exit_code=0,
        stdout="",
        stderr="",
        user_snippet="",
        policy_blocked=True,
        policy_reason="denylist",
    )
    blocked = await synthesize_lesson_text(llm, ws, inp_block, log_clip=200)
    assert "policy" in blocked.lower() or "chặn" in blocked

    vs = _FakeVectorStore()
    pid = await upsert_action_experience(
        vs,
        lesson="L",
        vector=[0.0] * EMBED_DIM,
        payload={"trace_id": "t", "run_id": "r"},
        point_id="fixed-id",
    )
    assert pid == "fixed-id"
    assert vs.upserts


@pytest.mark.asyncio
async def test_record_sandbox_lesson_and_fetch_context():
    ws = WorkerSettings()

    class _Ctx:
        settings = ws
        llm = _StubLLMChatEmbed()
        vector_store = _FakeVectorStore()
        semaphore = _SlotSemaphore()

    ctx = _Ctx()
    inp = SandboxLessonInput(
        trace_id="trace-ninety",
        run_id="r99",
        command="true",
        exit_code=0,
        stdout="",
        stderr="",
        user_snippet="goal",
        policy_blocked=False,
        policy_reason="",
    )
    await record_sandbox_lesson(ctx, inp)
    assert ctx.vector_store.upserts

    short = await fetch_action_experience_context(ctx, "short")
    assert short == ""

    long_q = "something wrong with deployment checkout in namespace prod"
    blob = await fetch_action_experience_context(ctx, long_q)
    assert "action_experience" in blob or blob == ""


@pytest.mark.asyncio
async def test_sandbox_manager_health_disabled():
    ws = WorkerSettings()
    mgr = SandboxManager(ws)
    ok, msg = await mgr.health_check()
    assert ok is False and "disabled" in msg.lower()
