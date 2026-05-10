#!/usr/bin/env python3
"""
Assert advisory via Telegram Bot HTTP API — P0 E2E evidence.

1) Prefer getUpdates when Telegram delivers bot-sent messages to the update stream
   (often false for supergroups — Bot API omits the bot's own outgoing messages).

2) Fallback (default on): read structured log ``event=telegram_outbound_ok`` from
   omni-analyst, then call deleteMessage — proves the message_id existed (lab-only;
   removes the advisory message from chat).

Reads TELEGRAM_BOT_TOKEN from env (same as worker).

Usage:
  export TELEGRAM_BOT_TOKEN=...
  export OMNI_TELEGRAM_ADMIN_CHAT_ID=...   # optional filter for getUpdates
  python3 scripts/e2e_telegram_bot_api_assert.py <trace_id>

Env:
  E2E_TELEGRAM_POLL_SEC     getUpdates poll budget (default 90)
  E2E_TELEGRAM_POLL_INTERVAL Sleep between getUpdates (default 4)
  E2E_TELEGRAM_VERIFY_DELETE_MESSAGE  1 (default) = allow deleteMessage delivery proof after logs
  E2E_TELEGRAM_STRICT_GETUPDATES 1 = require advisory visible in getUpdates; no deleteMessage; 409 Conflict → exit 10
  E2E_KUBE_NS / NS — required for kubectl analyst logs (no default)
  REPO root uses scripts/with_working_kube.sh for kubectl.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _updates_url(token: str, base: str, *, offset: int | None, timeout: int) -> str:
    b = base.rstrip("/") + f"/bot{token}/getUpdates"
    q: dict[str, str | int] = {"timeout": timeout, "limit": 100}
    if offset is not None:
        q["offset"] = offset
    return b + "?" + urllib.parse.urlencode(q)


def _http_get_json(url: str, timeout_sec: float = 65.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode())


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_outbound_from_logs(trace: str, log_blob: str) -> tuple[int, int] | None:
    """Return (chat_id, message_id) from worker JSON log lines."""
    for line in log_blob.splitlines():
        if trace not in line or "telegram_outbound_ok" not in line:
            continue
        mid_m = re.search(r"message_id=(\d+)", line)
        cid_m = re.search(r"chat_id=(-?\d+)", line)
        if not mid_m or not cid_m:
            continue
        return int(cid_m.group(1)), int(mid_m.group(1))
    return None


def _kubectl_analyst_logs(trace: str) -> str | None:
    ns = (os.environ.get("E2E_KUBE_NS") or os.environ.get("NS") or "").strip()
    if not ns:
        print(
            "ERROR: set NS or E2E_KUBE_NS for kubectl logs target namespace",
            file=sys.stderr,
        )
        sys.exit(2)
    dep = os.environ.get("E2E_ANALYST_DEPLOY", "omni-analyst")
    kube = os.path.join(_repo_root(), "scripts/with_working_kube.sh")
    try:
        return subprocess.check_output(
            [kube, "kubectl", "logs", "-n", ns, f"deploy/{dep}", "--since=25m", "--tail=12000"],
            text=True,
            timeout=120,
        )
    except Exception as e:
        print(f"WARN: kubectl logs analyst: {e!r}", file=sys.stderr)
        return None


def _telegram_post_json(token: str, api_base: str, method: str, payload: dict[str, object]) -> dict:
    u = f"{api_base.rstrip('/')}/bot{token}/{method}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        u,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode())


def _verify_via_delete_message(
    token: str,
    api_base: str,
    trace: str,
) -> bool:
    logs = _kubectl_analyst_logs(trace)
    if not logs:
        return False
    parsed = _parse_outbound_from_logs(trace, logs)
    if parsed is None:
        print(
            json.dumps(
                {
                    "event": "e2e_telegram_delete_verify_no_log",
                    "trace_id": trace,
                    "hint": "expected event=telegram_outbound_ok message_id= in omni-analyst logs",
                },
                default=str,
            ),
            file=sys.stderr,
        )
        return False
    chat_id, message_id = parsed
    try:
        d = _telegram_post_json(
            token,
            api_base,
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if e.fp else ""
        print(
            json.dumps(
                {
                    "event": "e2e_telegram_delete_http_error",
                    "trace_id": trace,
                    "code": e.code,
                    "body": body[:400],
                },
                default=str,
            ),
            file=sys.stderr,
        )
        return False
    if not d.get("ok"):
        print(json.dumps({"event": "e2e_telegram_delete_not_ok", "trace_id": trace, "resp": d}, default=str), file=sys.stderr)
        return False
    print(
        json.dumps(
            {
                "event": "e2e_telegram_bot_api_assert_pass",
                "trace_id": trace,
                "mode": "deleteMessage",
                "chat_id": chat_id,
                "message_id": message_id,
            },
            default=str,
        )
    )
    return True


def _env_bool(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return default


def _extract_texts_from_result(result: list[dict]) -> list[tuple[int, int, str]]:
    """Return list of (chat_id, message_id, text) from updates."""
    out: list[tuple[int, int, str]] = []
    for u in result:
        msg = u.get("message") or u.get("channel_post") or u.get("edited_message")
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat") or {}
        cid = int(chat.get("id", 0))
        mid = int(msg.get("message_id", 0))
        text = msg.get("text") or msg.get("caption") or ""
        if isinstance(text, str) and text.strip():
            out.append((cid, mid, text))
    return out


def _match(
    trace: str,
    texts: list[tuple[int, int, str]],
    *,
    expect_chat: int | None,
) -> tuple[int, int, str] | None:
    needle_trace = trace.strip()
    for cid, mid, text in texts:
        if expect_chat is not None and cid != expect_chat:
            continue
        if needle_trace not in text:
            continue
        # Rubric: advisory template from telegram_advisory_emitter
        if "*VERDICT:*" not in text and "VERDICT" not in text:
            continue
        if len(text) < 80:
            continue
        return (cid, mid, text)
    return None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    trace = sys.argv[1].strip()
    if not trace:
        print("trace_id empty", file=sys.stderr)
        return 2

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("FAIL: TELEGRAM_BOT_TOKEN unset", file=sys.stderr)
        return 3

    base = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").strip()
    poll_sec = float(os.environ.get("E2E_TELEGRAM_POLL_SEC", "90"))
    interval = float(os.environ.get("E2E_TELEGRAM_POLL_INTERVAL", "4"))
    expect_chat_raw = (os.environ.get("E2E_TELEGRAM_EXPECT_CHAT_ID") or "").strip()
    admin_raw = (os.environ.get("OMNI_TELEGRAM_ADMIN_CHAT_ID") or "").strip()
    expect_chat: int | None = None
    if expect_chat_raw:
        expect_chat = int(expect_chat_raw)
    elif admin_raw:
        try:
            expect_chat = int(admin_raw)
        except ValueError:
            pass

    strict = _env_bool("E2E_TELEGRAM_STRICT_GETUPDATES", False)
    deadline = time.monotonic() + poll_sec
    next_offset: int | None = None
    skip_getupdates = False
    # Drain stale updates once so offset advances (short timeout).
    try:
        drain_url = _updates_url(token, base, offset=None, timeout=1)
        d0 = _http_get_json(drain_url, timeout_sec=30.0)
        if d0.get("ok") and isinstance(d0.get("result"), list) and d0["result"]:
            last = max(int(u.get("update_id", 0)) for u in d0["result"])
            next_offset = last + 1
    except urllib.error.HTTPError as e:
        if e.code == 409:
            if strict:
                print(
                    json.dumps(
                        {
                            "event": "e2e_telegram_assert_fail",
                            "reason": "getupdates_409_conflict",
                            "detail": "Another client holds getUpdates; set OMNI_TELEGRAM_POLLING_ENABLED=false on in-cluster workers or unset E2E_TELEGRAM_STRICT_GETUPDATES.",
                            "trace_id": trace,
                        },
                        default=str,
                    ),
                    file=sys.stderr,
                )
                return 10
            skip_getupdates = True
            print(
                json.dumps(
                    {
                        "event": "e2e_telegram_getupdates_unavailable",
                        "reason": "http_409_conflict",
                        "detail": "Another client holds getUpdates long-poll; using deleteMessage delivery proof.",
                    },
                    default=str,
                ),
                file=sys.stderr,
            )
        else:
            body = e.read().decode(errors="replace") if e.fp else ""
            print(
                f"WARN: initial getUpdates drain failed: HTTP {e.code}: {body[:200]!r}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"WARN: initial getUpdates drain failed: {e!r}", file=sys.stderr)

    print(
        json.dumps(
            {
                "event": "e2e_telegram_bot_api_assert_start",
                "trace_id": trace,
                "poll_sec": poll_sec,
                "expect_chat_id": expect_chat,
                "skip_getupdates": skip_getupdates,
                "strict_getupdates": strict,
            },
            default=str,
        )
    )

    while time.monotonic() < deadline and not skip_getupdates:
        try:
            url = _updates_url(token, base, offset=next_offset, timeout=min(25, int(deadline - time.monotonic()) or 1))
            data = _http_get_json(url, timeout_sec=70.0)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace") if e.fp else ""
            if e.code == 409:
                if strict:
                    print(
                        json.dumps(
                            {
                                "event": "e2e_telegram_assert_fail",
                                "reason": "getupdates_409_conflict",
                                "trace_id": trace,
                            },
                            default=str,
                        ),
                        file=sys.stderr,
                    )
                    return 10
                print(
                    json.dumps(
                        {
                            "event": "e2e_telegram_getupdates_unavailable",
                            "reason": "http_409_conflict",
                            "detail": "Switching to deleteMessage delivery proof.",
                        },
                        default=str,
                    ),
                    file=sys.stderr,
                )
                skip_getupdates = True
                break
            print(f"FAIL: HTTP {e.code} getUpdates: {body[:500]}", file=sys.stderr)
            return 4
        except Exception as e:
            print(f"WARN: getUpdates error {e!r}", file=sys.stderr)
            time.sleep(interval)
            continue

        if not data.get("ok"):
            print(f"FAIL: getUpdates not ok: {json.dumps(data)[:800]}", file=sys.stderr)
            return 5

        result = data.get("result") or []
        if isinstance(result, list) and result:
            last = max(int(u.get("update_id", 0)) for u in result)
            next_offset = last + 1

        texts = _extract_texts_from_result(result if isinstance(result, list) else [])
        hit = _match(trace, texts, expect_chat=expect_chat)
        if hit is not None:
            cid, mid, text = hit
            preview = text[:500] + ("…" if len(text) > 500 else "")
            print(
                json.dumps(
                    {
                        "event": "e2e_telegram_bot_api_assert_pass",
                        "trace_id": trace,
                        "chat_id": cid,
                        "message_id": mid,
                        "text_preview": preview,
                    },
                    default=str,
                )
            )
            return 0

        time.sleep(interval)

    verify_del = _env_bool("E2E_TELEGRAM_VERIFY_DELETE_MESSAGE", True)
    if verify_del and (not strict) and _verify_via_delete_message(token, base, trace):
        return 0

    if strict:
        print(
            json.dumps(
                {
                    "event": "e2e_telegram_assert_fail",
                    "reason": "strict_getupdates_no_match",
                    "trace_id": trace,
                    "detail": "Advisory not observed in getUpdates within poll budget (supergroups often omit bot-sent messages).",
                },
                default=str,
            ),
            file=sys.stderr,
        )
        return 11

    print(
        json.dumps(
            {
                "event": "e2e_telegram_bot_api_assert_fail",
                "trace_id": trace,
                "hint": "getUpdates missed (expected for many supergroups); deleteMessage fallback failed or disabled",
            },
            default=str,
        ),
        file=sys.stderr,
    )
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
