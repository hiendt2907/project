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

from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from pkg.domain.taxonomy import (
    CANONICAL_DOMAINS,
    LANE_TO_DOMAIN,
    UNKNOWN,
    normalize_domain,
)

# domain → các lane trục A từng mang domain đó. Suy từ `LANE_TO_DOMAIN` chứ không khai
# tay: một bảng chép tay thứ hai là chỗ lệch tiếp theo.
#
# `SYS_HARD_FAIL` và `ONBOARDING_DISCOVERY` KHÔNG xuất hiện ở đây vì chúng map sang
# `unknown` — không domain nào "sở hữu" chúng. Hệ quả có chủ đích: một trigger khai
# `domains=["storage"]` sẽ KHÔNG khớp một sự cố còn mang lane `SYS_HARD_FAIL`. Muốn
# khớp thì khai thẳng `lanes=["SYS_HARD_FAIL"]` — tức phải nói ra là mình đang dựa vào
# một nhãn đã mất thông tin.
_DOMAIN_TO_LANES: dict[str, tuple[str, ...]] = {}
for _lane, _dom in LANE_TO_DOMAIN.items():
    if _dom != UNKNOWN:
        _DOMAIN_TO_LANES[_dom] = _DOMAIN_TO_LANES.get(_dom, ()) + (_lane.upper(),)

# Từ vựng domain là của `pkg.domain.taxonomy`, KHÔNG phải của module này. Liệt kê lại ở
# đây chính là gốc của việc lệch từ vựng ('k8s' vs 'kubernetes', 'os' vs 'os_host').
# Literal buộc phải viết literal tường minh (type checker không đọc được tuple runtime),
# nên chốt bằng assert bên dưới: thêm domain vào taxonomy mà quên chỗ này ⇒ vỡ lúc import,
# không phải vỡ âm thầm ở runtime nhiều tuần sau.
PlaybookDomain = Literal[
    "kubernetes", "os_host", "network", "storage", "database",
    "service", "application", "security", "hardware",
]

if set(get_args(PlaybookDomain)) != set(CANONICAL_DOMAINS):  # pragma: no cover — invariant
    raise RuntimeError(
        "PlaybookDomain lech CANONICAL_DOMAINS: "
        f"{sorted(set(get_args(PlaybookDomain)) ^ set(CANONICAL_DOMAINS))}"
    )

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

    # Cách MỚI để khai phạm vi: domain canonical (`pkg.domain.taxonomy`).
    domains: list[str] = Field(default_factory=list)
    # DEPRECATED (lane trục A): SYS_RESOURCE|SYS_HARD_FAIL|APP_HTTP|SIEM_SECURITY.
    # Sau validate, field này chứa HỢP của cả hai từ vựng — xem `_expand_scope`.
    lanes: list[str] = Field(default_factory=list)
    # regex-free: keyword chứa-trong (lowercase) trên alertname/reason/root_cause.
    fault_keywords: list[str] = Field(default_factory=list)
    severity_filter: str = ""  # "" = mọi severity

    @model_validator(mode="after")
    def _expand_scope(self) -> TriggerMatch:
        """Nhồi cả hai từ vựng vào ``lanes`` để matcher khớp bất kể caller gửi gì.

        Vì sao nhồi thay vì sửa matcher: PlaybookSpec đã lưu trong Redis (`pbspec:*`)
        và caller chuyển sang gửi domain KHÔNG cùng lúc. Nếu matcher chỉ so một từ
        vựng thì trong cả cửa sổ chuyển tiếp, playbook đúng lặng lẽ không được chọn —
        Omni mất năng lực khắc phục mà không có lỗi nào bật ra.

        Không xoá giá trị người vận hành đã khai, chỉ THÊM dạng tương đương. Nhãn lạ
        giữ nguyên (matcher tự không khớp) — không ném lỗi ở đây vì đây là đường ĐỌC
        spec cũ.
        """
        scope: list[str] = []

        def _add(value: str) -> None:
            if value and value not in scope:
                scope.append(value)

        for token in [*self.lanes, *self.domains]:
            raw = (token or "").strip()
            if not raw:
                continue
            _add(raw.upper())
            dom = normalize_domain(raw)
            if dom == UNKNOWN:
                dom = LANE_TO_DOMAIN.get(raw.lower().replace("-", "_"), UNKNOWN)
            if dom == UNKNOWN:
                continue
            _add(dom.upper())
            for lane in _DOMAIN_TO_LANES.get(dom, ()):
                _add(lane)

        object.__setattr__(self, "lanes", scope)
        return self


class PlaybookSpec(BaseModel):
    playbook_id: str
    version: int = Field(default=1, ge=1)
    name: str
    domain: PlaybookDomain = "kubernetes"
    trigger: TriggerMatch = Field(default_factory=TriggerMatch)
    proof_of_fault: ProofOfFault = Field(default_factory=ProofOfFault)
    steps: list[PlaybookStepSpec] = Field(min_length=1)
    # Trần blast-radius: số workload tối đa playbook được đụng trong 1 lần chạy.
    max_blast_radius: int = Field(default=1, ge=1, le=3)
    # Trạng thái graduation KHỞI ĐIỂM khi seed (runtime state ở PlaybookGovernor).
    initial_graduation: str = GRAD_CANDIDATE
    approved_by: str = ""
    notes: str = ""

    @field_validator("domain", mode="before")
    @classmethod
    def _canon_domain(cls, v: Any) -> Any:
        """Chuẩn hoá khi ĐỌC: PlaybookSpec đã lưu trong Redis từ trước dùng 'k8s'/'os'.

        Không chuẩn hoá ở đây thì mọi spec cũ trong `pbspec:*` sẽ fail validate và
        matcher im lặng không tìm được playbook nào — mất năng lực, không có lỗi nào bật.
        Giá trị lạ vẫn để nguyên cho Literal từ chối (đường ghi phải ồn ào).
        """
        if not isinstance(v, str):
            return v
        canon = normalize_domain(v)
        return canon if canon in CANONICAL_DOMAINS else v

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
