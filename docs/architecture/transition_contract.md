# Transition Contract

## Required Fields

- `trace_id`
- `transition`
- `status`
- `component`
- `sequence`
- `ts`

## Terminal Contract

- `REQUIRES_HUMAN` requires:
  - tombstone payload
  - DLQ emission
  - cleanup/update of retry/state keys

## Trace Propagation

- Dual propagation:
  - Kafka header: `trace_id`
  - payload field: `trace_id`
- Consumers prefer header, fallback to payload for backward compatibility.