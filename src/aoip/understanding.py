"""Understanding context + primitive-impl cho capability ``understand_host``.

Đây là Day-1 SRE behavior của MASTER_PLAN: Observe → Map → Ask. KHÔNG verb mới
(INV_MINIMAL_PRIMITIVES): tái dùng đúng các verb Observe/Hypothesize/Verify/
Assess/Escalate, chỉ khác mission/scope/backend. KHÔNG noun mới: dùng Observation,
Hypothesis, Fact, Communication, SystemModel, CapabilityState đã khai báo.

Vòng tri thức: Observation (raw inventory) → Hypothesis (mỗi service tuyên bố một
cổng) → Verify (probe thật) → Fact (đã verify) → fold SystemModel. Unknown →
Communication (interview), KHÔNG hallucinate (CRITICAL RULE: Never assume).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aoip.capability import CapabilityState, assess_knowledge
from aoip.discovery_backend import HostDiscoveryBackend
from aoip.objects import Communication, Fact, Hypothesis, Observation
from aoip.system_model import SystemModel


@dataclass
class UnderstandingContext:
    """Working Memory cho một mission khám-phá-hiểu (per host)."""

    host: str
    scope: str
    backend: HostDiscoveryBackend
    capability: CapabilityState
    model: SystemModel
    observations: list[Observation] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    communications: list[Communication] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def log(self, verb: str, detail: str) -> None:
        self.trace.append(f"{verb}: {detail}")


async def observe_host(ctx: UnderstandingContext) -> None:
    inventory = await ctx.backend.discover(ctx.host)
    obs = Observation(source="agent.discover", scope=ctx.scope, data=inventory)
    ctx.observations.append(obs)
    n = len(inventory.get("services", []))
    ctx.log("Observe", f"host={ctx.host} services={n} unknowns={inventory.get('unknowns', [])}")


async def hypothesize_services(ctx: UnderstandingContext) -> None:
    """Mỗi service khám phá được = một Hypothesis 'host exposes_port P' (chưa verify)."""
    obs = ctx.observations[-1]
    for svc in obs.data.get("services", []):
        h = Hypothesis(
            claim=f"{ctx.host} exposes_port {svc['port']} ({svc['name']})",
            predicted_evidence=(f"probe_port {svc['port']} == open",),
            prior=0.6,
            origin="OBSERVATION",
        )
        ctx.hypotheses.append(h)
    ctx.log("Hypothesize", f"{len(ctx.hypotheses)} service hypotheses")


async def verify_services(ctx: UnderstandingContext) -> None:
    """Probe thật từng cổng; reachable → Hypothesis trở thành Fact (đã verify)."""
    obs = ctx.observations[-1]
    confirmed = 0
    for svc in obs.data.get("services", []):
        reachable = await ctx.backend.probe_port(ctx.host, svc["port"])
        if not reachable:
            ctx.log("Verify", f"port {svc['port']} ({svc['name']}) KHÔNG reachable → bỏ (no Fact)")
            continue
        ctx.facts.append(
            Fact(
                subject=f"host:{ctx.host}",
                predicate="exposes_port",
                obj=str(svc["port"]),
                confidence=0.95,
                provenance=(obs.source, "agent.probe_port"),
            )
        )
        ctx.facts.append(
            Fact(
                subject=f"host:{ctx.host}",
                predicate="runs_service",
                obj=svc["name"],
                confidence=0.9,
                provenance=(obs.source,),
            )
        )
        confirmed += 1
    ctx.log("Verify", f"{confirmed} service(s) verified → Fact")


async def model_host(ctx: UnderstandingContext) -> None:
    """Fold Fact đã verify vào SystemModel (bất biến)."""
    ctx.model = ctx.model.fold(*ctx.facts)
    ctx.log("Model", f"entities={sorted(ctx.model.entities)} facts={len(ctx.model.facts)}")


async def interview(ctx: UnderstandingContext) -> None:
    """Unknown → Communication cho người. Never assume (CRITICAL RULE)."""
    obs = ctx.observations[-1]
    for unknown in obs.data.get("unknowns", []):
        ctx.communications.append(
            Communication(
                question=f"Tôi không xác định được '{unknown}' trên {ctx.host}. Bạn xác nhận giúp?",
                scope=ctx.scope,
                blocking_unknown=unknown,
            )
        )
    if ctx.communications:
        ctx.log("Escalate", f"{len(ctx.communications)} câu hỏi cho người (interview)")


async def assess_understanding(ctx: UnderstandingContext) -> None:
    """Đóng vòng: coverage = services verified / (verified + unknowns) → K↑."""
    obs = ctx.observations[-1]
    verified_services = len({f.obj for f in ctx.facts if f.predicate == "runs_service"})
    unknowns = len(obs.data.get("unknowns", []))
    denom = verified_services + unknowns
    coverage = 1.0 if denom == 0 else verified_services / denom
    ctx.capability = assess_knowledge(ctx.capability, coverage)
    ctx.log(
        "Assess",
        f"coverage={coverage:.2f} → K={ctx.capability.dimensions['K']} "
        f"score={ctx.capability.score:.4f} maturity={ctx.capability.maturity.value}",
    )
