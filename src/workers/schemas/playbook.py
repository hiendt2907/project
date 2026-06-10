"""PlaybookSpec — typed, domain-agnostic remediation playbook (L4 playbook-first).

Một schema, nhiều backend: ``backend="k8s"`` chạy qua executor SDK hiện có
(``run_execute_mutate_tool``); backend tương lai (``remote``) chạy qua signed
actuator channel. LLM CHỈ CHỌN playbook (PlaybookSelection) — không sinh lệnh.

Bất biến:
- Mọi playbook PHẢI có proof_of_fault (precheck) — unverifiable = abort fail-closed.
- Mọi step mutate PHẢI khai verify; rollback_type ghi rõ none|snapshot|compensating.
- Graduation per tenant×domain×playbook: DRAFT → CANDIDATE → GRADUATED → FROZEN.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PlaybookDomain = Literal["k8s", "os", "network", "service", "application", "api", "hardware"]
PlaybookBackend = Literal["k8s", "remote"]
RollbackType = Literal["none", "snapshot", "compensating"]

# Graduation states (per tenant×domain×playbook)
GRAD_DRAFT = "DRAFT"            # mới sinh (L5 hypothesis) — không bao giờ execute
GRAD_CANDIDATE = "CANDIDATE"    # chạy ở SUGGEST/HITL; đếm success
GRAD_GRADUATED = "GRADUATED"    # đủ điều kiện auto-execute (vẫn qua mọi gate)
GRAD_FROZEN = "FROZEN"          # breaker trip / admin freeze — chỉ admin mở
GRADUATION_STATES = (GRAD_DRAFT, GRAD_CANDIDATE, GRAD_GRADUATED, GRAD_FROZEN)


class ProofOfFault(BaseModel):
    """Precheck bắt buộc: re-verify fault còn tồn tại NGAY TRƯỚC mutate.

    method="reconcile": dùng workers.verify_reconcile.reconcile_advisory —
    verdict phải là "confirmed" mới được mutate. "refuted"/"unverifiable" = abort.
    """

    method: Literal["reconcile"] = "reconcile"
    # Khớp fault keywords với root_cause/alert (chống chọn nhầm playbook).
    fault_keywords: list[str] = Field(default_factory=list)


class VerifySpec(BaseModel):
    """Hậu kiểm sau mutate — đọc ground truth, không tin LLM self-grade."""

    method: Literal["reconcile"] = "reconcile"
    settle_sec: int = Field(default=30, ge=5, le=600)
    attempts: int = Field(default=3, ge=1, le=10)


class PlaybookStepSpec(BaseModel):
    step_order: int = Field(ge=1)
    backend: PlaybookBackend = "k8s"
    # backend=k8s: action = tool_name trong tool_registry (PHẢI thuộc lớp mutate).
    action: str
    # Template params; placeholder {namespace}/{deployment}/{pod} render từ alert ctx.
    params_template: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=120, ge=10, le=900)
    verify: VerifySpec = Field(default_factory=VerifySpec)
    rollback_type: RollbackType = "snapshot"
    requires_hitl: bool = False


class TriggerMatch(BaseModel):
    """Điều kiện match playbook với alert/advisory (deterministic, chạy trước LLM)."""

    lanes: list[str] = Field(default_factory=list)  # SYS_RESOURCE|SYS_HARD_FAIL|APP_HTTP|SIEM_SECURITY
    # regex-free: keyword chứa-trong (lowercase) trên alertname/reason/root_cause.
    fault_keywords: list[str] = Field(default_factory=list)
    severity_filter: str = ""  # "" = mọi severity


class PlaybookSpec(BaseModel):
    playbook_id: str
    version: int = Field(default=1, ge=1)
    name: str
    domain: PlaybookDomain = "k8s"
    trigger: TriggerMatch = Field(default_factory=TriggerMatch)
    proof_of_fault: ProofOfFault = Field(default_factory=ProofOfFault)
    steps: list[PlaybookStepSpec] = Field(min_length=1)
    # Trần blast-radius: số workload tối đa playbook được đụng trong 1 lần chạy.
    max_blast_radius: int = Field(default=1, ge=1, le=3)
    # Trạng thái graduation KHỞI ĐIỂM khi seed (runtime state ở PlaybookGovernor).
    initial_graduation: str = GRAD_CANDIDATE
    approved_by: str = ""
    notes: str = ""

    @field_validator("initial_graduation")
    @classmethod
    def _valid_grad(cls, v: str) -> str:
        if v not in GRADUATION_STATES:
            raise ValueError(f"initial_graduation must be one of {GRADUATION_STATES}")
        return v

    def ordered_steps(self) -> list[PlaybookStepSpec]:
        return sorted(self.steps, key=lambda s: s.step_order)

    def any_step_requires_hitl(self) -> bool:
        return any(s.requires_hitl for s in self.steps)


class PlaybookSelection(BaseModel):
    """Output typed của analyst: LLM/matcher CHỌN playbook, không sinh lệnh."""

    playbook_id: str
    version: int = 1
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    # Render context cho params_template — chỉ giá trị trích từ evidence/alert labels.
    render_ctx: dict[str, str] = Field(default_factory=dict)


def render_params(template: dict[str, Any], render_ctx: dict[str, str]) -> dict[str, Any]:
    """Render ``{placeholder}`` trong giá trị string của template từ render_ctx.

    Fail-closed: placeholder không có trong render_ctx → ValueError (không gửi
    args thiếu/biến dạng xuống executor).
    """
    out: dict[str, Any] = {}
    for k, v in template.items():
        if isinstance(v, str) and "{" in v:
            try:
                out[k] = v.format(**render_ctx)
            except (KeyError, IndexError) as exc:
                raise ValueError(f"unresolved placeholder in param {k!r}: {exc}") from exc
        else:
            out[k] = v
    return out
