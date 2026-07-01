"""Authorization — backend-enforced, server-side principal. KHÔNG tin client.

Nguyên tắc cứng (reviewer):
  - Ẩn menu KHÔNG phải authorization. Mọi query/mutation phải được BACKEND enforce.
  - Tenant identity đến từ authenticated server-side context (token → Principal), KHÔNG
    từ tenant_id do browser gửi. Tenant principal bị KHÓA vào đúng 1 tenant.
  - Xem / trả lời câu hỏi / duyệt mutation / đổi policy / xem raw evidence là các quyền
    RIÊNG BIỆT. Provider xem raw tenant evidence KHÔNG mặc định — cần quyền + bị audit.

Đây là Derived runtime policy value, không noun ontology mới.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Permissions (tách biệt, không gộp) ───────────────────────────────────────
P_VIEW = "view"                     # xem summary/timeline
P_ANSWER = "answer_question"        # trả lời architecture question
P_APPROVE = "approve_mutation"      # duyệt Action (bounded approval flow thật)
P_CHANGE_POLICY = "change_policy"   # đổi autonomy tier / gate
P_RAW_EVIDENCE = "access_raw_evidence"  # xem raw tenant evidence (audited)

KIND_PROVIDER = "provider"
KIND_TENANT = "tenant"

_PROVIDER_ROLES = {
    "platform_owner": {P_VIEW, P_ANSWER, P_APPROVE, P_CHANGE_POLICY, P_RAW_EVIDENCE},
    "platform_operator": {P_VIEW, P_APPROVE},
    "support_engineer": {P_VIEW, P_RAW_EVIDENCE},
    "security_auditor": {P_VIEW, P_RAW_EVIDENCE},
    "provider_viewer": {P_VIEW},
}
_TENANT_ROLES = {
    "tenant_owner": {P_VIEW, P_ANSWER, P_APPROVE, P_CHANGE_POLICY, P_RAW_EVIDENCE},
    "sre_lead": {P_VIEW, P_ANSWER, P_APPROVE, P_RAW_EVIDENCE},
    "operator": {P_VIEW, P_ANSWER},
    "approver": {P_VIEW, P_APPROVE},
    "auditor": {P_VIEW, P_RAW_EVIDENCE},
    "viewer": {P_VIEW},
}


@dataclass(frozen=True)
class Principal:
    """Danh tính đã xác thực server-side. tenant=None cho provider (mọi tenant)."""

    subject: str
    kind: str                       # KIND_PROVIDER | KIND_TENANT
    roles: tuple[str, ...]
    tenant: str | None = None       # tenant principal: khóa cứng vào đúng 1 tenant

    @property
    def permissions(self) -> frozenset[str]:
        table = _PROVIDER_ROLES if self.kind == KIND_PROVIDER else _TENANT_ROLES
        out: set[str] = set()
        for r in self.roles:
            out |= table.get(r, set())
        return frozenset(out)

    def can(self, perm: str) -> bool:
        return perm in self.permissions
