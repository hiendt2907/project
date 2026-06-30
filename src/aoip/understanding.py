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
from aoip.objects import Action, Communication, Decision, Fact, Finding, Hypothesis, Observation
from aoip.service_knowledge import expected_ports
from aoip.system_graph import infer_edges
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
    findings: list[Finding] = field(default_factory=list)
    communications: list[Communication] = field(default_factory=list)
    # Decision layer (Operate): phương án + quyết định phục hồi (chưa execute).
    decisions: list[Decision] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    diagnosis_confidence: float | None = None
    recovery_confidence: float | None = None
    requires_approval: bool = False
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


async def infer_topology(ctx: UnderstandingContext) -> None:
    """Suy ra edge quan hệ (proxies_to/depends_on/connects_to) từ hint cấu trúc.

    Edge = Fact quan hệ → đưa vào ctx.facts để model_host gấp chung. Đây là bước
    AI bắt đầu hiểu "hệ thống nối với nhau thế nào", không chỉ liệt kê service.
    """
    obs = ctx.observations[-1]
    edges = infer_edges(obs.data.get("relationships", []), default_evidence=obs.source)
    ctx.facts.extend(edges)
    ctx.log("Hypothesize", f"{len(edges)} topology edge(s) inferred")


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


# ── Reasoning loop: Observe → Expectation → Probe → Compare → Finding ────────
# Expectation = Hypothesis (predicted_evidence); Compare = Finding. KHÔNG noun mới.

async def expect_services(ctx: UnderstandingContext) -> None:
    """Mỗi service quan sát → Expectation từ tri thức tiên nghiệm (Senior prior).

    "Thấy nginx → kỳ vọng 80/443." Expectation chính là Hypothesis với
    predicted_evidence là tập cổng kỳ vọng. Service chưa có tri thức → bỏ qua
    (KHÔNG kỳ vọng giả → không Finding bịa).
    """
    obs = ctx.observations[-1]
    for svc in obs.data.get("services", []):
        name = svc.get("name", "")
        ports = expected_ports(name)
        if not ports:
            continue
        ctx.hypotheses.append(
            Hypothesis(
                claim=f"{name} should expose ports {list(ports)}",
                predicted_evidence=tuple(f"probe_port {p} == open" for p in ports),
                prior=0.8,
                origin="EXPERIENCE",
            )
        )
    ctx.log("Hypothesize", f"{len(ctx.hypotheses)} expectation(s) from prior knowledge")


async def compare_expectations(ctx: UnderstandingContext) -> None:
    """Probe từng cổng kỳ vọng, so sánh thực-tế-vs-kỳ-vọng → Finding.

    MET (cổng mở đúng kỳ vọng) → Finding verdict True + Fact (đã verify).
    UNMET (kỳ vọng mà không mở) → Finding verdict False + Communication (hỏi
    người: hỏng hay cấu hình khác?). Never assume: UNMET KHÔNG sinh Fact.
    """
    obs = ctx.observations[-1]
    src = obs.source
    for svc in obs.data.get("services", []):
        name = svc.get("name", "")
        for port in expected_ports(name):
            reachable = await ctx.backend.probe_port(ctx.host, port)
            if reachable:
                ctx.findings.append(
                    Finding(
                        claim=f"{name} exposes expected port {port}",
                        references=(src,),
                        verdict=True,
                        confidence=0.95,
                    )
                )
                ctx.facts.append(
                    Fact(
                        subject=f"host:{ctx.host}",
                        predicate="exposes_port",
                        obj=str(port),
                        confidence=0.95,
                        provenance=(src, "agent.probe_port"),
                    )
                )
            else:
                ctx.findings.append(
                    Finding(
                        claim=f"{name} EXPECTED port {port} but it is not listening",
                        references=(src,),
                        verdict=False,
                        confidence=0.7,
                    )
                )
                ctx.communications.append(
                    Communication(
                        question=(
                            f"{name} trên {ctx.host} thường mở cổng {port} nhưng tôi "
                            f"không thấy listen. Service lỗi, hay bạn cấu hình khác?"
                        ),
                        scope=ctx.scope,
                        blocking_unknown=f"missing_port:{name}:{port}",
                    )
                )
    met = sum(1 for f in ctx.findings if f.verdict)
    ctx.log("Verify", f"compare: {met}/{len(ctx.findings)} expectation(s) MET → Finding")


async def assess_expectations(ctx: UnderstandingContext) -> None:
    """Đóng vòng theo tỉ lệ kỳ vọng được đáp ứng (met / total) → K↑."""
    total = len(ctx.findings)
    met = sum(1 for f in ctx.findings if f.verdict)
    coverage = 1.0 if total == 0 else met / total
    ctx.capability = assess_knowledge(ctx.capability, coverage)
    ctx.log(
        "Assess",
        f"expectation coverage={coverage:.2f} → K={ctx.capability.dimensions['K']} "
        f"score={ctx.capability.score:.4f} maturity={ctx.capability.maturity.value}",
    )


async def assess_graph(ctx: UnderstandingContext) -> None:
    """Đóng vòng theo độ phân giải của graph: edge nối tới node đã biết / tổng edge.

    Unknown Edge (trỏ tới service chưa quan sát) kéo coverage xuống → AI tự biết
    bức tranh còn lỗ hổng kiến trúc, là nơi câu hỏi thông minh sẽ nhắm vào.
    """
    total = len(ctx.model.edges)
    unknown = len(ctx.model.unknown_edge_targets)
    coverage = 1.0 if total == 0 else max(0.0, (total - unknown) / total)
    ctx.capability = assess_knowledge(ctx.capability, coverage)
    ctx.log(
        "Assess",
        f"graph edges={total} unknown_targets={unknown} coverage={coverage:.2f} "
        f"→ K={ctx.capability.dimensions['K']} maturity={ctx.capability.maturity.value}",
    )


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
