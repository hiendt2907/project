"""OpenSandbox — httpx client + unified Redis audit stream `audit:sandbox`. Policy denylist at gate."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from execution.policy import PolicyVerdict, check_sandbox_command
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

ACTIVE_SET_KEY = "sandbox:active_ids"


@dataclass
class SandboxExecResult:
    trace_id: str
    session_id: str
    command: str
    run_id: str
    exit_code: int
    stdout: str
    stderr: str
    http_status: int | None
    policy_verdict: str
    policy_reason: str = ""
    command_truncated: bool = False
    raw_command_preview: str = ""


class SandboxManager:
    """Gọi HTTP API OpenSandbox — audit mọi lệnh vào một Redis Stream."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._s = settings

    @property
    def enabled(self) -> bool:
        return bool(self._s.opensandbox_enabled)

    def _url(self) -> str:
        return self._s.opensandbox_base_url.strip().rstrip("/")

    async def health_check(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "OpenSandbox disabled (OMNI_OPENSANDBOX_ENABLED=false)"
        try:
            async with httpx.AsyncClient(timeout=min(10.0, self._s.opensandbox_timeout_s)) as hc:
                for path in ("/health", "/api/health", "/"):
                    try:
                        r = await hc.get(f"{self._url()}{path}")
                        if r.status_code < 500:
                            return True, f"GET {path} status={r.status_code}"
                    except Exception:
                        continue
            return False, "No health endpoint responded"
        except Exception as e:
            return False, str(e)

    def _command_for_audit(self, cmd: str) -> tuple[str, bool]:
        c = cmd[:2000] if len(cmd) <= 2000 else cmd[:1997] + "..."
        truncated = len(cmd) > 2000
        return c, truncated

    async def _audit_sandbox(
        self,
        redis: Any,
        *,
        trace_id: str,
        session_id: str,
        command: str,
        run_id: str,
        policy_verdict: str,
        policy_reason: str = "",
        http_status: int | None = None,
        exit_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        cmd_show, trunc = self._command_for_audit(command)
        body: dict[str, Any] = {
            "ts": time.time(),
            "trace_id": trace_id,
            "session_id": session_id,
            "run_id": run_id,
            "command": cmd_show,
            "command_truncated": trunc,
            "policy_result": policy_verdict,
            "policy_reason": policy_reason,
            "http_status": http_status,
            "exit_code": exit_code,
        }
        if extra:
            body.update(extra)
        try:
            await redis.xadd(
                self._s.audit_sandbox_stream,
                {"data": json.dumps(body, ensure_ascii=False)},
                maxlen=self._s.audit_sandbox_maxlen,
                approximate=True,
            )
        except Exception as e:
            logger.debug("audit_sandbox skip: %s", e)

    async def execute_shell_structured(
        self,
        *,
        redis: Any,
        command: str,
        session_id: str,
        trace_id: str,
        image: str | None = None,
        env: list[dict[str, str]] | None = None,
        pod_labels: dict[str, str] | None = None,
    ) -> SandboxExecResult:
        tid = (trace_id or "unknown").strip() or "unknown"
        sid = (session_id or "default").replace(" ", "_")[:200]
        run_id = str(uuid.uuid4())
        cmd = (command or "").strip()

        if not self.enabled:
            await self._audit_sandbox(
                redis,
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                policy_verdict="disabled",
                policy_reason="opensandbox_off",
                http_status=None,
                exit_code=None,
            )
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-1,
                stdout="",
                stderr="disabled",
                http_status=None,
                policy_verdict="disabled",
                policy_reason="opensandbox_off",
            )

        if not cmd:
            await self._audit_sandbox(
                redis,
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                policy_verdict="denied",
                policy_reason="empty_command",
            )
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-1,
                stdout="",
                stderr="empty command",
                http_status=None,
                policy_verdict="denied",
                policy_reason="empty_command",
            )

        if len(cmd) > 8000:
            await self._audit_sandbox(
                redis,
                trace_id=tid,
                session_id=sid,
                command=cmd[:8000],
                run_id=run_id,
                policy_verdict="denied",
                policy_reason="command_too_long",
            )
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-1,
                stdout="",
                stderr="command too long",
                http_status=None,
                policy_verdict="denied",
                policy_reason="command_too_long",
            )

        pol = check_sandbox_command(cmd, lab_unchained=bool(self._s.lab_unchained))
        await self._audit_sandbox(
            redis,
            trace_id=tid,
            session_id=sid,
            command=cmd,
            run_id=run_id,
            policy_verdict=pol.verdict.value,
            policy_reason=pol.reason,
        )

        if pol.verdict == PolicyVerdict.DENIED:
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-2,
                stdout="",
                stderr=f"policy_denied:{pol.reason}",
                http_status=None,
                policy_verdict=pol.verdict.value,
                policy_reason=pol.reason,
            )

        body: dict[str, Any] = {
            "command": cmd,
            "argv": ["/bin/sh", "-c", cmd],
            "image": image or self._s.opensandbox_default_image,
            "timeout_sec": int(min(self._s.opensandbox_timeout_s, 300)),
            "run_id": run_id,
            "trace_id": tid,
        }
        if env:
            body["env"] = env[:64]
        if pod_labels:
            body["pod_labels"] = dict(list(pod_labels.items())[:24])

        url = f"{self._url()}{self._s.opensandbox_exec_path}"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._s.opensandbox_timeout_s) as hc:
                r = await hc.post(url, json=body)
                elapsed = time.monotonic() - t0
                text = (r.text or "")[:12000]
                await self._audit_sandbox(
                    redis,
                    trace_id=tid,
                    session_id=sid,
                    command=cmd,
                    run_id=run_id,
                    policy_verdict=pol.verdict.value,
                    policy_reason=pol.reason,
                    http_status=r.status_code,
                    extra={"elapsed_sec": round(elapsed, 3), "response_preview": text[:2000]},
                )
                if r.status_code == 404:
                    return SandboxExecResult(
                        trace_id=tid,
                        session_id=sid,
                        command=cmd,
                        run_id=run_id,
                        exit_code=-1,
                        stdout="",
                        stderr=f"404 at {url}",
                        http_status=404,
                        policy_verdict=pol.verdict.value,
                        policy_reason=pol.reason,
                    )
                r.raise_for_status()
                try:
                    data = r.json()
                    out = str(data.get("stdout") or data.get("output") or data.get("result") or text)
                    err = str(data.get("stderr") or "")
                    exit_code = int(data.get("exit_code", data.get("code", 0)))
                except Exception:
                    out = text
                    err = ""
                    exit_code = 0
                await self._audit_sandbox(
                    redis,
                    trace_id=tid,
                    session_id=sid,
                    command=cmd,
                    run_id=run_id,
                    policy_verdict=pol.verdict.value,
                    policy_reason=pol.reason,
                    http_status=r.status_code,
                    exit_code=exit_code,
                    extra={"stdout_len": len(out), "stderr_len": len(err)},
                )
                return SandboxExecResult(
                    trace_id=tid,
                    session_id=sid,
                    command=cmd,
                    run_id=run_id,
                    exit_code=exit_code,
                    stdout=out,
                    stderr=err,
                    http_status=r.status_code,
                    policy_verdict=pol.verdict.value,
                    policy_reason=pol.reason,
                )
        except httpx.HTTPStatusError as e:
            await self._audit_sandbox(
                redis,
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                policy_verdict=pol.verdict.value,
                http_status=e.response.status_code if e.response else None,
                extra={"error": repr(e)},
            )
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-1,
                stdout="",
                stderr=f"http_error:{e!s}",
                http_status=e.response.status_code if e.response else None,
                policy_verdict=pol.verdict.value,
                policy_reason=pol.reason,
            )
        except Exception as e:
            await self._audit_sandbox(
                redis,
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                policy_verdict=pol.verdict.value,
                extra={"error": repr(e)},
            )
            logger.exception("sandbox execute")
            return SandboxExecResult(
                trace_id=tid,
                session_id=sid,
                command=cmd,
                run_id=run_id,
                exit_code=-1,
                stdout="",
                stderr=str(e),
                http_status=None,
                policy_verdict=pol.verdict.value,
                policy_reason=pol.reason,
            )

    async def execute_shell(
        self,
        *,
        redis: Any,
        command: str,
        session_id: str,
        trace_id: str,
        image: str | None = None,
        env: list[dict[str, str]] | None = None,
        pod_labels: dict[str, str] | None = None,
    ) -> str:
        res = await self.execute_shell_structured(
            redis=redis,
            command=command,
            session_id=session_id,
            trace_id=trace_id,
            image=image,
            env=env,
            pod_labels=pod_labels,
        )
        return sandbox_result_to_user_text(res)


def sandbox_result_to_user_text(res: SandboxExecResult) -> str:
    """Chuỗi user-facing từ kết quả structured (sau khi đã audit)."""
    if res.policy_verdict == "disabled":
        return (
            "[DATA] error\n[DIAGNOSIS] OpenSandbox tắt — bật OMNI_OPENSANDBOX_ENABLED và deploy server "
            "(xem k8s/opensandbox/README.md)."
        )
    if res.policy_verdict == "denied" or res.exit_code == -2:
        return f"[DATA] error\n[DIAGNOSIS] Policy từ chối lệnh sandbox: {res.policy_reason or res.stderr}"
    if res.http_status == 404:
        return (
            "[DATA] error\n[DIAGNOSIS] OpenSandbox API 404. Chỉnh OMNI_OPENSANDBOX_EXEC_PATH theo spec server."
        )
    if res.exit_code < 0 and res.stderr.startswith("http_error"):
        return f"[DATA] error\n[DIAGNOSIS] OpenSandbox HTTP: {res.stderr}"
    if res.exit_code < 0:
        return f"[DATA] error\n[DIAGNOSIS] OpenSandbox: {res.stderr}"
    return f"[DATA] sandbox_exec ok exit={res.exit_code}\n[OUTPUT]\n{res.stdout}"[:14000]


async def auto_cleanup_sandboxes(redis: Any, *, trace_id: str = "") -> str:
    try:
        await redis.srem(ACTIVE_SET_KEY, trace_id)
    except Exception:
        pass
    return "[DATA] sandbox_cleanup noop\n[DIAGNOSIS] Kiểm TTL sandbox trên OpenSandbox server."
