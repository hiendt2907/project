"""Agent durable delivery loop — persist trước execute, resume sau restart, report terminal.

Vòng đời một command ở agent (khớp DoD Step 3):

  receive → persist local inbox (RECEIVED) → accept → validate → execute/reconcile
          → persist local outcome → report terminal → nhận Gateway terminal ack
          → archive local command

Bất biến an toàn (never blindly retry a mutating command after unknown outcome):
- Đã có outcome cục bộ (OUTCOME_RECORDED) → chỉ RE-REPORT, KHÔNG execute lại.
- RUNNING chưa outcome khi resume → gọi ``reconcile`` (không blind re-execute).
- Chỉ archive khi Gateway trả terminal acknowledgement.

Loop KHÔNG cầm transport mutation trực tiếp — nhận ``executor``/``reconciler`` callable để tầng
mutation (operations/recovery) tiêm vào. Điều này giữ crash-safety tách khỏi business logic
và test được độc lập.

Delivery ownership/fencing (Gateway twin: ``src/gateway/routes/agent_runtime.py``): mỗi command
polled mang ``delivery_attempt``/``fencing_token``/``record_version`` của lần claim đó. Loop lưu
NGUYÊN VẸN ba field này vào inbox cục bộ tại lần persist ĐẦU và echo lại y hệt trên mọi
accept/progress/report_terminal — Gateway reject (409) nếu không khớp record hiện tại (stale
sau redelivery). Đây là GAP CÒN LẠI đã biết: nếu Gateway redeliver (visibility timeout) TRONG
LÚC agent vẫn đang RUNNING command cũ (persist() không reset), report cuối cùng sẽ dùng attempt
cũ và bị Gateway từ chối — cần lease renewal/heartbeat để gia hạn visibility, KHÔNG sửa ở đây.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from aoip.agent.inbox import (
    L_ACCEPTED,
    L_ACKED,
    L_REPORTED,
    L_RUNNING,
    InboxEntry,
    LocalInbox,
)

# executor(payload) -> (terminal_state, outcome); reconciler(entry) -> (terminal_state, outcome)
Executor = Callable[[dict], Awaitable[tuple[str, dict]]]
Reconciler = Callable[[InboxEntry], Awaitable[tuple[str, dict]]]


class RuntimeDeliveryClient(Protocol):
    async def poll_runtime(self, agent_id: str) -> list[dict]: ...
    async def accept(self, agent_id: str, tenant_id: str, command_id: str, *,
                     delivery_attempt: int, fencing_token: str) -> None: ...
    async def progress(self, agent_id: str, tenant_id: str, command_id: str, phase: str, *,
                       delivery_attempt: int, fencing_token: str) -> None: ...
    async def report_terminal(self, agent_id: str, tenant_id: str, command_id: str,
                              state: str, outcome: dict, *,
                              delivery_attempt: int, fencing_token: str) -> dict: ...


class DeliveryLoop:
    def __init__(self, *, agent_id: str, client: RuntimeDeliveryClient, inbox: LocalInbox,
                 executor: Executor, reconciler: Reconciler | None = None) -> None:
        self._agent_id = agent_id
        self._client = client
        self._inbox = inbox
        self._executor = executor
        self._reconciler = reconciler

    async def resume(self) -> int:
        """Gọi khi agent khởi động. Xử lý mọi command chưa ACKED trong inbox cục bộ.

        Trả số command đã đưa về terminal-ack. KHÔNG blind re-execute mutating command.
        """
        handled = 0
        for entry in self._inbox.pending():
            if entry.has_outcome:
                await self._report_and_archive(entry, entry.outcome, _state_of(entry))
                handled += 1
            elif entry.needs_reconcile and self._reconciler is not None:
                state, outcome = await self._reconciler(entry)
                self._inbox.record_outcome(entry.command_id, outcome)
                await self._report_and_archive(self._inbox.get(entry.command_id), outcome, state)
                handled += 1
            # RECEIVED/ACCEPTED chưa RUNNING → an toàn để tick() xử lý lại từ đầu.
        return handled

    async def tick(self) -> int:
        """Một vòng: pull → persist → accept → execute → record → report → archive."""
        commands = await self._client.poll_runtime(self._agent_id)
        processed = 0
        for cmd in commands:
            cid = cmd["command_id"]
            tenant = cmd.get("tenant_id", "")
            existing = self._inbox.get(cid)
            if existing is not None and existing.has_outcome:
                # duplicate delivery của command đã chạy → re-report, ZERO re-mutation
                await self._report_and_archive(existing, existing.outcome, _state_of(existing))
                processed += 1
                continue

            entry = self._inbox.persist(
                cid, tenant_id=tenant, payload=cmd.get("payload", {}),
                delivery_attempt=cmd.get("delivery_attempt", 0),
                fencing_token=cmd.get("fencing_token", ""),
                record_version=cmd.get("record_version", 0))
            self._inbox.set_state(cid, L_ACCEPTED)
            await self._client.accept(self._agent_id, tenant, cid,
                                      delivery_attempt=entry.delivery_attempt,
                                      fencing_token=entry.fencing_token)

            self._inbox.set_state(cid, L_RUNNING)
            await self._client.progress(self._agent_id, tenant, cid, "RUNNING",
                                        delivery_attempt=entry.delivery_attempt,
                                        fencing_token=entry.fencing_token)
            state, outcome = await self._executor(entry.payload)   # mutation xảy ra ở đây

            self._inbox.record_outcome(cid, outcome)               # bền TRƯỚC khi report
            await self._report_and_archive(self._inbox.get(cid), outcome, state)
            processed += 1
        return processed

    async def _report_and_archive(self, entry: InboxEntry, outcome: dict, state: str) -> None:
        ack = await self._client.report_terminal(
            self._agent_id, entry.tenant_id, entry.command_id, state, outcome,
            delivery_attempt=entry.delivery_attempt, fencing_token=entry.fencing_token)
        self._inbox.set_state(entry.command_id, L_REPORTED)
        if ack.get("acknowledged"):
            self._inbox.set_state(entry.command_id, L_ACKED)
            self._inbox.archive(entry.command_id)


def _state_of(entry: InboxEntry) -> str:
    """Terminal state suy từ outcome cục bộ (mặc định COMPLETED nếu rc==0)."""
    oc = entry.outcome or {}
    if oc.get("escalate"):
        return "ESCALATED"
    if oc.get("rc", 0) != 0 or oc.get("failed"):
        return "FAILED"
    return "COMPLETED"
