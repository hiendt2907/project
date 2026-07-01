"""Read-model projections — CÙNG timeline, HAI khán giả khác nhau.

Không nguồn sự thật thứ hai: cả hai chiếu từ cùng RuntimeTrace timeline. Provider nhấn
mạnh vận hành nền tảng (correlation/command/lease/fencing/reconcile/infra); Tenant nhấn
mạnh dịch vụ/lý do/impact/approval/verify/knowledge — che chi tiết hạ tầng.

raw evidence chỉ lộ khi include_raw=True (gắn quyền P_RAW_EVIDENCE, và bị audit ở provider).
"""
from __future__ import annotations

# Thứ tự phase an toàn để suy ra "đang ở đâu".
_PHASE_ORDER = [
    "COMMAND_RECEIVED", "IDEMPOTENCY_CLAIMED", "LEASE_ACQUIRED", "APPROVAL_VALIDATED",
    "MUTATION_STARTED", "VERIFYING", "RECONCILE_REQUIRED", "RECONCILED",
    "COMPLETED", "ESCALATED", "ABORTED", "APPROVAL_REJECTED",
]
_TERMINAL = {"COMPLETED", "ESCALATED", "ABORTED", "RECONCILED"}


def _service_of(canonical_scope: str) -> str:
    """acme:svc:cust-db → svc:cust-db (bỏ tenant prefix cho tenant view)."""
    return canonical_scope.split(":", 1)[1] if ":" in canonical_scope else canonical_scope


def _current_phase(events: list[dict]) -> str:
    if not events:
        return "unknown"
    return events[-1]["event_type"]


def _outcome(events: list[dict]) -> str | None:
    for e in reversed(events):
        if e["event_type"] in _TERMINAL:
            return e["event_type"]
    return None


def _find(events: list[dict], event_type: str) -> dict | None:
    for e in events:
        if e["event_type"] == event_type:
            return e
    return None


def _lease_token(events: list[dict]) -> str | None:
    e = _find(events, "LEASE_ACQUIRED")
    if not e:
        return None
    for ref in e.get("evidence_refs", []):
        if ref.startswith("lease_token:"):
            return ref.split(":", 1)[1]
    return None


def provider_incident(events: list[dict], *, include_raw: bool) -> dict:
    """Chiếu VẬN HÀNH NỀN TẢNG: correlation/command/lease/fencing/phase/report/outcome."""
    if not events:
        return {}
    head = events[0]
    commands = sorted({e["command_id"] for e in events if e.get("command_id")})
    view = {
        "correlation_id": head["correlation_id"],
        "tenant_id": head["tenant_id"],           # provider thấy tenant nào — hợp lệ
        "incident_id": head["incident_id"],
        "agent_id": head["agent_id"],
        "canonical_scope": head["canonical_scope"],
        "command_ids": commands,
        "lease_token": _lease_token(events),
        "fencing_epoch": next((e.get("source_version") for e in reversed(events)
                               if e["event_type"] == "MUTATION_STARTED"), None),
        "execution_phase": _current_phase(events),
        "reconcile_required": bool(_find(events, "RECONCILE_REQUIRED")),
        "reported": bool(_outcome(events)),
        "outcome": _outcome(events),
        "steps": [{"seq": e["seq"], "event": e["event_type"],
                   "state": f'{e["state_before"]}→{e["state_after"]}', "reason": e["reason"]}
                  for e in events],
    }
    # Raw evidence KHÔNG mặc định: chỉ đếm; chi tiết cần P_RAW_EVIDENCE + audit.
    if include_raw:
        view["evidence"] = [e.get("evidence_refs", []) for e in events]
    else:
        view["evidence_redacted"] = sum(len(e.get("evidence_refs", [])) for e in events)
    return view


def tenant_incident(events: list[dict], *, include_raw: bool) -> dict:
    """Chiếu KHÁCH HÀNG: dịch vụ/chẩn đoán/impact/approval/execution/verify/outcome + LÝ DO.

    Che hạ tầng: KHÔNG lease_token, KHÔNG fencing, KHÔNG command identity nội bộ.
    """
    if not events:
        return {}
    head = events[0]
    pending = (_find(events, "APPROVAL_VALIDATED") is None
               and _find(events, "APPROVAL_REJECTED") is None
               and _outcome(events) is None)
    view = {
        "correlation_id": head["correlation_id"],   # cùng incident để đối chiếu 2 portal
        "incident_id": head["incident_id"],
        "service": _service_of(head["canonical_scope"]),
        "current_state": _current_phase(events),
        "pending_approval": pending,
        "approved_by": (_find(events, "APPROVAL_VALIDATED") or {}).get("reason"),
        "outcome": _outcome(events),
        # Lý do dễ hiểu — vì sao AOIP đi từng bước (reason đã là VI, human-facing).
        "explanation": [{"step": e["event_type"], "why": e["reason"]} for e in events
                        if e["event_type"] not in ("IDEMPOTENCY_CLAIMED", "LEASE_ACQUIRED")],
    }
    if include_raw:
        view["evidence"] = [r for e in events for r in e.get("evidence_refs", [])]
    return view
