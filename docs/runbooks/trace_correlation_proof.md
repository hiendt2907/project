# Trace Correlation Proof

## Objective

Prove the same `trace_id` is observable across gateway and at least three worker roles for one transaction.

## Evidence Sources

- gateway response (`trace_id`)
- worker logs (prober/analyst/executor/core)
- audit stream records
- terminal tombstone or verified success event

## Acceptance

- stage chain complete and ordered
- no mismatch between propagated trace values