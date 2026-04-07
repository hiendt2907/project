"""Autonomous Decider: Stateful ReAct (Thought → Action → Observation) hoặc legacy one-shot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
from workers.env_mode import is_dev_mode
from workers.observation_sanitize import sanitize_for_llm
from workers.react_logging import log_react_json
from workers.tool_observation import prepare_observation_for_llm, prepare_tool_return_for_llm
from workers.tool_registry import get_tool_registry
from workers.tools import TOOL_REGISTRY, ToolCallPayload

logger = logging.getLogger(__name__)

REDIS_KEY_COOLDOWN_PREFIX = "omni:autonomous_fix:cooldown:"
REDIS_REACT_STATE_PREFIX = "omni:autonomous:react_state:"
_AUTONOMOUS_DECIDER_REASON_MAX = 1800

_MUTATING_TOOLS = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "sandbox_cleanup",
        "execute_in_sandbox",
        "gated_allowlisted_execute",
    }
)


def _fingerprint(manifest: dict[str, Any]) -> str:
    dr = manifest.get("dr")
    evt = manifest.get("evt") or []
    zc = manifest.get("z_cpu")
    zm = manifest.get("z_mem")
    evt_s = json.dumps(evt, sort_keys=True, ensure_ascii=False)[:2000]
    payload = json.dumps(
        {"dr": dr, "evt": evt_s, "zc": zc, "zm": zm},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _parse_csv_set(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def _parse_tool_payload(content: str) -> ToolCallPayload:
    s = content.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    data = json.loads(s)
    return ToolCallPayload.model_validate(data)


def _strip_markdown_json(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:])
    return s.strip()


def _parse_react_turn(content: str) -> tuple[str, str, bool, dict[str, Any] | None] | None:
    """Trả về (thought, reasoning_path, is_clear, tool_call_dict) hoặc None nếu không parse ReAct JSON."""
    raw = _strip_markdown_json(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    thought = str(data.get("thought") or "")
    rp = str(data.get("reasoning_path") or "v3_react_thought")
    action = data.get("action")
    if action == "CLEAR" or (isinstance(action, str) and action.strip().upper() == "CLEAR"):
        return thought, rp, True, None
    if isinstance(action, dict) and action.get("tool"):
        return thought, rp, False, {"tool": str(action["tool"]), "args": dict(action.get("args") or {})}
    return None


def _is_clear(content: str) -> bool:
    line = (content.strip().splitlines() or [""])[0].strip().upper()
    return line == "CLEAR" or line.startswith("CLEAR ")


def _sigma_hint(manifest: dict[str, Any]) -> str:
    if not manifest.get("dr"):
        return ""
    zc = manifest.get("z_cpu")
    zm = manifest.get("z_mem")
    try:
        azc = abs(float(zc)) if zc is not None else None
        azm = abs(float(zm)) if zm is not None else None
    except (TypeError, ValueError):
        return "Statistical Anomaly Detected (out of 3-Sigma range). "
    parts: list[str] = ["Statistical Anomaly Detected (out of 3-Sigma range)."]
    if azc is not None and azm is not None:
        if azc >= azm:
            parts.append(f"Strongest |z| is CPU (z_cpu={zc}).")
        else:
            parts.append(f"Strongest |z| is memory (z_mem={zm}).")
    elif azc is not None:
        parts.append(f"z_cpu={zc}.")
    elif azm is not None:
        parts.append(f"z_mem={zm}.")
    return " ".join(parts)


def _build_user_prompt(manifest: dict[str, Any], dr: bool, evt_list: list[Any]) -> str:
    head = _sigma_hint(manifest) if dr else ""
    if evt_list and not head:
        head = "Kubernetes Warning events present. "
    elif evt_list and head:
        head += " Also check evt list. "
    body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    return (head + "Manifest:\n" + body)[:12000]


def _system_prompt(safe_tools: set[str], allowed_ns: set[str]) -> str:
    return (
        "You are an autonomous SRE decider. Given a System Health Manifest JSON, "
        "reply with exactly one of:\n"
        "1) CLEAR — no remediation.\n"
        "2) A single JSON object: {\"tool\":\"<name>\",\"args\":{...}} — one tool only, no markdown.\n"
        f"Allowed tools: {', '.join(sorted(safe_tools))}. "
        f"For k8s_rollout_restart, set args.namespace to one of: {', '.join(sorted(allowed_ns))}. "
        "Prefer CLEAR when uncertain."
    )


def _system_prompt_react(safe_tools: set[str], allowed_ns: set[str], schema_snippet: str) -> str:
    tools_csv = ", ".join(sorted(safe_tools))
    ns_csv = ", ".join(sorted(allowed_ns))
    return (
        "You are an autonomous SRE decider (ReAct). Output exactly ONE JSON object per message, no markdown fences.\n"
        "Schema: JSON with thought, reasoning_path (v3_react_thought), action CLEAR or object tool+args.\n"
        f"Allowed tools: {tools_csv}.\n"
        f"k8s_rollout_restart: args.namespace must be one of: {ns_csv}.\n"
        "If uncertain, use action CLEAR.\n"
        f"Tool JSON Schemas (subset):\n{schema_snippet[:6000]}"
    )


def _schemas_for_safe_tools(safe: set[str]) -> str:
    reg = get_tool_registry()
    parts: list[str] = []
    for name in sorted(safe):
        if reg.has(name):
            try:
                parts.append(f"{name}: {json.dumps(reg.json_schema_for(name), ensure_ascii=False)[:1500]}")
            except Exception as e:
                logger.debug("schema for %s: %s", name, e)
    return "\n".join(parts) if parts else "(no typed tools in allowlist)"


def _validate_k8s_ns(ws: Any, call: ToolCallPayload, allowed_ns: set[str]) -> bool:
    if is_dev_mode(ws):
        return True
    if call.tool != "k8s_rollout_restart":
        return True
    ns = str(call.args.get("namespace") or "").strip()
    if not ns:
        ns = (getattr(ws, "k8s_default_namespace", None) or "multi-agent").strip()
    return ns in allowed_ns


def _react_state_key(fp: str) -> str:
    return REDIS_REACT_STATE_PREFIX + fp


async def _save_react_state(
    redis: Any,
    fp: str,
    *,
    turn: int,
    last_tool: str,
    observation_masked: str,
    ttl_sec: int,
) -> None:
    payload = {
        "turn": turn,
        "last_tool": last_tool,
        "obs": observation_masked[:2000],
        "ts": time.time(),
    }
    try:
        await redis.setex(_react_state_key(fp), ttl_sec, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.debug("react_state set: %s", e)


async def _load_prior_react_state(redis: Any, fp: str) -> str | None:
    """P0: tick kế đọc state trước — đưa vào prompt để tránh lặp rollout vô ích."""
    try:
        raw = await redis.get(_react_state_key(fp))
        if not raw:
            return None
        s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        data = json.loads(s)
        if not isinstance(data, dict):
            return None
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:2500]
    except Exception as e:
        logger.debug("react_state get: %s", e)
        return None


def _args_fingerprint(args: dict[str, Any]) -> str:
    try:
        return hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode("utf-8"),
        ).hexdigest()[:16]
    except Exception:
        return "na"


async def _tick_legacy(ctx: Any, ws: Any, model: str, cooldown_sec: int) -> None:
    raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
    if not raw:
        return
    s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    try:
        manifest = json.loads(s)
    except json.JSONDecodeError:
        return
    if not isinstance(manifest, dict):
        return

    dr = bool(manifest.get("dr"))
    evt = manifest.get("evt")
    evt_list = evt if isinstance(evt, list) else []
    if not dr and len(evt_list) == 0:
        return

    fp = _fingerprint(manifest)
    ckey = REDIS_KEY_COOLDOWN_PREFIX + fp

    if bool(manifest.get("remediation_silent")):
        logger.info(
            "[REMEDIATION_SILENT_SKIP] fp=%s dr=%s evt_n=%s",
            fp,
            dr,
            len(evt_list),
        )
        try:
            await ctx.redis.setex(ckey, cooldown_sec, "1")
        except Exception as e:
            logger.debug("autonomous_decider silent cooldown set: %s", e)
        return

    try:
        if await ctx.redis.get(ckey):
            logger.info("[COOLDOWN_SKIP] fp=%s", fp)
            return
    except Exception as e:
        logger.debug("autonomous_decider cooldown get: %s", e)

    safe_tools = _parse_csv_set(getattr(ws, "autonomous_safe_tools", ""))
    allowed_ns = _parse_csv_set(getattr(ws, "autonomous_allowed_namespaces", "multi-agent"))
    if not safe_tools:
        logger.warning("autonomous_decider: autonomous_safe_tools empty")
        return

    user_prompt = _build_user_prompt(manifest, dr, evt_list)
    sys_prompt = _system_prompt(safe_tools, allowed_ns)

    prev_proactive = getattr(ctx, "inbound_proactive", False)
    prev_trace = getattr(ctx, "inbound_trace_id", "")
    token = await ctx.semaphore.acquire_proactive()
    try:
        ctx.inbound_proactive = True
        ctx.inbound_trace_id = f"autonomous-decider-{fp}"
        resp = await ctx.ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.1, "num_ctx": 4096},
            keep_alive=ws.ollama_keep_alive,
        )
    finally:
        await ctx.semaphore.release(token)
        ctx.inbound_proactive = prev_proactive
        ctx.inbound_trace_id = prev_trace

    content = ((resp.get("message") or {}).get("content") or "").strip()
    if not content:
        logger.info("[AUTONOMOUS_FIX] empty model output fp=%s", fp)
        return

    _reason_one_line = sanitize_for_llm(content)[:_AUTONOMOUS_DECIDER_REASON_MAX].replace("\n", " ").replace("\r", " ")
    logger.info("[AUTONOMOUS_DECIDER_REASON] fp=%s content=%s", fp, _reason_one_line)

    if _is_clear(content):
        logger.info("[AUTONOMOUS_FIX] CLEAR fp=%s", fp)
        try:
            await ctx.redis.setex(ckey, cooldown_sec, "1")
        except Exception as e:
            logger.debug("autonomous_decider cooldown set: %s", e)
        return

    try:
        call = _parse_tool_payload(content)
    except Exception as e:
        logger.warning("[AUTONOMOUS_FIX] parse JSON failed fp=%s err=%s", fp, e)
        return

    if call.tool not in safe_tools:
        logger.info("[AUTONOMOUS_FIX] denied tool=%s not in allowlist fp=%s", call.tool, fp)
        return

    if not _validate_k8s_ns(ws, call, allowed_ns):
        logger.info("[AUTONOMOUS_FIX] denied k8s namespace fp=%s args=%s", fp, call.args)
        return

    fn = TOOL_REGISTRY.get(call.tool)
    if not fn:
        logger.info("[AUTONOMOUS_FIX] unknown registry tool=%s", call.tool)
        return

    ctx.inbound_proactive = True
    ctx.inbound_trace_id = f"autonomous-decider-exec-{fp}"
    ctx.inbound_reasoning = _reason_one_line  # V6.3: Audit rationale
    try:
        out = await fn(ctx, dict(call.args))
        logger.info("[AUTONOMOUS_FIX] tool=%s fp=%s out_len=%s", call.tool, fp, len(out or ""))
    except Exception as e:
        logger.exception("[AUTONOMOUS_FIX] tool=%s fp=%s err=%s", call.tool, fp, e)
        out = f"error: {e!s}"
    finally:
        ctx.inbound_proactive = prev_proactive
        ctx.inbound_trace_id = prev_trace
        ctx.inbound_reasoning = None

    cid = getattr(ws, "telegram_admin_chat_id", None)
    tg = getattr(ctx, "telegram", None)
    if tg is not None and cid is not None:
        try:
            safe = prepare_tool_return_for_llm(ctx, out or "", max_chars=3500)
            msg = f"[AUTONOMOUS_FIX] tool={call.tool} fp={fp}\n{safe}"
            await tg.send_message(int(cid), msg[:3900])
        except Exception as e:
            logger.warning("[AUTONOMOUS_FIX] telegram: %s", e)

    try:
        await ctx.redis.setex(ckey, cooldown_sec, "1")
    except Exception as e:
        logger.debug("autonomous_decider cooldown set after tool: %s", e)


async def _run_tool(ctx: Any, tool: str, args: dict[str, Any]) -> str:
    reg = get_tool_registry()
    if reg.has(tool):
        return await reg.invoke(ctx, tool, args)
    fn = TOOL_REGISTRY.get(tool)
    if fn:
        raw = await fn(ctx, args)
        return prepare_tool_return_for_llm(ctx, raw or "")
    return f"[ERROR] unknown tool: {tool}"


async def _tick_react(ctx: Any, ws: Any, model: str, cooldown_sec: int) -> None:
    raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
    if not raw:
        return
    s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    try:
        manifest = json.loads(s)
    except json.JSONDecodeError:
        return
    if not isinstance(manifest, dict):
        return

    dr = bool(manifest.get("dr"))
    evt = manifest.get("evt")
    evt_list = evt if isinstance(evt, list) else []
    if not dr and len(evt_list) == 0:
        return

    fp = _fingerprint(manifest)
    ckey = REDIS_KEY_COOLDOWN_PREFIX + fp

    if bool(manifest.get("remediation_silent")):
        logger.info(
            "[REMEDIATION_SILENT_SKIP] fp=%s dr=%s evt_n=%s",
            fp,
            dr,
            len(evt_list),
        )
        try:
            await ctx.redis.setex(ckey, cooldown_sec, "1")
        except Exception as e:
            logger.debug("autonomous_decider silent cooldown set: %s", e)
        return

    try:
        if await ctx.redis.get(ckey):
            logger.info("[COOLDOWN_SKIP] fp=%s", fp)
            return
    except Exception as e:
        logger.debug("autonomous_decider cooldown get: %s", e)

    safe_tools = _parse_csv_set(getattr(ws, "autonomous_safe_tools", ""))
    allowed_ns = _parse_csv_set(getattr(ws, "autonomous_allowed_namespaces", "multi-agent"))
    if not safe_tools:
        logger.warning("autonomous_decider: autonomous_safe_tools empty")
        return

    max_turns = int(getattr(ws, "react_max_turns", 4) or 4)
    obs_max = int(getattr(ws, "react_observation_max_chars", 1200) or 1200)
    state_ttl = int(getattr(ws, "react_state_redis_ttl_sec", 0) or 0) or max(cooldown_sec * 2, 1200)

    user_base = _build_user_prompt(manifest, dr, evt_list)
    prior = await _load_prior_react_state(ctx.redis, fp)
    if prior:
        user_base = f"{user_base}\n\nPrior ReAct state (Redis, same fp):\n{prior}"
    schema_snippet = _schemas_for_safe_tools(safe_tools)
    sys_prompt = _system_prompt_react(safe_tools, allowed_ns, schema_snippet)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_base},
    ]

    prev_proactive = getattr(ctx, "inbound_proactive", False)
    prev_trace = getattr(ctx, "inbound_trace_id", "")
    resolved = False

    async def _exec_tool_turn(tname: str, targs: dict[str, Any], reasoning: str | None = None) -> str:
        if tname not in safe_tools:
            logger.info("[AUTONOMOUS_FIX] denied tool=%s fp=%s", tname, fp)
            return f"[DENIED] tool {tname} not in allowlist."
        if tname == "k8s_rollout_restart":
            tc = ToolCallPayload(tool=tname, args=targs)
            if not _validate_k8s_ns(ws, tc, allowed_ns):
                return "[DENIED] namespace not allowed for k8s_rollout_restart."
        afp = _args_fingerprint(targs)
        ctx.inbound_proactive = True
        ctx.inbound_trace_id = f"autonomous-decider-exec-{fp}"
        ctx.inbound_reasoning = reasoning or "ReAct autonomous turn"
        try:
            out = await _run_tool(ctx, tname, targs)
            log_react_json(
                "v3_tool_execute",
                fp=fp,
                tool=tname,
                args_hash=afp,
                out_len=len(out or ""),
            )
            return out
        except Exception as e:
            logger.exception("[AUTONOMOUS_FIX] tool=%s fp=%s", tname, fp)
            log_react_json(
                "v3_tool_execute",
                fp=fp,
                tool=tname,
                args_hash=afp,
                error=str(e)[:400],
            )
            return f"error: {e!s}"
        finally:
            ctx.inbound_proactive = prev_proactive
            ctx.inbound_trace_id = prev_trace
            ctx.inbound_reasoning = None

    for turn in range(1, max_turns + 1):
        token = await ctx.semaphore.acquire_proactive()
        try:
            ctx.inbound_proactive = True
            ctx.inbound_trace_id = f"autonomous-decider-react-{fp}-t{turn}"
            resp = await ctx.ollama.chat(
                model=model,
                messages=messages,
                options={"temperature": 0.1, "num_ctx": 4096},
                keep_alive=ws.ollama_keep_alive,
            )
        finally:
            await ctx.semaphore.release(token)
            ctx.inbound_proactive = prev_proactive
            ctx.inbound_trace_id = prev_trace

        content = ((resp.get("message") or {}).get("content") or "").strip()
        if not content:
            logger.info("[AUTONOMOUS_FIX] empty model output fp=%s turn=%s", fp, turn)
            break

        parsed = _parse_react_turn(content)
        if parsed is not None:
            thought, rp, is_clear, tool_call = parsed
            log_react_json(
                rp,
                fp=fp,
                turn=turn,
                thought=thought[:1500],
                phase="thought",
            )
            _line = sanitize_for_llm(content)[:_AUTONOMOUS_DECIDER_REASON_MAX].replace("\n", " ")
            logger.info("[AUTONOMOUS_DECIDER_REASON] fp=%s turn=%s content=%s", fp, turn, _line)

            if is_clear:
                resolved = True
                logger.info("[AUTONOMOUS_FIX] CLEAR fp=%s", fp)
                break

            if not tool_call:
                break

            tname = tool_call["tool"]
            targs = tool_call["args"]
            obs = await _exec_tool_turn(tname, targs, reasoning=thought)
            obs_final = prepare_observation_for_llm(obs, obs_max)
            log_react_json(
                "v3_react_observation",
                fp=fp,
                turn=turn,
                tool=tname,
                obs_len=len(obs_final),
            )
            await _save_react_state(
                ctx.redis,
                fp,
                turn=turn,
                last_tool=tname,
                observation_masked=obs_final,
                ttl_sec=state_ttl,
            )
            messages.append({"role": "assistant", "content": content[:8000]})
            messages.append({"role": "user", "content": f"Observation (turn {turn}):\n{obs_final}"})
            continue

        if _is_clear(content):
            resolved = True
            logger.info("[AUTONOMOUS_FIX] CLEAR legacy fp=%s", fp)
            break

        try:
            call = _parse_tool_payload(content)
        except Exception:
            logger.warning("[AUTONOMOUS_REACT] parse failed fp=%s turn=%s", fp, turn)
            messages.append({"role": "assistant", "content": content[:4000]})
            messages.append(
                {
                    "role": "user",
                    "content": "Invalid format. Reply with one JSON: "
                    '{"thought":"...","reasoning_path":"v3_react_thought","action":"CLEAR"} '
                    'or action object with tool and args.',
                }
            )
            continue

        tname = call.tool
        targs = dict(call.args)
        obs = await _exec_tool_turn(tname, targs)
        obs_final = prepare_observation_for_llm(obs, obs_max)
        log_react_json(
            "v3_react_observation",
            fp=fp,
            turn=turn,
            tool=tname,
            obs_len=len(obs_final),
        )
        await _save_react_state(
            ctx.redis,
            fp,
            turn=turn,
            last_tool=tname,
            observation_masked=obs_final,
            ttl_sec=state_ttl,
        )
        messages.append({"role": "assistant", "content": content[:8000]})
        messages.append({"role": "user", "content": f"Observation (turn {turn}):\n{obs_final}"})

    if not resolved:
        logger.error(
            "[REACT_ABORTED] AI stuck in reasoning loop fp=%s max_turns=%s",
            fp,
            max_turns,
        )
        log_react_json(
            "v3_react_aborted",
            fp=fp,
            reason="max_turns_exceeded",
            max_turns=max_turns,
        )
        cid = getattr(ws, "telegram_admin_chat_id", None)
        tg = getattr(ctx, "telegram", None)
        if tg is not None and cid is not None:
            try:
                await tg.send_message(
                    int(cid),
                    f"[REACT_ABORTED] AI stuck in reasoning loop fp={fp} max_turns={max_turns}",
                )
            except Exception as e:
                logger.warning("[REACT_ABORTED] telegram: %s", e)

    try:
        await ctx.redis.setex(ckey, cooldown_sec, "1")
    except Exception as e:
        logger.debug("autonomous_decider cooldown set: %s", e)


async def _tick(ctx: Any, ws: Any, model: str, cooldown_sec: int) -> None:
    if getattr(ws, "autonomous_react_enabled", True):
        await _tick_react(ctx, ws, model, cooldown_sec)
    else:
        await _tick_legacy(ctx, ws, model, cooldown_sec)


async def autonomous_decider_loop(ctx: Any, stop: asyncio.Event) -> None:
    ws = ctx.settings
    if not getattr(ws, "autonomous_decider_enabled", False):
        logger.info("autonomous_decider_loop disabled")
        return
    await ctx.scout_ready.wait()
    interval = float(getattr(ws, "autonomous_decider_interval_sec", 300) or 300)
    cooldown_sec = int(getattr(ws, "autonomous_fix_cooldown_sec", 600) or 600)
    model = (getattr(ws, "autonomous_decider_model", None) or "").strip()
    if not model:
        model = getattr(ws, "model_reasoning_engine", "deepseek-r1:8b")

    logger.info(
        "autonomous_decider_loop start interval_sec=%s cooldown_sec=%s model=%s react=%s",
        interval,
        cooldown_sec,
        model,
        getattr(ws, "autonomous_react_enabled", True),
    )

    while not stop.is_set():
        try:
            await _tick(ctx, ws, model, cooldown_sec)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("autonomous_decider_loop tick: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
