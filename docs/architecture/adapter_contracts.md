# Adapter Contracts

## Interfaces

- `IngressAdapter`
- `ProbeAdapter`
- `PlannerAdapter`
- `ActuatorAdapter`
- `VerifierAdapter`

## Contract Models

- `AdapterEvent`
- `AdapterPlan`
- `AdapterExecutionResult`

## Compatibility Rules

- Core state-machine semantics stay unchanged across adapters.
- Adapter implementation can vary, but terminal outcome contract is fixed.