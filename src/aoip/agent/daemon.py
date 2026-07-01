"""Long-running agent daemon — systemd service sống lâu trên VM Ubuntu.

    python -m aoip.agent.daemon --agent-id ubuntu-edge-1 --tenant acme \
        --gateway https://gateway.internal --api-key $KEY --inbox /var/lib/aoip/inbox

Vòng bền: register → heartbeat → RESUME (inbox pending) → pull → persist → execute →
report → sleep → repeat. Chống mất command qua process restart + machine reboot vì:
- inbox durable (fsync + atomic rename, StateDirectory systemd) — resume() chạy đầu mỗi
  boot: OUTCOME_RECORDED → re-report; RUNNING → reconcile; RECEIVED → tick xử lý lại.
- Gateway giữ record durable + redelivery đến khi terminal ack (GET=peek).

Executor mutation thật là ``operations.build_recovery_executor`` (nối ``run_guarded_recovery``
— lease + IdempotencyLedger + current-state revalidation, KHÔNG bypass). ``run_daemon`` dùng
executor này làm default KHI được tiêm đủ dependency (``redis``/``transport``/``audit_log``/
``gate``); nếu KHÔNG (vd unit test không cần mutation thật, hoặc CLI chưa wiring dependency —
xem gap ở cuối module), rơi về ``_noop_executor`` — DEV/TEST-ONLY, KHÔNG phải production path
mặc định khi đã có đủ dependency.
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from aoip.agent.delivery_loop import DeliveryLoop
from aoip.agent.inbox import LocalInbox
from aoip.agent.omni_client import HTTPOmniClient
from aoip.agent.operations import build_recovery_executor

_DEFAULT_INBOX = "/var/lib/aoip/inbox"
_DEFAULT_INTERVAL_S = 15


async def _noop_executor(payload: dict) -> tuple[str, dict]:
    """DEV/TEST-ONLY: KHÔNG mutation. Chỉ dùng khi chưa tiêm dependency recovery thật
    (redis/transport/audit_log/gate) — KHÔNG phải production default khi đã có đủ."""
    return "COMPLETED", {"rc": 0, "noop": True, "verb": payload.get("verb", "")}


def _default_executor(*, redis, transport, audit_log, gate, holder: str,
                      env_auto_execute: bool, now=None):
    """Chọn executor mặc định: production-safe adapter nếu có đủ dependency, else no-op."""
    if redis is not None and transport is not None and audit_log is not None and gate is not None:
        return build_recovery_executor(redis=redis, holder=holder, transport=transport,
                                       audit_log=audit_log, gate=gate,
                                       env_auto_execute=env_auto_execute, now=now)
    return _noop_executor


async def run_daemon(*, agent_id: str, tenant: str, gateway: str, api_key: str,
                     inbox_root: str, interval_s: int, executor=None,
                     max_ticks: int | None = None, client=None,
                     redis=None, transport=None, audit_log=None, gate=None,
                     env_auto_execute: bool = False, now=None) -> int:
    """Chạy vòng bền cho tới khi bị dừng (SIGTERM) hoặc đủ ``max_ticks`` (dùng cho test).

    ``client`` cho phép tiêm RuntimeDeliveryClient giả (test); production tự tạo HTTP client.
    ``executor`` tiêm trực tiếp (test) thắng mọi lựa chọn mặc định. Không tiêm ``executor``
    nhưng có đủ ``redis``/``transport``/``audit_log``/``gate`` → dùng adapter recovery thật.
    ``now`` (test-only): clock injectable cho ``build_recovery_executor``.
    """
    client = client or HTTPOmniClient(gateway, api_key=api_key)
    inbox = LocalInbox(inbox_root)
    resolved_executor = executor or _default_executor(
        redis=redis, transport=transport, audit_log=audit_log, gate=gate,
        holder=agent_id, env_auto_execute=env_auto_execute, now=now)
    loop = DeliveryLoop(agent_id=agent_id, client=client, inbox=inbox,
                        executor=resolved_executor)

    stopping = asyncio.Event()

    def _stop(*_):
        stopping.set()

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _stop)
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, _stop)
    except (NotImplementedError, RuntimeError):
        pass  # không phải main thread / platform không hỗ trợ

    # RESUME trước tiên: xử lý mọi command dang dở sau restart/reboot.
    resumed = await loop.resume()
    ticks = 0
    while not stopping.is_set():
        try:
            await loop.tick()
        except Exception:  # noqa: BLE001 — daemon không được chết vì 1 lỗi transport
            pass
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        await aclose()
    return resumed + ticks


def main() -> None:
    p = argparse.ArgumentParser(description="AOIP durable agent daemon")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--gateway", required=True)
    p.add_argument("--api-key", default="")
    p.add_argument("--inbox", default=_DEFAULT_INBOX)
    p.add_argument("--interval", type=int, default=_DEFAULT_INTERVAL_S)
    args = p.parse_args()
    asyncio.run(run_daemon(
        agent_id=args.agent_id, tenant=args.tenant, gateway=args.gateway,
        api_key=args.api_key, inbox_root=args.inbox, interval_s=args.interval))


if __name__ == "__main__":
    main()
