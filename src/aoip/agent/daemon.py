"""Long-running agent daemon — systemd service sống lâu trên VM Ubuntu.

    python -m aoip.agent.daemon --agent-id ubuntu-edge-1 --tenant acme \
        --gateway https://gateway.internal --api-key $KEY --inbox /var/lib/aoip/inbox

Vòng bền: register → heartbeat → RESUME (inbox pending) → pull → persist → execute →
report → sleep → repeat. Chống mất command qua process restart + machine reboot vì:
- inbox durable (fsync + atomic rename, StateDirectory systemd) — resume() chạy đầu mỗi
  boot: OUTCOME_RECORDED → re-report; RUNNING → reconcile; RECEIVED → tick xử lý lại.
- Gateway giữ record durable + redelivery đến khi terminal ack (GET=peek).

Executor mutation thật do tầng operations/recovery tiêm; ở đây chưa nối để giữ daemon
mỏng và test được — mặc định executor là no-op an toàn (report COMPLETED rc=0) trừ khi
``--enable-mutation`` (nối vào recovery pipeline, ngoài phạm vi slice này).
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from aoip.agent.delivery_loop import DeliveryLoop
from aoip.agent.inbox import LocalInbox
from aoip.agent.omni_client import HTTPOmniClient

_DEFAULT_INBOX = "/var/lib/aoip/inbox"
_DEFAULT_INTERVAL_S = 15


async def _noop_executor(payload: dict) -> tuple[str, dict]:
    """An toàn mặc định: KHÔNG mutation. Ghi nhận đã nhận, report COMPLETED rc=0."""
    return "COMPLETED", {"rc": 0, "noop": True, "verb": payload.get("verb", "")}


async def run_daemon(*, agent_id: str, tenant: str, gateway: str, api_key: str,
                     inbox_root: str, interval_s: int, executor=None,
                     max_ticks: int | None = None, client=None) -> int:
    """Chạy vòng bền cho tới khi bị dừng (SIGTERM) hoặc đủ ``max_ticks`` (dùng cho test).

    ``client`` cho phép tiêm RuntimeDeliveryClient giả (test); production tự tạo HTTP client.
    """
    client = client or HTTPOmniClient(gateway, api_key=api_key)
    inbox = LocalInbox(inbox_root)
    loop = DeliveryLoop(agent_id=agent_id, client=client, inbox=inbox,
                        executor=executor or _noop_executor)

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
