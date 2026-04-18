# SIEM Capability Proof — Operator Runbook

Covers `scripts/prove_siem_capabilities.py` and the `make siem-lab-gate` / `siem-proof-3x` targets.

## What the proof script verifies

| Capability | What is tested |
|---|---|
| **C1 DETECT** | `translate_incident()` produces correct Alertmanager envelope; HITL flag and trace_id propagate from SIEM incident to both `omni-alerts` and `omni-diagnostic-evidence` |
| **C2 DECIDE** | Analyst action plan contains required fields (`trace_id`, `tool_name`, `args`, `reasoning_chain`) |
| **C3 HITL GATE** | Message injected to `omni-hitl-pending` does NOT reach `omni-actions` without approval; HITL API record is created with status `pending` |
| **C4A APPROVE** | POST approve decision → dispatcher routes action to `omni-actions` within 25 s |
| **C4B REJECT** | POST reject decision → feedback on `omni-action-feedback` with `hitl_rejected=true`; nothing on `omni-actions` |
| **C5 FAIL-CLOSED** | When HITL API is unreachable, dispatcher emits auto-reject feedback (`HITL_REGISTER_FAILED`) — mutation is never forwarded |
| **C6 AUDIT** | Redis key `omni:hitl:state:<trace_id>` records final status; HITL API has a queryable decision record |

## Prerequisites

Port-forwards must be active before running:

```bash
kubectl port-forward -n multi-agent svc/kafka 29092:9092 &
kubectl port-forward -n finguard-customer svc/redis 16379:6379 &
kubectl port-forward -n multi-agent svc/redis 19379:6379 &
kubectl port-forward -n finguard-customer svc/finguard-hitl-api 18081:8081 &
```

## Running the proof

```bash
# Single run — artifact written to artifacts/siem_capability_proof_<YYYYMMDD_HHMM>.json
make prove-siem-capabilities

# 3-run flake burn-in — fails fast on first failure
make siem-proof-3x

# Full lab gate (readiness check + proof)
make siem-lab-gate
```

## CAP-5 fault injection — what happens and how to recover

**What it does:** The script patches `finguard-hitl-api` service in `finguard-customer` to forward traffic to port 9999 (no listener), simulating an unreachable HITL API.

**Expected behavior:** The dispatcher catches the connection error and emits an auto-reject to `omni-action-feedback`. No mutation is forwarded to `omni-actions`.

**Automatic restore:** The script restores the service to port 8081 immediately after the feedback is received (or after the 60 s timeout). Verify:

```bash
kubectl get svc finguard-hitl-api -n finguard-customer -o jsonpath='{.spec.ports[0].targetPort}'
# Expected: 8081
curl http://localhost:18081/healthz   # Expected: {"status":"ok"}
```

**Manual restore (if script crashed mid-test):**

```bash
kubectl patch svc finguard-hitl-api -n finguard-customer \
  --type='json' -p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":8081}]'
```

## Rollback reference

See `Makefile` targets `deploy-siem-stack`, `deploy-hitl-api`, and `verify-hitl-production`.
Full rollback commands: `kubectl rollout undo deployment/<name> -n <namespace>`.
