"""Capability: ``systemd.restart_unit`` — M1 Human-approved Systemd Service Recovery.

Vertical slice sản phẩm (KHÔNG phải generic capability framework):

    Incident/Mission → evidence-backed Decision → approval required
    → typed capability command → Agent preflight → guarded execution
    → verification → structured outcome → Mission close/escalate → audit

Cố ý KHÔNG dùng ``aoip.algebra``/``aoip.primitives`` (framework mô phỏng cũ,
KHÔNG có durable delivery/lease/fencing thật) — capability này build TRÊN Living
Operations Runtime thật đã có: ``aoip.recovery`` (operator process_down+systemd
đã tồn tại, KHÔNG viết lại) + ``aoip.agent.operations`` (``run_guarded_recovery``
— lease/ledger/idempotency, KHÔNG bypass) + Gateway durable delivery (attempt/
fencing đã có ở tầng dưới, KHÔNG re-implement ở đây).

Lớp NÀY chỉ thêm 4 thứ CHƯA có: (1) typed payload contract có version, (2) agent-
side unit allowlist (an toàn nhất của milestone — KHÔNG tồn tại trước đây), (3)
approval↔payload hash binding (đổi target sau approval → mất hiệu lực), (4) outcome
taxonomy sản phẩm (map RecoveryOutcome kỹ thuật → nhãn operator hiểu được) + SHADOW
mode (validate/preflight, KHÔNG mutate).

KHÔNG capability thứ hai trong module này. KHÔNG dynamic plugin registry — một
entry duy nhất, tra cứu bằng if/else tường minh (xem ``describe_capability``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from aoip import audit
from aoip.agent.operations import run_guarded_recovery
from aoip.agent.timing_config import TimingConfig
from aoip.objects import Action, ActionState, Finding
from aoip.recovery import Approval, RecoveryGate, RecoveryOutcome, RecoveryRequest, plan_recovery

CAPABILITY_NAME = "systemd.restart_unit"
CAPABILITY_VERSION = "1"

# Capability metadata (Bước 2/3) — query được, KHÔNG cần Agent chạy thử để biết.
CAPABILITY_METADATA = {
    "capability": CAPABILITY_NAME,
    "capability_version": CAPABILITY_VERSION,
    "requires_approval": True,          # HUMAN_APPROVED trong milestone này, luôn True
    "risk_class": "low",                # smallest reversible action (restart), 1 unit, 1 host
    "blast_radius": "single_unit",
    "reversibility": "reversible_via_restart",  # phục hồi lại state trước đó bằng cách restart lần nữa
    "verification_required": True,
}

# ── Unit name validation (Bước 4 — agent-side allowlist) ────────────────────
# Canonical systemd unit name: KHÔNG path (`/`), KHÔNG whitespace, KHÔNG shell
# metacharacter. Bắt buộc suffix `.service` (capability này chỉ restart service unit).
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}\.service$")

_ENV_ALLOWED_UNITS = "AOIP_ALLOWED_SYSTEMD_UNITS"
_ENV_ALLOW_SELF_RESTART = "AOIP_ALLOW_SELF_RESTART"
_ENV_AGENT_SERVICE_NAME = "AOIP_AGENT_SERVICE_NAME"

# ── Product outcome taxonomy (Bước 9) ────────────────────────────────────────
OUTCOME_EXECUTED_AND_VERIFIED = "EXECUTED_AND_VERIFIED"
OUTCOME_NO_ACTION_NEEDED = "NO_ACTION_NEEDED"
OUTCOME_BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
OUTCOME_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
OUTCOME_APPROVAL_REJECTED = "APPROVAL_REJECTED"
OUTCOME_PRECONDITION_FAILED = "PRECONDITION_FAILED"
OUTCOME_EXECUTION_FAILED = "EXECUTION_FAILED"
OUTCOME_VERIFICATION_FAILED = "VERIFICATION_FAILED"
OUTCOME_OWNERSHIP_LOST_AMBIGUOUS = "OWNERSHIP_LOST_AMBIGUOUS"
OUTCOME_UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
OUTCOME_SHADOW_RECOMMENDATION = "SHADOW_RECOMMENDATION"

MODE_SHADOW = "shadow"
MODE_HUMAN_APPROVED = "human_approved"
_VALID_MODES = (MODE_SHADOW, MODE_HUMAN_APPROVED)


class CapabilityRejected(Exception):
    """Preflight fail-closed — KHÔNG gọi run_guarded_recovery. ``.product_outcome`` +
    ``.evidence`` mang đủ thông tin để caller build structured operator result."""

    def __init__(self, product_outcome: str, reason: str, evidence: dict | None = None) -> None:
        super().__init__(reason)
        self.product_outcome = product_outcome
        self.reason = reason
        self.evidence = evidence or {}


def describe_capability(capability: str, capability_version: str) -> dict | None:
    """Registry tra cứu tường minh (Bước 3) — KHÔNG dynamic import/execute từ payload.

    Trả None nếu capability/version không được hỗ trợ → caller PHẢI fail-closed
    (``UNSUPPORTED_CAPABILITY``), KHÔNG thử đoán/generic-fallback.
    """
    if capability == CAPABILITY_NAME and capability_version == CAPABILITY_VERSION:
        return CAPABILITY_METADATA
    return None


# ── Policy (agent-side allowlist) ────────────────────────────────────────────
@dataclass(frozen=True)
class SystemdRestartPolicy:
    """Cấu hình allowlist phía Agent — fail-closed: rỗng = KHÔNG restart gì (KHÔNG
    phải wildcard-allow). Match CHÍNH XÁC tên unit canonical, không wildcard."""

    allowed_units: frozenset[str] = frozenset()
    allow_self_restart: bool = False
    agent_service_name: str = ""

    def is_allowed(self, unit: str) -> bool:
        if not self.allow_self_restart and self.agent_service_name and unit == self.agent_service_name:
            return False
        return unit in self.allowed_units


def load_policy_from_env(env: dict | None = None) -> SystemdRestartPolicy:
    """Đọc allowlist từ env (convention repo — xem CLAUDE.md ENV section).

    Thiếu/rỗng ``AOIP_ALLOWED_SYSTEMD_UNITS`` → allowlist RỖNG (fail-closed), KHÔNG
    fallback cho phép tất cả.
    """
    env = os.environ if env is None else env
    raw = env.get(_ENV_ALLOWED_UNITS, "").strip()
    units = frozenset(u.strip() for u in raw.split(",") if u.strip())
    allow_self = env.get(_ENV_ALLOW_SELF_RESTART, "false").strip().lower() == "true"
    agent_service = env.get(_ENV_AGENT_SERVICE_NAME, "").strip()
    return SystemdRestartPolicy(allowed_units=units, allow_self_restart=allow_self,
                                agent_service_name=agent_service)


def validate_unit_name(unit: str) -> str | None:
    """Trả None nếu hợp lệ; else lý do reject. KHÔNG raise — caller quyết định outcome."""
    if not unit or not _UNIT_NAME_RE.fullmatch(unit):
        return (f"invalid_unit_name: {unit!r} không khớp canonical pattern "
                f"(chỉ [A-Za-z0-9_.:@-]+.service, KHÔNG path/whitespace/shell metachar)")
    return None


# ── Typed payload contract (Bước 2) ──────────────────────────────────────────
def build_typed_payload(
    *, mission_id: str, decision_id: str, incident_id: str, summary: str, unit: str,
    require_unit_exists: bool = True, require_allowlisted: bool = True,
    require_active_state: bool = True, health_check: dict | None = None,
) -> dict:
    """Contract tối thiểu — KHÔNG raw shell command, chỉ target + reason + policy flags."""
    return {
        "capability": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "target": {"unit": unit},
        "reason": {"mission_id": mission_id, "decision_id": decision_id,
                  "incident_id": incident_id, "summary": summary},
        "preconditions": {"require_unit_exists": require_unit_exists,
                          "require_allowlisted": require_allowlisted},
        "verification": {"require_active_state": require_active_state,
                         "health_check": health_check},
    }


def _plan_action(unit: str) -> Action:
    """Derive Action (decision_goal/scope) qua ``plan_recovery`` — DÙNG CHUNG giữa approval-
    issuing (``issue_capability_command``) và decode (``_decode``) để không lệch nhau."""
    return plan_recovery(failed_node=f"svc:{unit}", failure_mode="process_down",
                         substrate="systemd", unit=unit, port=None, risk=0.30)


def capability_payload_hash(typed_payload: dict) -> str:
    """Hash canonical (sorted keys, không whitespace) — đổi BẤT KỲ field nào (kể cả
    reason.summary) sau khi approval issue → hash khác → approval mất hiệu lực."""
    canonical = json.dumps(typed_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def issue_capability_command(
    *, typed_payload: dict, approver: str, tenant: str, issued_at: float, expires_at: float,
    action_id: str = "", canonical_scope: str = "",
    findings: tuple[Finding, ...] = (), diagnosis_confidence: float | None = None,
) -> dict:
    """Ký approval BINDING với payload_hash TẠI THỜI ĐIỂM issue (Bước 6) rồi gói thành
    envelope durable-command hoàn chỉnh (sẵn sàng ``enqueue_command`` phía Gateway).

    ``action_id``/``canonical_scope`` mặc định derive từ unit nếu không truyền — capability
    này không cần caller tự quản lý identity nếu chỉ có 1 unit/1 decision.
    """
    unit = typed_payload["target"]["unit"]
    decision_id = typed_payload["reason"]["decision_id"]
    action_id = action_id or f"act-{decision_id}-{unit}"
    canonical_scope = canonical_scope or f"{tenant}:svc:{unit}"
    action = _plan_action(unit)
    approval = Approval.issue(
        approver=approver, tenant=tenant, canonical_scope=canonical_scope,
        decision_goal=action.decision_goal, action_id=action_id, action_scope=action.scope,
        issued_at=issued_at, expires_at=expires_at)
    approved_hash = capability_payload_hash(typed_payload)
    return {
        **typed_payload,
        "approved_payload_hash": approved_hash,
        "approval": {
            "approved": approval.approved, "approver": approval.approver,
            "tenant": approval.tenant, "decision_goal": approval.decision_goal,
            "expires_at": approval.expires_at, "action_id": approval.action_id,
            "action_scope": approval.action_scope, "canonical_scope": approval.canonical_scope,
            "issued_at": approval.issued_at,
        },
        "evidence": {
            "diagnosis_confidence": diagnosis_confidence,
            "findings": [{"claim": f.claim, "references": list(f.references),
                         "verdict": f.verdict, "confidence": f.confidence} for f in findings],
        },
    }


@dataclass
class _EvidenceCtx:
    findings: list[Finding] = field(default_factory=list)
    diagnosis_confidence: float | None = None
    trace: list[str] = field(default_factory=list)

    def log(self, verb: str, detail: str) -> None:
        self.trace.append(f"{verb}: {detail}")


def _decode(payload: dict, *, tenant: str) -> tuple[RecoveryRequest, Approval, _EvidenceCtx, dict]:
    """Parse typed payload → (RecoveryRequest, Approval, ctx, preconditions/verification dict).

    Raise ``CapabilityRejected`` fail-closed cho MỌI lỗi decode/hash-mismatch — caller
    KHÔNG được gọi run_guarded_recovery khi bắt exception này.
    """
    capability = payload.get("capability")
    capability_version = payload.get("capability_version")
    if describe_capability(capability, capability_version) is None:
        raise CapabilityRejected(
            OUTCOME_UNSUPPORTED_CAPABILITY,
            f"unsupported capability={capability!r} version={capability_version!r}",
            {"capability": capability, "capability_version": capability_version})

    target = payload.get("target") or {}
    unit = target.get("unit", "")
    bad = validate_unit_name(unit)
    if bad:
        raise CapabilityRejected(OUTCOME_PRECONDITION_FAILED, bad, {"unit": unit})

    expected_hash = payload.get("approved_payload_hash", "")
    typed_only = {k: v for k, v in payload.items()
                 if k in ("capability", "capability_version", "target", "reason",
                          "preconditions", "verification")}
    actual_hash = capability_payload_hash(typed_only)
    if not expected_hash or actual_hash != expected_hash:
        raise CapabilityRejected(
            OUTCOME_PRECONDITION_FAILED,
            "payload_hash_mismatch: payload đã đổi sau khi approval issue — approval mất hiệu lực",
            {"expected_hash": expected_hash, "actual_hash": actual_hash})

    appr_d = payload.get("approval") or {}
    if not appr_d.get("approved", False):
        # ``Approval.issue()`` luôn construct approved=True (production path) — REJECTED
        # phải chặn TRƯỚC khi gọi issue(), không dựa vào field .approved của kết quả.
        raise CapabilityRejected(OUTCOME_APPROVAL_REJECTED, "approval.approved=False — human đã từ chối")
    try:
        approval = Approval.issue(
            approver=appr_d["approver"], tenant=appr_d["tenant"],
            canonical_scope=appr_d["canonical_scope"], decision_goal=appr_d["decision_goal"],
            action_id=appr_d["action_id"], action_scope=appr_d.get("action_scope", f"svc:{unit}"),
            issued_at=float(appr_d["issued_at"]), expires_at=float(appr_d["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise CapabilityRejected(OUTCOME_APPROVAL_REQUIRED, f"invalid_or_missing_approval: {exc}") from exc

    reason_d = payload.get("reason") or {}
    action = _plan_action(unit).at(ActionState.APPROVED)
    req = RecoveryRequest(failed_node=f"svc:{unit}", failure_mode="process_down", substrate="systemd",
                          unit=unit, port=None, action=action, risk=0.30,
                          diagnosed_at=float(reason_d.get("diagnosed_at", time.time())),
                          dependents=(), tenant=tenant)

    ev_d = payload.get("evidence") or {}
    findings = [Finding(claim=f["claim"], references=tuple(f.get("references") or ()),
                        verdict=bool(f.get("verdict", False)), confidence=float(f.get("confidence", 0.0)))
               for f in (ev_d.get("findings") or ())]
    ctx = _EvidenceCtx(findings=findings, diagnosis_confidence=ev_d.get("diagnosis_confidence"))

    preflight_cfg = {**(payload.get("preconditions") or {}), **(payload.get("verification") or {})}
    return req, approval, ctx, preflight_cfg


async def _unit_exists(transport, unit: str) -> bool:
    """`systemctl show -p LoadState` — argv cố định, KHÔNG shell=True/bash -c/eval."""
    out, _ = await transport.run(["systemctl", "show", "-p", "LoadState", "--value", unit], timeout=5.0)
    return out.strip().lower() not in ("", "not-found")


def _classify_product_outcome(outcome: RecoveryOutcome) -> str:
    """Map RecoveryOutcome kỹ thuật → nhãn taxonomy sản phẩm (Bước 9)."""
    reason = outcome.reason.lower()
    if outcome.status == "recovered":
        return OUTCOME_EXECUTED_AND_VERIFIED
    if outcome.status == "escalated":
        if "ownership_lost" in reason:
            return OUTCOME_OWNERSHIP_LOST_AMBIGUOUS
        return OUTCOME_VERIFICATION_FAILED
    # status == "aborted"
    if "healthy" in reason:
        return OUTCOME_NO_ACTION_NEEDED
    if "lease" in reason:
        return OUTCOME_PRECONDITION_FAILED  # conflicting operation trên cùng target
    if "approval" in reason:
        return OUTCOME_APPROVAL_REJECTED
    return OUTCOME_EXECUTION_FAILED


def _operator_summary(*, unit: str, mode: str, product_outcome: str, reason: str,
                      evidence: dict, duration_s: float, approver: str = "") -> dict:
    """Result operator-facing (Bước 9) — KHÔNG raw traceback, luôn có next step gợi ý."""
    next_step = {
        OUTCOME_EXECUTED_AND_VERIFIED: "Không cần hành động thêm — service đã verified active.",
        OUTCOME_NO_ACTION_NEEDED: "Service đã healthy trước khi restart — không có mutation nào chạy.",
        OUTCOME_BLOCKED_BY_POLICY: "Thêm unit vào AOIP_ALLOWED_SYSTEMD_UNITS nếu restart là chủ ý.",
        OUTCOME_APPROVAL_REQUIRED: "Cần approval hợp lệ (đúng tenant/scope/decision, chưa hết hạn).",
        OUTCOME_APPROVAL_REJECTED: "Approval đã bị từ chối — xem lại quyết định với người phê duyệt.",
        OUTCOME_PRECONDITION_FAILED: "Kiểm tra evidence preflight — payload/lease/unit không hợp lệ.",
        OUTCOME_EXECUTION_FAILED: "Kiểm tra log agent/transport — command restart thất bại.",
        OUTCOME_VERIFICATION_FAILED: "Service KHÔNG active sau restart — cần điều tra thủ công (escalated).",
        OUTCOME_OWNERSHIP_LOST_AMBIGUOUS: "Xác minh thủ công trạng thái unit — có thể 2 agent đã mutate.",
        OUTCOME_UNSUPPORTED_CAPABILITY: "Nâng cấp agent hoặc dùng capability được hỗ trợ.",
        OUTCOME_SHADOW_RECOMMENDATION: "SHADOW mode — duyệt sang HUMAN_APPROVED để thực thi thật.",
    }.get(product_outcome, "Xem evidence để quyết định bước tiếp theo.")
    return {
        "capability": CAPABILITY_NAME, "capability_version": CAPABILITY_VERSION,
        "attempted": f"restart {unit}", "target": {"unit": unit}, "mode": mode,
        "approver": approver, "product_outcome": product_outcome, "reason": reason,
        "evidence": evidence, "duration_s": round(duration_s, 3), "next_step": next_step,
    }


async def build_systemd_restart_executor(
    *, redis, holder: str, transport, audit_log: audit.FileAuditLog, gate: RecoveryGate,
    policy: SystemdRestartPolicy, tenant: str, mode: str = MODE_HUMAN_APPROVED,
    env_auto_execute: bool = False, timing: TimingConfig | None = None, now=None,
):
    """Adapter capability-aware — nối payload typed → preflight → run_guarded_recovery.

    ``mode``: ``human_approved`` (mặc định — thực thi thật qua guarded recovery, giữ
    nguyên lease/ledger/fencing) hoặc ``shadow`` (validate + preflight, KHÔNG mutate,
    trả recommendation). Cả hai đều fail-closed như nhau ở preflight.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode={mode!r}, phải là {_VALID_MODES}")
    timing = timing or TimingConfig()
    clock = now or time.time

    async def executor(payload: dict) -> tuple[str, dict]:
        start = clock()
        unit = (payload.get("target") or {}).get("unit", "")
        try:
            req, approval, ctx, cfg = _decode(payload, tenant=tenant)
        except CapabilityRejected as exc:
            summary = _operator_summary(unit=unit, mode=mode, product_outcome=exc.product_outcome,
                                        reason=exc.reason, evidence=exc.evidence,
                                        duration_s=clock() - start)
            return "FAILED", {"rc": 1, **summary}

        # ── Preflight (Bước 5) — evidence trước khi chạm run_guarded_recovery ──
        evidence: dict[str, Any] = {"capability_checks": []}

        def _check(name: str, ok: bool, detail: str) -> bool:
            evidence["capability_checks"].append({"check": name, "ok": ok, "detail": detail})
            return ok

        allowlisted = policy.is_allowed(unit) if cfg.get("require_allowlisted", True) else True
        if not _check("unit_allowlisted", allowlisted, f"unit={unit} allowlist={sorted(policy.allowed_units)}"):
            summary = _operator_summary(unit=unit, mode=mode, product_outcome=OUTCOME_BLOCKED_BY_POLICY,
                                        reason=f"unit {unit!r} không nằm trong allowlist agent",
                                        evidence=evidence, duration_s=clock() - start,
                                        approver=approval.approver)
            return "FAILED", {"rc": 1, **summary}

        if cfg.get("require_unit_exists", True):
            exists = await _unit_exists(transport, unit)
            if not _check("unit_exists", exists, f"systemctl show LoadState unit={unit}"):
                summary = _operator_summary(unit=unit, mode=mode,
                                            product_outcome=OUTCOME_PRECONDITION_FAILED,
                                            reason=f"unit {unit!r} không tồn tại trên host",
                                            evidence=evidence, duration_s=clock() - start,
                                            approver=approval.approver)
                return "FAILED", {"rc": 1, **summary}
        _check("capability_version_supported", True, f"{CAPABILITY_NAME}@{CAPABILITY_VERSION}")
        _check("payload_hash_bound", True, "approved_payload_hash khớp payload nhận được")

        if mode == MODE_SHADOW:
            summary = _operator_summary(
                unit=unit, mode=mode, product_outcome=OUTCOME_SHADOW_RECOMMENDATION,
                reason="SHADOW: preflight PASS — KHÔNG thực thi mutation thật",
                evidence={**evidence, "would_execute": "systemctl restart " + unit,
                         "predicted_verification_plan": ["systemctl is-active " + unit]},
                duration_s=clock() - start, approver=approval.approver)
            audit_log.append(audit.EV_RECOVERY_PLANNED,
                             {"node": req.failed_node, "mode": "shadow", "unit": unit},
                             trace_id=req.action.scope)
            return "COMPLETED", {"rc": 0, **summary}

        # ── HUMAN_APPROVED: thực thi qua run_guarded_recovery THẬT (lease/ledger/fencing) ──
        outcome = await run_guarded_recovery(
            ctx, req=req, transport=transport, audit_log=audit_log, gate=gate,
            approval=approval, env_auto_execute=env_auto_execute, now=clock(), redis=redis,
            holder=holder, lease_ttl_s=int(timing.execution_lease_ttl_s),
            lease_renewal_interval_s=timing.lease_renewal_interval_s)

        product_outcome = _classify_product_outcome(outcome)
        summary = _operator_summary(unit=unit, mode=mode, product_outcome=product_outcome,
                                    reason=outcome.reason,
                                    evidence={**evidence, "recovery_evidence": list(outcome.evidence)},
                                    duration_s=clock() - start, approver=approval.approver)
        if outcome.status == "recovered":
            return "COMPLETED", {"rc": 0, "verified": True, **summary}
        if product_outcome == OUTCOME_NO_ACTION_NEEDED:
            return "COMPLETED", {"rc": 0, **summary}
        if outcome.status == "escalated":
            return "ESCALATED", {"rc": 1, **summary}
        return "FAILED", {"rc": 1, **summary}

    return executor
