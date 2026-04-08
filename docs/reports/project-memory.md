# Project Memory Registry

**Canonical kiến trúc (bám code):** [../vendor/OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md)

## Invariants

- **Action feedback Kafka topic (split):** execution outcomes are published to **`omni-action-feedback`** (settings `kafka_topic_action_feedback`); consumed by **`omni-analyst`** (`kafka_action_feedback_loop`). Not `omni-results` — see canonical doc.
- `EXECUTE_MUTATE` only executes mutate-capable tools; read/query tools must route to `SUGGEST_REMEDIATION`.
- Mutate decisions are fail-closed in `prod` and must keep `trace_id` + auditable `reason_code`.
- Planner output cannot override Proof-of-Fault controls (critical evidence + 3-sigma + observation window).
- Runtime/app config must not ship embedded credentials; DSN defaults stay placeholder-only and secret-injected at runtime.
- Grafana provisioning for Omni monitoring is canonicalized to five dashboards: `Omni Ops`, `Omni Security`, `Omni Learning`, `Omni Pod Resources`, `Omni Node Resources`.
- Advanced self-learning tiers must be zero-impact by default: `OMNI_MULTI_HYPOTHESIS_ENABLED=false`, `OMNI_DEEP_PROBE_ORCHESTRATION_ENABLED=false`, `OMNI_KNOWLEDGE_DRAFT_ENABLED=false`, `OMNI_AUTODOC_GIT_PUSH_ENABLED=false`.
- Incident training execution must be registry-driven (`config/incident_training_matrix.yaml`) and not hardcoded in scattered shell branches.
- Chaos / RAG self-learning lab (banking-safe path B): do not auto-ingest Redis shadow artifacts (`omni:selflearn:shadow:*`) into PGVector; gold dataset for vector ingest only after human **VERIFIED_SUCCESS** and a separate ingest step (`docs/reports/chaos-rag-selflearn-export-ingest.md`).
- Sprint A lab: keep `OMNI_AUTODOC_GIT_PUSH_ENABLED=false`; no automated `git push` for `docs/vendor/knownbase.md` from workers — updates via human PR only.

## FailurePatterns

- Classifier misroute can happen when broad regex rows run before label-constrained rows.
- Planner can emit read-only/hallucinated tools even when JSON shape is valid.
- Single metric spikes are noisy; windowed sigma checks are required before mutation.
- Strict proactive audit can fail in low-noise lab windows (`sigma_gate_ok=false`) even when rollout and contract tests pass.
- Strict trace-stage checks can be timing-sensitive under split topology/log propagation.
- Dashboard drift appears when ConfigMap payload and JSON source files are not synchronized from one canonical set.
- Full matrix can pass while strict audit still fails if lab noise is too low for sigma evidence (`dr=0`, `z=0`) or trace propagation races under proactive checks.
- Missing **Registry** (trace_id ↔ scenario_id) at Matrix run time forces log archaeology and corrupts Learning Delta / labels.
- Redis shadow TTL (24h `setex`) can expire before reviewer export — artifact loss; monitor `ttl_remaining_sec` in exporter output (`scripts/omni_redis_shadow_jsonl_exporter.py`).
- Learning Delta invalid if PGVector baseline or embed model changes between round 1 and round 2.

## ReasonCodes

- Semantic/channel: `ERR_SEM_CHANNEL_MISMATCH`, `ERR_SEM_INVALID_TOOL_TAXONOMY`.
- Governance: `ERR_GOV_NS_OUT_OF_BOUNDS`, `ERR_GOV_UNAUTHORIZED_MUTATION`, `ERR_GOV_ENV_PROD_STRICT`.
- Reasoning/evidence: `ERR_REA_NO_PHYSICAL_PROOF`, `ERR_REA_SIGMA_GATE_BLOCKED`, `ERR_REA_SCHEMA_VIOLATION`, `ERR_REA_HALLUCINATION_DETECTED`.
- Terminal: `SUCCESS_VERIFIED_EVIDENCE`, `ESC_TIMEOUT_TOMBSTONE`, `ESC_MAX_ATTEMPTS_EXCEEDED`.

## Guardrails

- Keep mutate/read-only taxonomy explicit in runtime constants and CI gates.
- Keep classifier regression gate for `ProbeFailureLab` not mapping to `ollama_500_context`.
- Documentation gate blocks incomplete phase records.
- Keep `gitleaks` critical gate for working tree (`--no-git`) and run history scan as separate governance audit target.
- Always classify runtime verify failures explicitly as `infra_blocker` or `logic_blocker` before release messaging.
- Keep non-impact gates enabled in CI (`validate_nonimpact_guards_gate.py`, `validate_learning_loop_gate.py`) before any self-learning tier promotion.

## CrossPhaseConstraints

- Any change touching mutate/classifier/planner must update tests and gates together.
- Every phase report must include `What Changed in System Behavior` and `Memory Applied`.
- Any detected historical secret requires key rotation first, then explicit approval before history rewrite actions.

