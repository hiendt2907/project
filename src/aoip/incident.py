"""Incident Understanding — reasoning sự cố trên Knowledge Graph (EPIC Operate).

Vì sao tồn tại: khách hàng KHÔNG trả tiền để AI biết Postgres ở đâu — họ trả tiền
khi Redis chết và AI tự hiểu chuyện gì đang xảy ra. Đây là lúc graph đã xây trả
tiền: từ một service hỏng, suy ra BLAST RADIUS (ai bị ảnh hưởng) bằng traversal —
KHÔNG GPT, KHÔNG hallucination, chỉ reasoning trên dependency thật.

Vòng: Observe (triệu chứng) → Verify (probe thật: có thật sự down?) → Impact
(blast radius từ graph) → Hypothesis (recovery) → recommend Recovery Mission.
Recovery/mutation là EPIC sau (cần executor + authority) — ở đây CHỈ hiểu sự cố.

KHÔNG noun mới: tái dùng Observation/Finding/Hypothesis/Mission/SystemModel +
UnderstandingContext (Working Memory). INV_FALSIFICATION_FIRST: node khỏe (probe
reachable) KHÔNG bị quy là sự cố, KHÔNG đề xuất recovery (never assume).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from aoip.mission import DoDCheck, Mission, MissionStep, run_mission
from aoip.objects import Finding, Hypothesis, Observation

# Probe sự cố: trả True nếu node CÒN reachable (khỏe), False nếu down. Có thể sync
# hoặc async (vd OrbTransport /dev/tcp thật). Seam tới thế giới thật.
IncidentProbe = Callable[[str], "bool | Awaitable[bool]"]


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


def _incident_plan(failed_node: str, symptom: str, probe: IncidentProbe | None) -> list[MissionStep]:
    src = "incident"

    async def observe_incident(ctx) -> None:
        ctx.observations.append(
            Observation(source=src, scope=ctx.scope, data={"node": failed_node, "symptom": symptom})
        )
        ctx.log("Observe", f"incident on {failed_node}: {symptom}")

    async def verify_failure(ctx) -> None:
        if probe is None:
            ctx.log("Verify", "no probe — cảnh báo chưa kiểm chứng (không tự quy sự cố)")
            return
        reachable = await _maybe_await(probe(failed_node))
        if reachable:
            ctx.findings.append(Finding(
                claim=f"{failed_node} appears HEALTHY (reachable) — không phải sự cố",
                references=(src,), verdict=False, confidence=0.9,
            ))
            ctx.log("Verify", f"{failed_node} reachable → bác bỏ cảnh báo")
        else:
            ctx.findings.append(Finding(
                claim=f"{failed_node} is DOWN (probe failed)",
                references=(src,), verdict=True, confidence=0.95,
            ))
            ctx.log("Verify", f"{failed_node} KHÔNG reachable → xác nhận DOWN")

    def _confirmed_down(ctx) -> bool:
        return any(f.verdict and "DOWN" in f.claim for f in ctx.findings)

    async def assess_impact(ctx) -> None:
        if not _confirmed_down(ctx):
            ctx.log("Assess", "chưa xác nhận DOWN → bỏ qua phân tích blast radius")
            return
        affected = ctx.model.blast_radius(failed_node)
        ctx.findings.append(Finding(
            claim=f"blast radius of {failed_node}: {list(affected)}",
            references=(src,), verdict=True, confidence=0.9,
        ))
        ctx.log("Assess", f"blast radius = {list(affected)} ({len(affected)} service bị ảnh hưởng)")

    async def recommend_recovery(ctx) -> None:
        if not _confirmed_down(ctx):
            return
        affected = ctx.model.blast_radius(failed_node)
        ctx.hypotheses.append(Hypothesis(
            claim=f"recover_service:{failed_node} sẽ khôi phục {list(affected)}",
            predicted_evidence=(f"{failed_node} reachable trở lại", "dependents hết lỗi"),
            prior=0.7, origin="TOPOLOGY",
        ))
        ctx.log("Recommend", f"đề xuất Recovery Mission: recover_service:{failed_node}")

    return [observe_incident, verify_failure, assess_impact, recommend_recovery]


def _incident_dod(probe: IncidentProbe | None) -> list[DoDCheck]:
    def _resolved(ctx) -> bool:
        # Điều tra "xong" khi: đã verify (có Finding kết luận) — dù DOWN hay HEALTHY.
        return any("DOWN" in f.claim or "HEALTHY" in f.claim for f in ctx.findings)

    return [
        ("incident_observed", lambda c: len(c.observations) >= 1),
        ("failure_verified", _resolved),
    ]


async def investigate_incident_mission(
    ctx,
    *,
    failed_node: str,
    symptom: str,
    probe: IncidentProbe | None = None,
    mission_id: str | None = None,
) -> Mission:
    """Chạy 'investigate_incident' như một Mission trên graph tri thức đã có."""
    mission = Mission(
        mission_id=mission_id or f"investigate_incident:{failed_node}",
        goal="investigate_incident",
        scope=ctx.scope,
    )
    return await run_mission(
        mission, ctx,
        plan=_incident_plan(failed_node, symptom, probe),
        dod=_incident_dod(probe),
    )
