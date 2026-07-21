"""Multi-turn closed-loop diagnosis for RemoteAgent evidence.

INVARIANT INV_NO_SINGLE_TURN: Minimum 2 LLM turns before emitting conclusion.
INVARIANT INV_DIAG_STORED: Full session stored in Redis before Telegram emit.
INVARIANT INV_READONLY_CMDS: Commands dispatched via Redis queue, enforced at agent.

Flow per turn:
  1. Build context (vm_profile + evidence + previous turns)
  2. Call LLM → parse DiagnosisTurnResponse
  3. If diagnosis_complete or max turns → finalize
  4. Else: enqueue commands via Redis → wait for results → next turn

Redis keys:
  omni:agent:profile:{agent_id}          VMProfile from discovery
  omni:agent:cmd:{agent_id}              Command queue (LPUSH) to agent
  omni:diag:cmdresult:{cmd_id}           Command result from agent (poll)
  omni:diag:session:{trace_id}           FinalDiagnosis stored here
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TURNS = 8
_MIN_TURNS = 2
_CMD_RESULT_POLL_INTERVAL_S = 5.0
_CMD_RESULT_TIMEOUT_S = 90.0
_CMD_QUEUE_PREFIX = "omni:agent:cmd:"
_CMD_RESULT_PREFIX = "omni:diag:cmdresult:"
_PROFILE_KEY_PREFIX = "omni:agent:profile:"
_REGISTRY_KEY_PREFIX = "omni:remote_agent:registry:"
_SESSION_KEY_PREFIX = "omni:diag:session:"
_SESSION_TTL = 86400
_CMD_QUEUE_TTL = 300
# Agent counts as online if it re-registered within this window (gateway TTL=120s).
_AGENT_ONLINE_MAX_AGE_S = 120

_DIAGNOSIS_SYSTEM_PROMPT = """You are an SRE diagnostic AI for Linux bare-metal and VM systems.
You perform ROOT CAUSE ANALYSIS through iterative evidence gathering.

EVIDENCE PRIORITY — follow this order strictly:
1. EXTRACTED FACTS FIRST: The [INITIAL EVIDENCE] block contains pre-collected metrics in "Facts:" JSON.
   Treat facts like disk_usage_pct, error_count, inode_free as CONFIRMED measurements — do NOT re-verify them.
   If facts already establish the root cause (e.g. disk_usage_pct >= 95), set diagnosis_complete=true immediately.
2. RAW LOG LINES: The "Alert:" and kernel/syslog lines in the evidence are real observed events.
3. COMMAND OUTPUT: Use commands only to fill gaps not covered by facts or logs.

COMMAND RULES — subprocess exec (no shell):
- Commands run via subprocess.exec — shell features DO NOT work.
- NEVER use globs like /var/* or /etc/*. Use exact paths: /var/log, /var/lib, /var/cache.
  A glob will FAIL — "du -sh /var/*" is wrong; emit one entry per path instead.
- NEVER use pipes (|), redirects (>), semicolons (;), or backticks.
- One command per entry. Multiple paths = multiple command entries.
- Prefer specific paths over wildcards. Wrong: "df -h /var/*". Correct: "df -h /var".
- If a command fails (rc != 0), do NOT retry the SAME command. Move to an alternative.

TOOL SELECTION — match the symptom to the RIGHT tool (critical for quality):
- INODE exhaustion / "no space" with free bytes → df -i <path>   (NOT du; du measures bytes, df -i measures inodes).
  Then find the dir with most files: for inodes, prefer "ls" counts on suspect dirs, not "du -sh".
- DISK BYTES full → df -h <path>, then du -sh <exact subdir> (e.g. /var/log, /var/lib/docker).
- RUNAWAY LOGS → ls -lS /var/log (largest first), du -sh /var/log, journalctl --disk-usage.
- SERVICE DOWN / crashloop → systemctl status <unit>, journalctl -u <unit> -n 200, systemctl is-failed <unit>.
- HIGH MEMORY / OOM → free -h, ps aux --sort=-%rss, dmesg (grep oom not allowed — read dmesg raw).
- PORT / CONNECTION → ss -ltnp, ss -s.
- Pick the tool that DIRECTLY measures the quantity named in the alert. Re-read alert_hint before choosing.

DIAGNOSIS RULES:
1. If extracted facts already show the problem (high disk, service down, error spike) → conclude in turn 1-2.
2. Only request commands to fill gaps NOT covered by existing evidence.
3. After 2 consecutive rc=1 on the same target, skip it and reason from what you have.
4. A result marked TIMEOUT / agent_unreachable means the command DID NOT RUN (agent offline) — it is NOT a
   permission error and NOT evidence about the host. Do NOT infer anything from it; do NOT retry it; conclude
   from the pre-collected facts instead.
5. Minimum 2 turns before concluding. Maximum 8 turns.
6. Always populate remediation_steps — even when confidence is moderate (>= 0.5).
7. Commands must be read-only: df, du, ls, stat, ps, ss, systemctl status, journalctl, free, lsblk.

SECURITY — METADATA ONLY (INV_NO_DATA_EXFIL, non-negotiable):
- You inspect METADATA: sizes, counts, listings, process/network/disk/service STATUS.
- You MUST NOT read the CONTENT of any file or database. These are HARD-BLOCKED by the
  agent and will fail: cat, head, tail, grep, awk, sed, cut, strings, less, more, wc,
  mysql/psql SELECT, curl, nc. Do NOT request them — pick a metadata alternative.
- To find big logs: use "ls -lS <dir>" and "du -sh <dir>" (sizes), NEVER "cat"/"tail" the log.
- To inspect a service: "systemctl status <unit>" and "journalctl -u <unit> --no-pager -n 50"
  (operational logs are allowed); never cat the app's own data/log files.
- Omni commits to the operator: we never exfiltrate a single line of their VM's data.

SYSTEM-THINKING — BLAST RADIUS:
- Every alert affects the wider system. Beyond the local fault, reason about what ELSE is
  impacted: dependent services, listeners/ports, databases, downstream APIs — using the
  discovered VM PROFILE (services + listeners) as your dependency baseline.
- When you need to confirm impact scope, request metadata commands that measure it
  (e.g. "systemctl is-active <dependent-unit>", "ss -ltnp" for affected listeners).
- Populate the "blast_radius" field with the system-wide impact assessment.

OUTPUT FORMAT (strict JSON, no markdown):
{
  "reasoning": "<your thinking — cite specific fact values or log lines>",
  "hypothesis": "<current best hypothesis with specific component>",
  "evidence_gaps": ["<only list gaps NOT already in facts>"],
  "commands_to_run": [
    {"command": "df", "args": ["-h", "/var"], "purpose": "verify current disk usage on /var partition"}
  ],
  "diagnosis_complete": false,
  "confidence": 0.4,
  "root_cause": null,
  "affected_components": [],
  "blast_radius": "",
  "impact_summary": "",
  "remediation_steps": [],
  "suggested_recovery": null
}

SUGGESTED_RECOVERY — for automated dispatch, extremely narrow scope. Three capabilities exist —
pick AT MOST ONE, and only when its specific condition is clearly met:

1. "systemd.restart_unit" — the unit is CURRENTLY down/inactive/crash-looping (still broken right
   now) — confirmed either by the pre-collected "failed_units"/"critical_failed_units" facts in
   [INITIAL EVIDENCE], OR by "systemctl status <unit>" / "journalctl -u <unit>" output seen this
   session — AND a plain restart is the correct, sufficient fix — not a symptom of disk/memory/
   inode exhaustion, not a dependency outage, not something requiring config changes.
   Format: {"capability": "systemd.restart_unit", "unit": "<exact unit name copied verbatim>"}

2. "systemd.reset_failed" — the unit shows "failed" in "systemctl is-failed <unit>" / status output
   (e.g. it hit systemd's start-limit and stopped retrying) BUT other evidence THIS session shows
   the underlying problem is already resolved (e.g. a dependency that was down is now active, or
   the process is confirmed healthy by another check) — the "failed" flag is stale bookkeeping, not
   a live problem. This does NOT restart or start the unit — it only clears systemd's failed-state
   counter so the unit can be started/retried normally afterward. Use this INSTEAD of restart_unit
   when the evidence shows the fix already happened and only the stale flag remains.
   Format: {"capability": "systemd.reset_failed", "unit": "<exact unit name copied verbatim>"}

3. "systemd.journal_vacuum" — disk pressure is caused by systemd's OWN journal log data (e.g. "df"
   showing a full/near-full partition together with a large "journalctl --disk-usage" figure seen
   THIS session, or /var/log/journal appearing as the largest consumer in a "du -sh" output) — NOT
   by application data, database files, uploads, or anything outside journald's own retained logs.
   This runs the official "journalctl --vacuum-size=<target>" — it NEVER deletes application data,
   NEVER restarts/stops any process. The target unit is ALWAYS the literal string
   "systemd-journald.service" (journal vacuum acts on journald's own data, not on the unit whose
   disk filled up) — do not substitute any other unit name for this capability.
   Format: {"capability": "systemd.journal_vacuum", "unit": "systemd-journald.service"}

- In cases 1 and 2 the unit name MUST be copied verbatim from the evidence facts or a command
  output — never invent one, never use a unit name only mentioned in free-text alert prose. Never
  add or remove a ".service" suffix yourself, copy the string exactly as it appears in the
  evidence. Case 3 is the one exception: the unit is always the fixed literal
  "systemd-journald.service", not copied from evidence.
- In every other case (disk full from app/database data, OOM, inode exhaustion, network issue,
  unclear root cause, confidence < 0.75, or ANY doubt about which of the three capabilities
  applies) — set suggested_recovery to null. Leaving it null is always safe; a wrong non-null value
  could trigger an unwanted automated restart, a premature reset-failed on a unit that is still
  actually broken, or a journal vacuum that does not address the real disk consumer.

GROUNDING — NON-NEGOTIABLE (INV_DIAG_GROUNDED):
- Every number, percentage, file path, mount point, and service name in root_cause,
  impact_summary, and remediation_steps MUST appear VERBATIM in the evidence facts or in a
  command output from THIS session. If you did not see it, do not write it.
- NEVER write "confirmed" about a quantity you did not measure with the matching tool
  (inode claim requires a df -i output in this session; disk % requires a df output).
- If evidence is insufficient, say so and lower confidence — do not fill gaps from memory.

SCOPE — HOST-SHARED MOUNTS ARE OFF-LIMITS:
- Mounts of the hypervisor host filesystem (e.g. /mnt/mac, fstype virtiofs/9p/vboxsf/prl_fs)
  belong to the HOST machine, not this VM. Never diagnose them as this VM's problem and
  never propose remediation that touches files under them.

When diagnosis_complete=true, set root_cause to a single concrete sentence naming the exact
component and the measured value you saw in evidence (cite the number verbatim), list
affected_components, and provide 3-5 remediation_steps.

REMEDIATION FORMAT — each step MUST be a concrete, copy-pasteable shell command, NOT prose.
The operator runs these by hand, so vague advice is useless.
- WRONG (prose): "Archive or delete unnecessary log files"
- RIGHT (command): "sudo journalctl --vacuum-size=500M  # truncate journal to 500M"
- RIGHT (command): "sudo truncate -s 0 <exact-path-you-saw-in-ls-or-du-output>  # zero the largest offender"
Reference ONLY the exact paths/files/services that appeared in this session's command
output — never a path from general knowledge or from these format examples."""


_TRUNCATED_NOTE = "\n…[truncated for context budget]"
_TRUNCATE_KEEP_CHARS = 400
_CHARS_PER_TOKEN = 3  # heuristic for log-heavy text
_KEEP_RECENT_MESSAGES = 4  # 2 most recent (assistant, user) pairs


def _enforce_context_budget(
    messages: list[dict[str, str]], num_ctx: int
) -> list[dict[str, str]]:
    """Return a NEW message list trimmed to fit the LLM context window.

    Ollama silently drops the HEAD of the context (system prompt rules) when
    num_ctx is exceeded. To prevent that, keep messages[0] (system) and
    messages[1] (initial evidence) intact plus the 2 most recent turn pairs,
    and truncate the content of everything in between.
    """
    budget = num_ctx * _CHARS_PER_TOKEN
    total_chars = sum(len(m["content"]) for m in messages)
    if total_chars <= budget or len(messages) <= 2 + _KEEP_RECENT_MESSAGES:
        return list(messages)

    head = [dict(m) for m in messages[:2]]
    tail = [dict(m) for m in messages[-_KEEP_RECENT_MESSAGES:]]
    middle = [
        {**m, "content": m["content"][:_TRUNCATE_KEEP_CHARS] + _TRUNCATED_NOTE}
        for m in messages[2:-_KEEP_RECENT_MESSAGES]
    ]
    trimmed = head + middle + tail
    logger.warning(
        "[diag-loop] context budget exceeded (%d chars > %d): truncated %d middle messages",
        total_chars, budget, len(middle),
    )
    return trimmed


def _format_command(cmd: dict[str, Any]) -> str:
    """Render a command dict as the exact shell-like string that was executed.

    Used to show the operator WHAT was run on the host (e.g. "ls -lS /var/log"),
    not just the LLM's free-text purpose.
    """
    name = str(cmd.get("command", "")).strip()
    args = [str(a) for a in cmd.get("args", [])]
    return " ".join([name, *args]).strip()


# ── Grounding gate (INV_DIAG_GROUNDED) ──────────────────────────────────────
# The 7B model has been observed parroting concrete paths/numbers from its own
# prompt examples into conclusions (e.g. "/var/log/vmware/hostd.log", "inode
# exhaustion confirmed" with no df -i in session). Post-hoc gate: any absolute
# path or percentage in the final conclusion must appear verbatim somewhere in
# this session's evidence (facts + alert + command outputs), else the claim is
# flagged, the offending remediation step dropped, and confidence capped.
_GROUND_PATH_RE = re.compile(r"(?:/[\w.@+-]+){2,}")
_GROUND_PCT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?%")
_UNGROUNDED_CONFIDENCE_CAP = 0.3
_GATE_DROP_NOTE = "[grounding-gate] dropped {n} step(s) referencing paths/numbers absent from evidence"

# systemd.journal_vacuum's target unit is ALWAYS this literal (see diagnosis
# prompt case 3) — it is never copied from evidence like the other two
# capabilities' units are, so the grounding gate checks it against this one
# valid value instead of substring-matching the evidence corpus.
_JOURNAL_VACUUM_FIXED_UNIT = "systemd-journald.service"


def _extract_groundable_claims(text: str) -> set[str]:
    return set(_GROUND_PATH_RE.findall(text)) | set(_GROUND_PCT_RE.findall(text))


def _apply_grounding_gate(
    final: dict[str, Any], evidence_corpus: str
) -> dict[str, Any]:
    """Return a NEW final dict with ungrounded claims flagged and neutralized."""
    root_cause = str(final.get("root_cause", "") or "")
    ungrounded_rc = sorted(
        c for c in _extract_groundable_claims(root_cause) if c not in evidence_corpus
    )

    kept_steps: list[str] = []
    dropped_steps: list[str] = []
    ungrounded_step_claims: list[str] = []
    for step in final.get("remediation_steps", []) or []:
        step_s = str(step)
        bad = sorted(
            c for c in _extract_groundable_claims(step_s) if c not in evidence_corpus
        )
        if bad:
            dropped_steps.append(step_s)
            ungrounded_step_claims.extend(bad)
        else:
            kept_steps.append(step_s)
    if dropped_steps:
        kept_steps.append(_GATE_DROP_NOTE.format(n=len(dropped_steps)))

    suggested = final.get("suggested_recovery")
    suggested_unit = str(suggested.get("unit", "")) if isinstance(suggested, dict) else ""
    if isinstance(suggested, dict) and suggested.get("capability") == "systemd.journal_vacuum":
        # Fixed-target capability: ground against the one valid literal
        # instead of the evidence corpus — see _JOURNAL_VACUUM_FIXED_UNIT.
        suggested_unit_ungrounded = suggested_unit != _JOURNAL_VACUUM_FIXED_UNIT
    else:
        suggested_unit_ungrounded = (
            isinstance(suggested, dict) and suggested_unit not in evidence_corpus
        )

    ungrounded = sorted(set(ungrounded_rc) | set(ungrounded_step_claims))
    if not ungrounded and not suggested_unit_ungrounded:
        return dict(final)

    result = dict(final)
    if suggested_unit_ungrounded:
        logger.warning(
            "[diag-loop] grounding gate: suggested_recovery.unit %r absent from evidence — dropped",
            suggested.get("unit", ""),
        )
        result["suggested_recovery"] = None

    if not ungrounded:
        return result

    logger.warning(
        "[diag-loop] grounding gate: ungrounded claims %s — confidence capped", ungrounded
    )
    new_root_cause = (
        f"[UNVERIFIED: {', '.join(ungrounded_rc)}] {root_cause}"
        if ungrounded_rc else root_cause
    )
    return {
        **result,
        "root_cause": new_root_cause,
        "remediation_steps": kept_steps,
        "confidence": min(float(final.get("confidence", 0.0) or 0.0), _UNGROUNDED_CONFIDENCE_CAP),
        "ungrounded_claims": ungrounded,
        "dropped_remediation_steps": dropped_steps,
    }


# Capabilities the diagnosis loop is allowed to suggest for automated dispatch.
# Keep in sync with workers.auto_recovery_bridge._SUPPORTED_CAPABILITIES — a
# capability the bridge does not know how to dispatch must never be suggested
# here (it would just be silently dropped downstream, better to fail closed
# at the single source of truth for "what capabilities exist").
_SUGGESTABLE_CAPABILITIES = frozenset({
    "systemd.restart_unit", "systemd.reset_failed", "systemd.journal_vacuum",
})


def _parse_suggested_recovery(raw: Any) -> dict[str, str] | None:
    """Defensively shape the LLM's raw suggested_recovery field. Any
    malformed/partial shape becomes None — this function never raises, since
    a bad automated-dispatch hint must fail closed, not break diagnosis."""
    if not isinstance(raw, dict):
        return None
    capability = str(raw.get("capability", "")).strip()
    unit = str(raw.get("unit", "")).strip()
    if capability not in _SUGGESTABLE_CAPABILITIES or not unit:
        return None
    return {"capability": capability, "unit": unit}


_FALLBACK_LABEL = "[generic fallback — not host-specific; verify before running]"


def _fallback_remediation(text: str) -> list[str]:
    """Labeled wrapper: prepend a generic-fallback notice so the operator knows
    these steps are NOT host-specific diagnosis output."""
    return [_FALLBACK_LABEL, *_fallback_remediation_steps(text)]


def _fallback_remediation_steps(text: str) -> list[str]:
    """Generate keyword-based remediation steps when LLM leaves remediation_steps empty."""
    t = text.lower()
    if "disk" in t or "inode" in t or "space" in t or "partition" in t:
        return [
            "Run: df -h (identify which partition is full)",
            "Run: du -sh /var/log /var/lib /var/cache (find largest dirs)",
            "Free space: journalctl --vacuum-size=500M",
            "Free space: find /var/tmp -mtime +7 -delete",
            "If log rotation broken: logrotate --force /etc/logrotate.conf",
        ]
    if "service" in t or "process" in t or "crash" in t or "failed" in t:
        return [
            "Check service: systemctl status <service>",
            "Review logs: journalctl -xe -n 200",
            "Restart if safe: systemctl restart <service>",
        ]
    if "memory" in t or "oom" in t or "swap" in t:
        return [
            "Check memory: free -h",
            "Find consumers: ps aux --sort=-%mem | head -10",
            "Review OOM: journalctl -k | grep -i oom",
        ]
    if "mysql" in t or "database" in t or "db" in t:
        return [
            "Check MySQL: systemctl status mysql",
            "Check connections: mysqladmin status",
            "Review slow queries: mysqladmin processlist",
        ]
    return [
        "Review system logs: journalctl -xe -n 200",
        "Check disk: df -h",
        "Check memory: free -h",
        "Check failed services: systemctl --failed",
    ]


async def _load_vm_profile(redis: Any, agent_id: str) -> dict[str, Any]:
    key = f"{_PROFILE_KEY_PREFIX}{agent_id}"
    try:
        raw = await redis.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.debug("[diag-loop] vm_profile load failed agent=%s err=%s", agent_id, exc)
    return {}


async def _agent_is_online(redis: Any, agent_id: str) -> bool:
    """True if the agent re-registered within _AGENT_ONLINE_MAX_AGE_S.

    Commands are only dispatched to a live agent — enqueuing to an unregistered
    or stale agent_id means nobody polls the queue and every command would time
    out after 90s. We short-circuit that wasted 8×90s and run a degraded,
    facts-only diagnosis instead.
    """
    try:
        raw = await redis.get(f"{_REGISTRY_KEY_PREFIX}{agent_id}")
        if not raw:
            return False
        rec = json.loads(raw)
        last_seen = int(rec.get("last_seen", 0))
        return (time.time() - last_seen) <= _AGENT_ONLINE_MAX_AGE_S
    except Exception as exc:
        logger.debug("[diag-loop] agent_online check failed agent=%s err=%s", agent_id, exc)
        return False


async def _enqueue_commands(
    redis: Any,
    agent_id: str,
    commands: list[dict[str, Any]],
    trace_id: str,
) -> list[str]:
    """Enqueue commands to Redis list for agent to poll. Returns list of cmd_ids."""
    cmd_ids: list[str] = []
    queue_key = f"{_CMD_QUEUE_PREFIX}{agent_id}"
    for cmd in commands[:5]:  # cap per turn
        cmd_id = f"cmd-{uuid.uuid4().hex[:12]}"
        payload = json.dumps({
            "cmd_id": cmd_id,
            "command": cmd.get("command", ""),
            "args": cmd.get("args", []),
            "timeout_s": cmd.get("timeout_s", 30),
            "trace_id": trace_id,
            "purpose": cmd.get("purpose", ""),
            "command_kind": "diagnostic_probe",
            "enqueued_at": int(time.time()),
        })
        try:
            await redis.lpush(queue_key, payload)
            await redis.expire(queue_key, _CMD_QUEUE_TTL)
            cmd_ids.append(cmd_id)
        except Exception as exc:
            logger.warning("[diag-loop] enqueue_cmd failed cmd=%s err=%s", cmd.get("command"), exc)
    return cmd_ids


async def _wait_for_results(
    redis: Any,
    cmd_ids: list[str],
    timeout_s: float = _CMD_RESULT_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Poll Redis until all cmd_ids have results or timeout. Returns list of result dicts."""
    deadline = time.monotonic() + timeout_s
    pending = set(cmd_ids)
    results: list[dict[str, Any]] = []

    while pending and time.monotonic() < deadline:
        await asyncio.sleep(_CMD_RESULT_POLL_INTERVAL_S)
        for cmd_id in list(pending):
            key = f"{_CMD_RESULT_PREFIX}{cmd_id}"
            try:
                raw = await redis.get(key)
                if raw:
                    results.append(json.loads(raw))
                    pending.discard(cmd_id)
            except Exception:
                pass

    if pending:
        logger.warning(
            "[diag-loop] timeout waiting for cmd results: %s", list(pending)
        )
        for cmd_id in pending:
            results.append({
                "cmd_id": cmd_id,
                "blocked": False,
                "status": "timeout",  # command never executed — agent unreachable
                "stdout": "",
                "stderr": "TIMEOUT: agent did not poll/execute this command in time (agent likely offline)",
                "rc": 124,  # standard timeout exit code — distinct from a real rc=1 failure
                "duration_ms": int(timeout_s * 1000),
            })

    return results


def _parse_llm_response(raw_text: str) -> dict[str, Any]:
    """Parse LLM JSON output, handle markdown fences and partial JSON."""
    text = raw_text.strip()
    # Strip markdown code fence if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            l for l in lines
            if not l.startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except Exception:
                pass
    logger.warning("[diag-loop] LLM response not parseable: %s", text[:200])
    return {
        "reasoning": text[:500],
        "hypothesis": "parse_error",
        "commands_to_run": [],
        "diagnosis_complete": False,
        "confidence": 0.0,
        "_parse_error": True,
    }


def _build_initial_context(
    vm_profile: dict[str, Any],
    ev_doc: dict[str, Any],
) -> str:
    """Build the FIRST user message: VM profile + initial evidence.

    INV_ONE_ALERT_ONE_SESSION: this is sent exactly once at turn 1. Subsequent
    turns append only the NEW command results via _build_followup_context — the
    LLM keeps the prior context in its own conversation history, so we never
    re-send the initial evidence or replay earlier turns as flat text.
    """
    parts: list[str] = []

    # VM profile summary (no file content)
    if vm_profile:
        services = [s["name"] for s in vm_profile.get("services", [])[:20]]
        listeners = [f":{l['port']}({l.get('service','')})" for l in vm_profile.get("listeners", [])[:15]]
        os_info = vm_profile.get("os_info", {})
        parts.append(
            f"[VM PROFILE]\n"
            f"Hostname: {vm_profile.get('hostname', 'unknown')}\n"
            f"OS: {os_info.get('distro', 'unknown')} kernel={os_info.get('kernel', '')}\n"
            f"Running services: {', '.join(services) or 'none detected'}\n"
            f"Listening ports: {', '.join(listeners) or 'none detected'}\n"
        )

    # Initial evidence — highlight extracted facts so LLM uses them as primary evidence
    alert_hint = ev_doc.get("alert_hint", "")
    probe = ev_doc.get("probe", "")
    lane = ev_doc.get("lane", "")
    extracted_raw = ev_doc.get("extracted_fact", {})
    if isinstance(extracted_raw, str):
        try:
            extracted = json.loads(extracted_raw)
        except Exception:
            extracted = {}
    else:
        extracted = extracted_raw if isinstance(extracted_raw, dict) else {}

    # Build a human-readable summary of key metrics from extracted_fact
    key_metrics: list[str] = []
    for k, v in (extracted or {}).items():
        if k in ("agent_id", "hostname", "e2e_test", "simulated"):
            continue
        key_metrics.append(f"{k}={v}")

    metrics_block = (
        "KEY METRICS (pre-collected, treat as CONFIRMED):\n  " + "\n  ".join(key_metrics)
        if key_metrics else "No pre-collected metrics."
    )

    parts.append(
        f"[INITIAL EVIDENCE]\n"
        f"Probe: {probe} | Lane: {lane}\n"
        f"Alert: {alert_hint[:500]}\n"
        f"{metrics_block}\n"
        f"Raw facts JSON: {json.dumps(extracted, ensure_ascii=False)[:600]}\n"
    )

    parts.append(
        f"\nAnalyze the evidence above. If you have enough to conclude, set "
        f"diagnosis_complete=true. Otherwise request read-only commands. "
        f"Turn 1 of {_MAX_TURNS}."
    )

    return "\n\n".join(parts)


def _build_followup_context(
    command_results: list[dict[str, Any]],
    next_turn: int,
) -> str:
    """Build the user message for turn >= 2 — ONLY the new command results.

    The LLM already has the initial evidence and all prior hypotheses/results
    in its conversation history (message-history is accumulated, not reset).
    Resending them would waste context and risk contradicting the model's own
    remembered reasoning, so we send only what is new since the last turn.
    """
    parts: list[str] = ["[COMMAND RESULTS from your previous request]"]
    if not command_results:
        parts.append("(no commands were dispatched)")
    for cmd_result in command_results:
        cmd_id = cmd_result.get("cmd_id", "")
        purpose = cmd_result.get("purpose", "")
        stdout = cmd_result.get("stdout", "")[:1500]
        stderr = (cmd_result.get("stderr", "") or "")[:300]
        rc = cmd_result.get("rc", 0)
        header = f"[CMD {cmd_id}]{' (' + purpose + ')' if purpose else ''}"
        if cmd_result.get("blocked"):
            parts.append(f"{header} BLOCKED: {cmd_result.get('block_reason', '')}")
        elif cmd_result.get("status") == "timeout":
            # The command never ran — make this unambiguous so the LLM does not
            # mistake an unreachable agent for a permission error or host evidence.
            parts.append(
                f"{header}\nTIMEOUT — agent_unreachable: command did NOT execute. "
                f"Ignore as evidence; diagnose from pre-collected facts."
            )
        elif rc != 0:
            # Real non-zero exit: surface stderr so the LLM can pick an alternative.
            parts.append(f"{header}\nrc={rc}\nstderr: {stderr}\n{stdout}")
        else:
            parts.append(f"{header}\nrc={rc}\n{stdout}")

    parts.append(
        f"\nIncorporate these results with everything you already reasoned about. "
        f"Do NOT re-request commands you already ran. If you can now conclude, set "
        f"diagnosis_complete=true. Turn {next_turn} of {_MAX_TURNS}."
    )
    return "\n\n".join(parts)


def _extract_raw_content(resp: Any) -> str:
    """Pull the assistant text out of whatever shape the LLM client returns."""
    if hasattr(resp, "message"):
        return resp.message.content or ""
    if hasattr(resp, "choices") and resp.choices:
        return resp.choices[0].message.content or ""
    if isinstance(resp, dict):
        return (resp.get("message") or {}).get("content", "") or ""
    return ""


async def _call_llm_turn(
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
    num_ctx: int = 8192,
) -> tuple[dict[str, Any], str]:
    """Call LLM for one diagnosis turn using the ACCUMULATED message history.

    INV_ONE_ALERT_ONE_SESSION: `messages` carries the full conversation (system
    + every prior user/assistant turn). Returns (parsed_response, raw_text) so
    the caller can append the assistant turn back into the history before the
    next iteration — keeping the session stateful across turns 2..8.
    """
    try:
        resp = await llm_client.chat(
            model=model,
            messages=messages,
            format="json",
            options={"num_ctx": num_ctx, "temperature": 0.1, "num_predict": 1024},
        )
        raw = _extract_raw_content(resp)
        return _parse_llm_response(raw), raw
    except Exception as exc:
        logger.error("[diag-loop] LLM turn failed: %s", exc)
        return (
            {
                "reasoning": f"LLM error: {exc}",
                "hypothesis": "llm_error",
                "commands_to_run": [],
                "diagnosis_complete": False,
                "confidence": 0.0,
            },
            "",
        )


async def run_diagnosis_loop(
    redis: Any,
    llm_client: Any,
    agent_id: str,
    ev_doc: dict[str, Any],
    trace_id: str,
    model: str = "qwen2.5-coder:7b",
    num_ctx: int = 8192,
) -> dict[str, Any]:
    """Run multi-turn diagnosis loop. Returns FinalDiagnosis dict.

    INVARIANT INV_NO_SINGLE_TURN: runs at least MIN_TURNS regardless of confidence.
    INVARIANT INV_DIAG_STORED: saves session to Redis before returning.
    """
    logger.info(
        "[diag-loop] START trace_id=%s agent_id=%s probe=%s",
        trace_id, agent_id, ev_doc.get("probe", ""),
    )

    vm_profile = await _load_vm_profile(redis, agent_id)
    turns: list[dict[str, Any]] = []
    final: dict[str, Any] = {}

    # Gate command dispatch on agent liveness. Enqueuing to an offline/unknown
    # agent_id means nobody polls the queue → every command times out (8×90s)
    # and the LLM gets fed empty results it misreads as host evidence.
    agent_online = await _agent_is_online(redis, agent_id)
    if not agent_online:
        logger.warning(
            "[diag-loop] agent OFFLINE trace=%s agent=%s — degraded facts-only diagnosis",
            trace_id, agent_id,
        )

    # Signatures (command + args) already dispatched — prevents the LLM from
    # re-requesting the same command across turns (rule 'do NOT re-request').
    executed_signatures: set[tuple[str, tuple[str, ...]]] = set()

    initial_context = _build_initial_context(vm_profile, ev_doc)
    if not agent_online:
        initial_context += (
            "\n\n[COMMAND EXECUTION UNAVAILABLE]\n"
            "The remote agent is OFFLINE — no diagnostic commands can be run. "
            "Diagnose ROOT CAUSE from the pre-collected facts above and conclude "
            "within 2 turns. Do NOT request commands; set commands_to_run=[]."
        )

    # INV_ONE_ALERT_ONE_SESSION: a SINGLE conversation per trace_id. The system
    # prompt + initial evidence are seeded once; each turn appends the assistant
    # reply and the next user message (new command results) so the LLM retains
    # every hypothesis it already explored or ruled out.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _DIAGNOSIS_SYSTEM_PROMPT},
        {"role": "user", "content": initial_context},
    ]

    # INV_DIAG_GROUNDED: everything the LLM may legitimately cite (facts, alert,
    # command outputs) accumulates here; the grounding gate checks conclusions
    # against it. The system prompt is deliberately EXCLUDED — its format
    # examples are exactly what must not be citable.
    evidence_corpus_parts: list[str] = [initial_context]

    for turn_n in range(1, _MAX_TURNS + 1):
        logger.info(
            "[diag-loop] turn=%d/%d trace=%s msg_history=%d",
            turn_n, _MAX_TURNS, trace_id, len(messages),
        )

        messages = _enforce_context_budget(messages, num_ctx)
        llm_resp, raw = await _call_llm_turn(llm_client, model, messages, num_ctx)
        # Append the assistant turn so the next call continues this same session.
        messages.append({"role": "assistant", "content": raw or json.dumps(llm_resp)})

        commands_requested = llm_resp.get("commands_to_run", [])
        command_results: list[dict[str, Any]] = []

        is_complete = bool(llm_resp.get("diagnosis_complete")) and turn_n >= _MIN_TURNS

        # Drop commands already run this session (dedup) — keeps the loop from
        # spinning on the same df/lsblk request when it yields nothing new.
        fresh_commands: list[dict[str, Any]] = []
        for c in commands_requested:
            sig = (c.get("command", ""), tuple(str(a) for a in c.get("args", [])))
            if sig in executed_signatures:
                continue
            fresh_commands.append(c)

        # Only dispatch to a LIVE agent, when not yet concluding, and only fresh cmds.
        has_commands = bool(fresh_commands) and not is_complete and agent_online

        if has_commands:
            cmd_ids = await _enqueue_commands(redis, agent_id, fresh_commands, trace_id)
            if cmd_ids:
                # cmd_ids are returned in the same order fresh_commands were enqueued.
                purpose_by_id = {
                    cid: fresh_commands[i].get("purpose", "")
                    for i, cid in enumerate(cmd_ids)
                    if i < len(fresh_commands)
                }
                for c in fresh_commands:
                    executed_signatures.add(
                        (c.get("command", ""), tuple(str(a) for a in c.get("args", [])))
                    )
                # Map the ACTUAL command string back onto each result so the
                # Telegram card can show "what was run" verbatim — the polled
                # result dict from the agent only carries stdout/stderr/rc.
                cmd_str_by_id = {
                    cid: _format_command(fresh_commands[i])
                    for i, cid in enumerate(cmd_ids)
                    if i < len(fresh_commands)
                }
                command_results = await _wait_for_results(redis, cmd_ids)
                for r in command_results:
                    cid = r.get("cmd_id", "")
                    r["purpose"] = purpose_by_id.get(cid, "")
                    r["command_str"] = cmd_str_by_id.get(cid, "")
                    if r.get("status") != "timeout":
                        evidence_corpus_parts.append(
                            f"{r.get('command_str', '')}\n{r.get('stdout', '')}\n{r.get('stderr', '')}"
                        )

        turn_record = {
            "turn": turn_n,
            "reasoning": llm_resp.get("reasoning", ""),
            "hypothesis": llm_resp.get("hypothesis", ""),
            "evidence_gaps": llm_resp.get("evidence_gaps", []),
            "confidence": llm_resp.get("confidence", 0.0),
            "commands_requested": commands_requested,
            "command_results": command_results,
            "diagnosis_complete_claimed": bool(llm_resp.get("diagnosis_complete")),
        }
        turns.append(turn_record)

        if is_complete:
            root_cause = llm_resp.get("root_cause") or llm_resp.get("hypothesis", "")
            final = _apply_grounding_gate(
                {
                    "root_cause": root_cause,
                    "affected_components": llm_resp.get("affected_components", []),
                    "blast_radius": llm_resp.get("blast_radius", ""),
                    "impact_summary": llm_resp.get("impact_summary", ""),
                    "remediation_steps": llm_resp.get("remediation_steps") or [],
                    "confidence": llm_resp.get("confidence", 0.0),
                    "suggested_recovery": _parse_suggested_recovery(
                        llm_resp.get("suggested_recovery")
                    ),
                },
                "\n".join(evidence_corpus_parts),
            )
            # Ensure remediation_steps is never empty when root cause is known
            if not final["remediation_steps"] and root_cause:
                final["remediation_steps"] = _fallback_remediation(root_cause)
            logger.info(
                "[diag-loop] COMPLETE turn=%d confidence=%.2f trace=%s",
                turn_n, final["confidence"], trace_id,
            )
            break

        # Offline agent: no commands will ever run. Once the minimum-turn floor is
        # met, stop spinning empty turns and finalize from the facts.
        if not agent_online and turn_n >= _MIN_TURNS:
            logger.info(
                "[diag-loop] agent offline — finalizing facts-only at turn=%d trace=%s",
                turn_n, trace_id,
            )
            break

        # Not complete → feed the new command results into the SAME session so
        # the next turn continues the conversation (INV_ONE_ALERT_ONE_SESSION).
        if turn_n < _MAX_TURNS:
            if llm_resp.get("_parse_error"):
                # Error-recovery contract: tell the model WHY the turn was wasted
                # and HOW to retry, instead of silently burning the turn.
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous reply was NOT valid JSON and was discarded. "
                        "Re-emit ONE complete JSON object exactly per OUTPUT FORMAT "
                        "(no markdown fences, no prose outside the JSON). "
                        f"Turn {turn_n + 1} of {_MAX_TURNS}."
                    ),
                })
            else:
                messages.append({
                    "role": "user",
                    "content": _build_followup_context(command_results, turn_n + 1),
                })

    if not final:
        last = turns[-1] if turns else {}
        hypothesis = last.get("hypothesis", "")
        final = _apply_grounding_gate(
            {
                "root_cause": hypothesis or "Diagnosis inconclusive after max turns — see hypothesis per turn",
                "affected_components": [],
                "blast_radius": "",
                "impact_summary": "Diagnosis reached maximum turns. Best-effort root cause from available evidence.",
                "remediation_steps": [],
                "confidence": last.get("confidence", 0.0),
            },
            "\n".join(evidence_corpus_parts),
        )
        final["remediation_steps"] = _fallback_remediation(hypothesis)
        logger.warning("[diag-loop] max_turns reached trace=%s", trace_id)

    session = {
        "trace_id": trace_id,
        "agent_id": agent_id,
        "probe": ev_doc.get("probe", ""),
        "lane": ev_doc.get("lane", ""),
        "alert_hint": ev_doc.get("alert_hint", ""),
        "turns": turns,
        "total_turns": len(turns),
        "final": final,
        "degraded": not agent_online,
        "degraded_reason": "" if agent_online else "agent_offline: no command execution available",
        "completed_at": int(time.time()),
    }

    # INVARIANT INV_DIAG_STORED: persist before any Telegram emit
    try:
        key = f"{_SESSION_KEY_PREFIX}{trace_id}"
        await redis.set(key, json.dumps(session, ensure_ascii=False), ex=_SESSION_TTL)
        logger.info("[diag-loop] session stored key=%s", key)
    except Exception as exc:
        logger.error("[diag-loop] session store FAILED trace=%s err=%s — ABORTING emit", trace_id, exc)
        raise RuntimeError(f"INV_DIAG_STORED violated: {exc}") from exc

    return session
