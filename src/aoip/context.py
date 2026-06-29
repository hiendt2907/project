"""ExecutionContext — Working Memory (ephemeral workspace cho một mission run).

Đây là Working Memory (Knowledge Model §Q3): mutable workspace có chủ đích, chứa
object bất biến tích lũy qua các primitive. Các object lõi vẫn immutable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aoip.backends import ApprovalGate, K8sBackend
from aoip.capability import CapabilityState
from aoip.objects import Action, Decision, Finding, Hypothesis, Observation


@dataclass
class ExecutionContext:
    scope: str
    backend: K8sBackend
    approval: ApprovalGate
    capability: CapabilityState
    namespace: str
    deployment: str
    observations: list[Observation] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    decision: Decision | None = None
    action: Action | None = None
    findings: list[Finding] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def log(self, verb: str, detail: str) -> None:
        self.trace.append(f"{verb}: {detail}")
