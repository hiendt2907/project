"""Capability: ``systemd.config_rollback`` — M6 Human-approved Config Restore-from-Backup.

Capability THỨ SÁU của VM/AOIP lane (Phase 4, remote-host/VM action library
expansion — ``docs/plans/omni-close-autonomous-sre-gaps-2026-07-23.md``). Cùng
vertical slice sản phẩm, cùng khuôn mẫu: Incident/Mission → evidence-backed
Decision → approval required → typed capability command → Agent preflight →
guarded execution → verification → structured outcome → Mission close/escalate
→ audit.

GAP đóng: khi bằng chứng (SIEM/config-drift detector, hoặc human report qua
``question_lifecycle``) cho thấy một unit hỏng vì file cấu hình vừa bị sửa sai
(không phải process down/failed/runaway), 3 capability trước đều không giải
quyết đúng gốc — restart/reset_failed/kill chỉ tác động tiến trình, KHÔNG phục
hồi NỘI DUNG config. ``apply()`` copy ``<path>.aoip-backup`` (backup last-known-
good, quy ước tên cố định — KHÔNG BAO GIỜ path do payload tự do chỉ định) đè
lên ``<path>`` rồi restart unit sở hữu config đó.

PATH RESOLUTION (quan trọng — khác các capability trước): ``<path>`` KHÔNG nằm
trong payload — nó được resolve từ ``unit`` qua allowlist map phía server
(``AOIP_CONFIG_ROLLBACK_PATHS="unit1:/etc/x.conf,unit2:/etc/y.conf"``, đọc ở
``aoip.recovery.config_rollback_path_for_unit``). Điều này khoá path khỏi
payload — kẻ tấn công đổi payload không thể trỏ tới path tuỳ ý; unit không nằm
trong map → KHÔNG có action nào chạy (fail-closed, coi như "no backup known").

REVERSIBILITY (bắt buộc chứng minh trước khi thêm capability — xem Phase 4 exit
criteria): TRƯỚC KHI ghi đè config sống, ``capture_before`` (ở
``aoip.recovery._cr_capture_before``) snapshot file HIỆN TẠI (sắp bị thay thế)
thành ``<path>.pre_rollback_snapshot`` — nếu rollback sai, vận hành có thể khôi
phục thủ công snapshot đó. Đây là capability RỦI RO NHẤT trong lô này (ghi đè
nội dung file + restart unit → downtime risk thật giống restart_unit), nên
risk đăng ký ngang ``systemd.restart_unit`` (0.30), KHÔNG thấp hơn.

Kiến trúc: build TRÊN cùng hạ tầng đã có, giống hệt
``systemd_reset_failed.py``/``systemd_journal_vacuum.py`` — KHÔNG viết lại
lease/idempotency/audit/tier_gate. Registry operator mới nằm ở ``aoip.recovery``
(``("config_drifted", "systemd")``), KHÔNG sửa executor chung.

Tái sử dụng TRỰC TIẾP (không hardcode lại) từ ``systemd_restart.py``: allowlist
policy/env var (unit sở hữu config phải nằm trong ``AOIP_ALLOWED_SYSTEMD_UNITS``
NGOÀI RA còn phải có entry trong ``AOIP_CONFIG_ROLLBACK_PATHS`` — 2 lớp allowlist
độc lập, cả hai đều fail-closed), ``validate_unit_name``, ``_unit_exists``,
``_classify_product_outcome``, hằng số ``OUTCOME_*``/``MODE_*``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aoip import audit
from aoip.agent.operations import capability_payload_hash, run_guarded_recovery
from aoip.agent.timing_config import TimingConfig
from aoip.capabilities.systemd_restart import (
    MODE_HUMAN_APPROVED,
    MODE_SHADOW,
    OUTCOME_APPROVAL_REJECTED,
    OUTCOME_APPROVAL_REQUIRED,
    OUTCOME_BLOCKED_BY_POLICY,
    OUTCOME_EXECUTED_AND_VERIFIED,
    OUTCOME_EXECUTION_FAILED,
    OUTCOME_NO_ACTION_NEEDED,
    OUTCOME_OWNERSHIP_LOST_AMBIGUOUS,
    OUTCOME_PRECONDITION_FAILED,
    OUTCOME_SHADOW_RECOMMENDATION,
    OUTCOME_UNSUPPORTED_CAPABILITY,
    OUTCOME_VERIFICATION_FAILED,
    SystemdRestartPolicy as SystemdUnitAllowlistPolicy,
    _classify_product_outcome,
    _unit_exists,
    _VALID_MODES,
    load_policy_from_env,
    validate_unit_name,
)
from aoip.objects import Action, ActionState, Finding
from aoip.recovery import (
    Approval,
    RecoveryGate,
    RecoveryRequest,
    config_rollback_path_for_unit,
    plan_recovery,
)

CAPABILITY_NAME = "systemd.config_rollback"
CAPABILITY_VERSION = "1"

_FAILURE_MODE = "config_drifted"
_SUBSTRATE = "systemd"
# Same as restart_unit — this capability restarts the unit after overwriting a
# live config file, so it carries the same downtime-risk profile.
_RISK = 0.30

CAPABILITY_METADATA = {
    "capability": CAPABILITY_NAME,
    "capability_version": CAPABILITY_VERSION,
    "requires_approval": True,
    "risk_class": "low",
    "blast_radius": "single_unit_single_config_file",
    "reversibility": "reversible_via_pre_rollback_snapshot",
    "verification_required": True,
}

SystemdRestartPolicy = SystemdUnitAllowlistPolicy


class CapabilityRejected(Exception):
    """Preflight fail-closed — KHÔNG gọi run_guarded_recovery. ``.product_outcome`` +
    ``.evidence`` mang đủ thông tin để caller build structured operator result."""

    def __init__(self, product_outcome: str, reason: str, evidence: dict | None = None) -> None:
        super().__init__(reason)
        self.product_outcome = product_outcome
        self.reason = reason
        self.evidence = evidence or {}


def describe_capability(capability: str, capability_version: str) -> dict | None:
    """Registry tra cứu tường minh — KHÔNG dynamic import/execute từ payload.

    Trả None nếu capability/version không được hỗ trợ → caller PHẢI fail-closed
    (``UNSUPPORTED_CAPABILITY``), KHÔNG thử đoán/generic-fallback.
    """
    if capability == CAPABILITY_NAME and capability_version == CAPABILITY_VERSION:
        return CAPABILITY_METADATA
    return None


def build_typed_payload(
    *, mission_id: str, decision_id: str, incident_id: str, summary: str, unit: str,
    require_unit_exists: bool = True, require_allowlisted: bool = True,
    require_config_drifted: bool = True,
) -> dict:
    """Contract tối thiểu — KHÔNG raw shell command, KHÔNG path (path resolve
    server-side qua ``config_rollback_path_for_unit``), chỉ target + reason +
    policy flags."""
    return {
        "capability": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "target": {"unit": unit},
        "reason": {"mission_id": mission_id, "decision_id": decision_id,
                  "incident_id": incident_id, "summary": summary},
        "preconditions": {"require_unit_exists": require_unit_exists,
                          "require_allowlisted": require_allowlisted,
                          "require_config_drifted": require_config_drifted},
        "verification": {"require_checksum_matches_backup": True},
    }


def _plan_action(unit: str) -> Action:
    """Derive Action (decision_goal/scope) qua ``plan_recovery`` — DÙNG CHUNG giữa
    approval-issuing (``issue_capability_command``) và decode (``_decode``)."""
    return plan_recovery(failed_node=f"svc:{unit}", failure_mode=_FAILURE_MODE,
                         substrate=_SUBSTRATE, unit=unit, port=None, risk=_RISK)


def issue_capability_command(
    *, typed_payload: dict, approver: str, tenant: str, issued_at: float, expires_at: float,
    action_id: str = "", canonical_scope: str = "",
    findings: tuple[Finding, ...] = (), diagnosis_confidence: float | None = None,
) -> dict:
    """Ký approval BINDING với payload_hash TẠI THỜI ĐIỂM issue rồi gói thành envelope
    durable-command hoàn chỉnh — cùng dual-shape (capability + recovery) như
    ``systemd_reset_failed.issue_capability_command`` (xem ADR-005)."""
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
        "recovery": {
            "failed_node": f"svc:{unit}",
            "failure_mode": action.result["failure_mode"],
            "substrate": action.result["substrate"],
            "unit": unit,
            "risk": action.result["risk"],
            "diagnosed_at": issued_at,
            "tenant": tenant,
            "dependents": [],
            "mission_id": typed_payload["reason"]["mission_id"],
            "incident_id": typed_payload["reason"]["incident_id"],
            "decision_id": typed_payload["reason"]["decision_id"],
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
        raise CapabilityRejected(OUTCOME_APPROVAL_REJECTED, "approval.approved=False — human đã từ chối")
    try:
        approval = Approval.issue(
            approver=appr_d["approver"], tenant=appr_d["tenant"],
            canonical_scope=appr_d["canonical_scope"], decision_goal=appr_d["decision_goal"],
            action_id=appr_d["action_id"], action_scope=appr_d.get("action_scope", f"svc:{unit}"),
            issued_at=float(appr_d["issued_at"]), expires_at=float(appr_d["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise CapabilityRejected(OUTCOME_APPROVAL_REQUIRED,
                                 f"invalid_or_missing_approval: {exc}") from exc

    reason_d = payload.get("reason") or {}
    action = _plan_action(unit).at(ActionState.APPROVED)
    req = RecoveryRequest(failed_node=f"svc:{unit}", failure_mode=_FAILURE_MODE, substrate=_SUBSTRATE,
                          unit=unit, port=None, action=action, risk=_RISK,
                          diagnosed_at=float(reason_d.get("diagnosed_at", time.time())),
                          dependents=(), tenant=tenant)

    ev_d = payload.get("evidence") or {}
    findings = [Finding(claim=f["claim"], references=tuple(f.get("references") or ()),
                        verdict=bool(f.get("verdict", False)), confidence=float(f.get("confidence", 0.0)))
               for f in (ev_d.get("findings") or ())]
    ctx = _EvidenceCtx(findings=findings, diagnosis_confidence=ev_d.get("diagnosis_confidence"))

    preflight_cfg = {**(payload.get("preconditions") or {}), **(payload.get("verification") or {})}
    return req, approval, ctx, preflight_cfg


def _operator_summary(*, unit: str, mode: str, product_outcome: str, reason: str,
                      evidence: dict, duration_s: float, approver: str = "") -> dict:
    """Result operator-facing — KHÔNG raw traceback, luôn có next step gợi ý."""
    next_step = {
        OUTCOME_EXECUTED_AND_VERIFIED: "Không cần hành động thêm — config đã verified khớp backup.",
        OUTCOME_NO_ACTION_NEEDED: "Config không còn drift so với backup — không có mutation nào chạy.",
        OUTCOME_BLOCKED_BY_POLICY: "Thêm unit vào AOIP_ALLOWED_SYSTEMD_UNITS nếu config rollback là chủ ý.",
        OUTCOME_APPROVAL_REQUIRED: "Cần approval hợp lệ (đúng tenant/scope/decision, chưa hết hạn).",
        OUTCOME_APPROVAL_REJECTED: "Approval đã bị từ chối — xem lại quyết định với người phê duyệt.",
        OUTCOME_PRECONDITION_FAILED: "Kiểm tra evidence preflight — payload/lease/unit/config-path không hợp lệ.",
        OUTCOME_EXECUTION_FAILED: "Kiểm tra log agent/transport — command cp/restart thất bại.",
        OUTCOME_VERIFICATION_FAILED: "Config VẪN không khớp backup hoặc unit không active sau rollback (escalated).",
        OUTCOME_OWNERSHIP_LOST_AMBIGUOUS: "Xác minh thủ công trạng thái unit — có thể 2 agent đã mutate.",
        OUTCOME_UNSUPPORTED_CAPABILITY: "Nâng cấp agent hoặc dùng capability được hỗ trợ.",
        OUTCOME_SHADOW_RECOMMENDATION: "SHADOW mode — duyệt sang HUMAN_APPROVED để thực thi thật.",
    }.get(product_outcome, "Xem evidence để quyết định bước tiếp theo.")
    return {
        "capability": CAPABILITY_NAME, "capability_version": CAPABILITY_VERSION,
        "attempted": f"config-rollback {unit}", "target": {"unit": unit}, "mode": mode,
        "approver": approver, "product_outcome": product_outcome, "reason": reason,
        "evidence": evidence, "duration_s": round(duration_s, 3), "next_step": next_step,
    }


async def build_systemd_config_rollback_executor(
    *, redis, holder: str, transport, audit_log: audit.FileAuditLog, gate: RecoveryGate,
    policy: SystemdUnitAllowlistPolicy, tenant: str, mode: str = MODE_HUMAN_APPROVED,
    env_auto_execute: bool = False, timing: TimingConfig | None = None, now=None,
):
    """Adapter capability-aware — nối payload typed → preflight → run_guarded_recovery.

    Thêm 1 preflight check RIÊNG so với 5 capability trước: unit PHẢI có config
    path đã map (``config_rollback_path_for_unit``) — thiếu map → PRECONDITION_FAILED
    fail-closed TRƯỚC KHI chạm run_guarded_recovery, KHÔNG suy đoán path.
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

        config_path = config_rollback_path_for_unit(unit)
        if not _check("config_path_mapped", config_path is not None,
                      f"unit={unit} AOIP_CONFIG_ROLLBACK_PATHS mapping"):
            summary = _operator_summary(unit=unit, mode=mode,
                                        product_outcome=OUTCOME_PRECONDITION_FAILED,
                                        reason=f"unit {unit!r} không có config path đã map — "
                                               "không biết backup nào để rollback",
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
                evidence={**evidence,
                         "would_execute": f"cp {config_path}.aoip-backup {config_path} && "
                                          f"systemctl restart {unit}",
                         "predicted_verification_plan": [f"sha256sum {config_path}",
                                                          "systemctl is-active " + unit]},
                duration_s=clock() - start, approver=approval.approver)
            audit_log.append(audit.EV_RECOVERY_PLANNED,
                             {"node": req.failed_node, "mode": "shadow", "unit": unit},
                             trace_id=req.action.scope)
            return "COMPLETED", {"rc": 0, **summary}

        outcome = await run_guarded_recovery(
            ctx, req=req, transport=transport, audit_log=audit_log, gate=gate,
            approval=approval, env_auto_execute=env_auto_execute, now=clock(), redis=redis,
            holder=holder, lease_ttl_s=int(timing.execution_lease_ttl_s),
            lease_renewal_interval_s=timing.lease_renewal_interval_s)

        product_outcome = _classify_product_outcome(outcome)
        summary = _operator_summary(unit=unit, mode=mode, product_outcome=product_outcome,
                                    reason=outcome.reason,
                                    evidence={**evidence,
                                              "recovery_evidence": list(outcome.evidence),
                                              "verification": outcome.verification.to_dict()},
                                    duration_s=clock() - start, approver=approval.approver)
        if outcome.status == "recovered":
            return "COMPLETED", {"rc": 0, "verified": True, **summary}
        if product_outcome == OUTCOME_NO_ACTION_NEEDED:
            return "COMPLETED", {"rc": 0, **summary}
        if outcome.status == "escalated":
            return "ESCALATED", {"rc": 1, **summary}
        return "FAILED", {"rc": 1, **summary}

    return executor
