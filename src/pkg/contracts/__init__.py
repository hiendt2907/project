"""Canonical cross-lane contracts (Phase 0 of the 0-6 roadmap).

Pure, stdlib-only vocabulary shared by the K8s lane (src/workers/) and the
VM/AOIP lane (src/gateway/, src/aoip/) — same constraint as aoip.protocol
(no Kafka/Redis/LLM/Kubernetes imports here, ever). This package does not
replace any lane's internal representation; it defines a canonical shape
plus lossless adapter functions at the boundary, so both lanes can be
reasoned about with one mental model without a forced rewrite.

Scope note (honesty, not aspiration): as of Phase 0b this package covers
Evidence (three divergent shapes existed: DiagnosticEvidenceDict,
EvidenceItem/AgentEvidenceRequest, EvidenceObject) and CorrelationIdentity
(the tenant/mission/incident/decision/action/command identity fields that
should — but do not yet consistently — appear across all six
Command/CommandResult shapes found in the audit). It does NOT yet unify the
Command/CommandResult transport envelopes themselves (ToolCallPayload,
CommandItem, EnqueueRuntimeCommand, RecoveryRequest, capability typed
payload) — those have genuinely different transport mechanics (fire-and-
forget K8s tool call vs. durable at-least-once RA delivery vs. AOIP
recovery decode) and unifying them is Phase 3's "pick one canonical
capability-dispatch path" work, not this one.
"""
