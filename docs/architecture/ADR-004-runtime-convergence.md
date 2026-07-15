# ADR-004: Runtime Convergence Without Package Collapse

**Date:** 2026-07-14  
**Status:** Accepted — P0 product foundation

## Context

Omni currently has two substantial Python areas:

| Area | Responsibility | Deployment boundary |
|---|---|---|
| `src/workers/` | Kafka consumers, probes, RAG/LLM reasoning, tool registry, K8s execution, feedback loops | worker image only |
| `src/aoip/` | product/domain model, mission/understanding, agent contracts, control-plane projections and protocol vocabulary | gateway, portal/control-plane, and agent-compatible code |

They are related, but they do not have the same runtime responsibility. The gateway must
remain free of worker/executor imports. The AOIP domain must remain importable by the gateway
without pulling Kafka, Kubernetes, LLM clients or mutating tools. The worker runtime needs the
opposite: access to infrastructure clients and long-running loops.

The repository already has one successful convergence pattern: `aoip.protocol` owns the
command state vocabulary while the Gateway and agent implementations retain their transport
and persistence responsibilities. `ADR-002` is the precedent for this decision.

## Decision

### 1. Do not collapse `src/workers/` and `src/aoip/` into one package

Physical package collapse is rejected. It would:

- break the gateway/worker deployment boundary;
- increase the gateway image and dependency attack surface;
- make pure domain code depend accidentally on infrastructure clients;
- blur ownership between product state and execution mechanics;
- make it harder to run or test the agent/control-plane contract independently.

### 2. Converge them through one product runtime model

`src/aoip/` is the product/domain/control-plane layer. `src/workers/` is the execution
engine. They form one product, but remain separate runtime layers.

The ownership rule is:

```text
AOIP/domain/control plane
    owns product state, contracts, policy vocabulary, mission and projections

Workers/execution engine
    owns infrastructure observation, reasoning loops and mutations

Shared package boundaries
    own pure schemas, protocol contracts, invariants and normalization
```

### 3. New shared contracts belong in `src/pkg/` or an existing pure AOIP contract module

Shared code must be pure and dependency-light. It must not import worker loops, LLM clients,
Kubernetes clients or executor implementations. Existing examples are:

- `src/aoip/protocol/` for command lifecycle vocabulary;
- `src/pkg/reasoning/` for advisory and evidence contracts;
- `src/pkg/onboarding/` for portable onboarding normalization.

No new parallel `runtime`, `protocol`, `control_plane` or `agent_protocol` package is to be
created without a new ADR.

### 4. Integration direction is one-way

Allowed direction:

```text
workers  ──uses──> pure AOIP/pkg contracts
gateway  ──uses──> pure AOIP/pkg contracts
agent    ──uses──> pure AOIP/pkg contracts
```

Forbidden direction:

```text
gateway  ──X──> workers
AOIP domain ──X──> workers
pure contracts ──X──> Kafka/Redis/LLM/Kubernetes clients
```

The worker may consume AOIP state/projections, but AOIP domain code must not call worker
loops directly. Integration belongs in an adapter or orchestration boundary.

### 5. Canonical runtime migration is incremental

The existing worker pipeline remains the production execution engine. AOIP runtime modules
are adopted where they provide product/domain contracts or safer agent lifecycle behavior.
Each migration must have:

- a contract test;
- a compatibility adapter if the existing transport is live;
- a live deployment proof before replacing the old path;
- a removal/sunset criterion for the old implementation.

## Consequences

Positive:

- One product model without one unsafe monolithic package.
- Gateway remains small and fail-closed.
- Worker execution can evolve independently from portal/control-plane UX.
- Agent protocol can be tested without Kafka/LLM/Kubernetes.
- The current ADR-002 command convergence remains valid.

Negative:

- There will be adapters and explicit boundaries during migration.
- Some concepts need a canonical mapping instead of a simple rename.
- The repository will temporarily contain compatibility code.

## Next implementation slices

1. Add architecture boundary regression tests.
2. Define canonical Tenant/Environment/Agent/Mission/Incident/Command mappings.
3. Implement Tenant/Environment lifecycle on the control plane.
4. Move fleet/enrollment reads and writes behind the product control-plane contract.
5. Migrate worker onboarding and incident orchestration through those contracts.

## Rejected alternatives

- **Merge all modules under `src/aoip/`:** rejected because the gateway would inherit worker
  dependencies and execution authority.
- **Move all AOIP code into `src/workers/`:** rejected because the product/control plane would
  become worker-owned and unavailable to the gateway without an unsafe dependency.
- **Create a third parallel runtime package:** rejected because it adds another source of truth.
