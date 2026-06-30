"""Evidence Completion Engine — exhaust suy luận TRƯỚC khi hỏi người.

Vì sao tồn tại (Slice 4): hiện thực INV_INFER_BEFORE_ASK — "Never ask what can be
inferred." Senior SRE không hỏi ngay mỗi Unknown; họ tự lấp khoảng trống bằng suy
luận, kiểm chứng runtime, đọc tài liệu, hỏi host khác — chỉ hỏi người ở khoảng
trống THỰC SỰ không chứng minh được.

Đối tượng kiến trúc tái dùng (KHÔNG noun mới):
  - Gap (Unknown node) = một Hypothesis ("node X tồn tại & định vị được").
  - Resolver chứng minh được → sinh Fact (provenance = phương pháp, confidence,
    verified_time) — chính là Evidence Graph: mỗi tri thức biết VÌ SAO mình đúng.
  - Không chứng minh được → Communication (câu hỏi tầng kiến trúc).
  - CompletionReport = Derived metric (KPI), không persist (INV_DERIVED_NEVER_PERSIST).

Thang bằng chứng (rẻ→đắt) khóa thứ tự ưu tiên, đóng INV_INFER_BEFORE_ASK:
  INFER → RUNTIME → DOCUMENT → PEER_HOST → (fallback) INTERVIEW.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from aoip.objects import Communication, Fact, Hypothesis
from aoip.system_model import SystemModel

# Prober/registry là seam tới thế giới thật (DNS/socket/k8s/terraform/doc-store…).
# Có thể đồng bộ hoặc bất đồng bộ; engine await kết quả nếu là awaitable.
Prober = Callable[[str], "str | None | Awaitable[str | None]"]


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


class EvidenceResolver(Protocol):
    method: str

    async def resolve(self, node: str, model: SystemModel) -> Fact | None: ...


def _proof(node: str, method: str, detail: str, confidence: float) -> Fact:
    """Fact 'observed_via' (thuộc tính, phi quan hệ) → node trở thành known_node.

    provenance ghi rõ phương pháp+chi tiết → Evidence Graph: tri thức tự giải thích.
    """
    return Fact(
        subject=node,
        predicate="observed_via",
        obj=method,
        confidence=confidence,
        provenance=(f"{method}:{detail}",),
    )


class InferenceResolver:
    """(1) Suy luận thuần từ Fact/graph đã có — không I/O.

    Nếu node đã có tri thức bổ trợ (xuất hiện làm SUBJECT của ≥1 edge: nó CHỦ ĐỘNG
    gọi/đọc thứ khác → tồn tại là hệ quả logic) thì coi như chứng minh được.
    """

    method = "inference"

    async def resolve(self, node: str, model: SystemModel) -> Fact | None:
        outgoing = [e for e in model.edges if e.subject == node]
        if outgoing:
            return _proof(node, self.method, f"{len(outgoing)} corroborating edges", 0.7)
        return None


class RuntimeResolver:
    """(2) Kiểm chứng runtime — DNS/socket/port/k8s lookup qua ``prober``."""

    method = "runtime"

    def __init__(self, prober: Prober) -> None:
        self._prober = prober

    async def resolve(self, node: str, model: SystemModel) -> Fact | None:
        location = await _maybe_await(self._prober(node))
        if location:
            return _proof(node, self.method, str(location), 0.95)
        return None


class DocumentResolver:
    """(3) Tài liệu/runbook đã ingest (metadata) định vị node."""

    method = "document"

    def __init__(self, index: dict[str, str]) -> None:
        self._index = index

    async def resolve(self, node: str, model: SystemModel) -> Fact | None:
        location = self._index.get(node)
        if location:
            return _proof(node, self.method, str(location), 0.8)
        return None


class PeerHostResolver:
    """(4) Hỏi host/agent khác trong tenant (mỗi agent là một sensor)."""

    method = "peer_host"

    def __init__(self, registry: dict[str, str]) -> None:
        self._registry = registry

    async def resolve(self, node: str, model: SystemModel) -> Fact | None:
        location = self._registry.get(node)
        if location:
            return _proof(node, self.method, str(location), 0.85)
        return None


@dataclass(frozen=True)
class CompletionReport:
    """KPI Derived (không persist): bao nhiêu Unknown TỰ giải vs phải hỏi người."""

    total_gaps: int
    resolved: dict[str, str]  # node -> method đã chứng minh
    asked: tuple[str, ...]

    @property
    def resolved_count(self) -> int:
        return len(self.resolved)

    @property
    def asked_count(self) -> int:
        return len(self.asked)

    @property
    def inference_rate(self) -> float:
        """Tỉ lệ Unknown tự lấp được — KPI chính (càng cao càng giống Senior)."""
        return 1.0 if self.total_gaps == 0 else self.resolved_count / self.total_gaps


class EvidenceCompletionEngine:
    """Chạy thang resolver theo thứ tự; dừng ở resolver đầu tiên chứng minh được."""

    def __init__(self, resolvers: list[EvidenceResolver]) -> None:
        self._resolvers = resolvers

    async def resolve_gap(self, node: str, model: SystemModel) -> tuple[Fact, str] | None:
        for resolver in self._resolvers:
            fact = await resolver.resolve(node, model)
            if fact is not None:
                return fact, resolver.method
        return None


def _smart_question(node: str, model: SystemModel) -> str:
    """Câu hỏi TẦNG KIẾN TRÚC: nhắc service tham chiếu + nguồn evidence đã thử."""
    refs = [e for e in model.edges if e.obj == node]
    if refs:
        e = refs[0]
        ref_desc = f"{e.subject} {e.predicate} {node}"
        evidence = ", ".join(sorted({p for r in refs for p in r.provenance}))
        return (
            f"Tôi thấy {ref_desc} (bằng chứng: {evidence}) nhưng đã exhaust "
            f"inference/runtime/document/peer mà chưa định vị được {node}. "
            f"Nó nằm host khác, managed service, hay ngoài phạm vi agent?"
        )
    return f"Tôi phát hiện {node} nhưng không tự xác định được. Bạn xác nhận giúp?"


async def complete_evidence(ctx, engine: EvidenceCompletionEngine) -> CompletionReport:
    """Lấp mọi Unknown edge target qua thang bằng chứng; phần còn lại → câu hỏi.

    Đóng INV_INFER_BEFORE_ASK: Communication chỉ sinh khi MỌI resolver thất bại.
    """
    gaps = sorted(ctx.model.unknown_edge_targets)
    resolved: dict[str, str] = {}
    asked: list[str] = []
    new_facts: list[Fact] = []

    for node in gaps:
        # Gap = Hypothesis cần chứng minh (tái dùng noun, không tạo mới).
        Hypothesis(
            claim=f"{node} exists and is locatable",
            predicted_evidence=("inference", "runtime", "document", "peer_host"),
            prior=0.5,
            origin="TOPOLOGY",
        )
        outcome = await engine.resolve_gap(node, ctx.model)
        if outcome is not None:
            fact, method = outcome
            new_facts.append(fact)
            resolved[node] = method
        else:
            asked.append(node)
            ctx.communications.append(
                Communication(
                    question=_smart_question(node, ctx.model),
                    scope=ctx.scope,
                    blocking_unknown=node,
                )
            )

    if new_facts:
        ctx.facts.extend(new_facts)
        ctx.model = ctx.model.fold(*new_facts)

    report = CompletionReport(total_gaps=len(gaps), resolved=resolved, asked=tuple(asked))
    ctx.log(
        "Assess",
        f"evidence-completion: {report.resolved_count}/{report.total_gaps} tự giải "
        f"(infer-before-ask), {report.asked_count} câu hỏi cho người "
        f"(rate={report.inference_rate:.2f})",
    )
    return report
