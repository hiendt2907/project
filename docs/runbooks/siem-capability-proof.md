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

---

## Lab vs Prod configuration table

| Setting | Lab (OrbStack) | Prod (external cluster) |
|---|---|---|
| kubectl context | `orbstack` | `<prod-context-name>` — must be set via `KUBECONFIG` |
| `finguard-hitl-api` image | `finguard-hitl-api:lab` | `registry.finguard.local:5000/finguard/hitl-api:prod` |
| `AGENT_ENV` | `lab` | `production` |
| `AGENT_POSTGRES_SSLMODE` | `disable` (via AGENT_POSTGRES_SSL_MODE, wrong key — harmless) | `require` (explicit, enforced by Go config validation) |
| `AGENT_POSTGRES_SSLROOTCERT` | not set | not required for `require` mode; needed only for `verify-ca`/`verify-full` |
| ui-api / ui-frontend registry | `docker.io/finguard/` | `registry.finguard.local:5000/finguard/` |
| hitl-dispatcher image | `omni-hitl-dispatcher:latest` | `registry.finguard.local:5000/omni/hitl-dispatcher:<tag>` |
| Postgres TLS | disabled (local dev Postgres, no cert) | TLS required (`sslmode=require`) |
| Token parity check | `bash scripts/rotate_hitl_token.sh` | same script, prod KUBECONFIG context |

---

## Two-track verification matrix

### Track L — Lab / OrbStack regression

Run after every deployment or code change on the OrbStack cluster:

```bash
# Activate port-forwards first
kubectl port-forward -n multi-agent svc/kafka 29092:9092 &
kubectl port-forward -n finguard-customer svc/finguard-hitl-api 18081:8081 &
kubectl port-forward -n multi-agent svc/redis 19379:6379 &
kubectl port-forward -n finguard-customer svc/redis 16379:6379 &

# Gate 1: HITL readiness (12/12)
bash scripts/verify_hitl_production.sh

# Gate 2: Capability proof (7/7) — artifact under artifacts/siem_capability_proof_<YYYYMMDD_HHMM>.json
HITL_TOKEN=$(kubectl get secret hitl-dispatcher-secret -n multi-agent \
  -o jsonpath='{.data.hitl_api_token}' | base64 -d) \
  .venv/bin/python scripts/prove_siem_capabilities.py \
  --out artifacts/siem_capability_proof_lab_$(date +%Y%m%d_%H%M).json
```

Artifact naming convention: `siem_capability_proof_lab_<YYYYMMDD_HHMM>.json`

### Track P — Prod cluster (external)

**Blocker prerequisite: prod cluster kubeconfig is required.** Without it, this track cannot be completed — see NO-GO below.

When prod kubeconfig is available:

```bash
export KUBECONFIG=/path/to/prod-kubeconfig.yaml
PROD_CONTEXT=<prod-context-name>   # e.g. arn:aws:eks:ap-southeast-1:...:cluster/prod

# Step 0: Verify context
kubectl config use-context "$PROD_CONTEXT"
kubectl cluster-info

# Step 1: Pre-deploy snapshot
kubectl get deployment omni-hitl-dispatcher omni-siem-bridge omni-evidence-adapter \
  -n multi-agent -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].image}{"\n"}{end}'

# Step 2: Apply prod overlay (kustomize)
kubectl apply -k smart-siem/customer/k3s/overlays/prod

# Step 3: Apply Omni HITL dispatcher (prod manifest)
kubectl apply -f k8s/deployments/omni-hitl-dispatcher-production.yaml

# Step 4: Verify token parity
bash scripts/rotate_hitl_token.sh   # idempotent if token unchanged; verifies both ns sha256

# Step 5: HITL readiness gate (must be 12/12)
bash scripts/verify_hitl_production.sh

# Step 6: Capability proof against prod
# Set port-forwards for prod endpoints first (same commands, different cluster)
HITL_TOKEN=$(kubectl get secret hitl-dispatcher-secret -n multi-agent \
  -o jsonpath='{.data.hitl_api_token}' | base64 -d) \
  .venv/bin/python scripts/prove_siem_capabilities.py \
  --out artifacts/siem_capability_proof_prod_$(date +%Y%m%d_%H%M).json
```

Artifact naming convention: `siem_capability_proof_prod_<YYYYMMDD_HHMM>.json`

---

## GO / NO-GO for cloud-prod

**GO requires ALL of the following — as of 2026-04-19:**

| Gate | Lab Status | Prod Status |
|---|---|---|
| Track L: verify_hitl_production.sh 12/12 | PASS (2026-04-19) | **NOT RUN — prod cluster access required** |
| Track L: prove_siem_capabilities.py 7/7 | PASS (2026-04-19, artifact: siem_capability_proof_prod_20260419_0751.json) | **NOT RUN** |
| Overlay applies cleanly (`kubectl kustomize`) | PASS after placeholder fix | **NOT VERIFIED on prod cluster** |
| TLS posture: `AGENT_ENV=production` + `AGENT_POSTGRES_SSLMODE=require` | Patch authored (`hitl-api-prod-env.yaml`) | **NOT DEPLOYED** |
| Images pinned (no `:lab`/`:latest` in prod overlay) | kustomization.yaml updated | pending digest pinning via `make print-image-digests` |
| Token parity across namespaces | PASS (sha256[:16]=62a1413df730a43b) | **NOT VERIFIED on prod cluster** |

**Current verdict: NO-GO for external cloud prod.**

Missing prerequisites:
1. `KUBECONFIG` for the prod cluster — not available in this environment.
2. Track P gates not run (proof artifact not generated against prod cluster).
3. Image digests not pinned — `make print-image-digests` must be run after prod build and digest values inserted into `overlays/prod/kustomization.yaml` as `newDigest: sha256:...`.
4. `AGENT_POSTGRES_SSLROOTCERT` path must be confirmed if the prod Postgres uses TLS `verify-ca`/`verify-full` (not needed for `require` mode).

**Next step to close:** Provide prod kubeconfig context name and run Track P gates. All in-repo artifacts and patches are ready.
