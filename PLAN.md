# PLAN.md — Phase 6.2: Zero-Hardcode Remediation

**Status:** DRAFT — awaiting approval  
**Date:** 2026-04-29  
**Author:** autonomous-agent-conductor / claude-sonnet-4-6

---

## 1. Mission

Remove all hardcoded OrbStack ClusterIP addresses and unstable localhost fallbacks from
operational scripts. Replace them with dynamic kubectl-resolved addresses or env-var
driven configuration so the system survives pod restarts without manual IP updates.

**Business Driver:** During Phase 6.1 validation, FinGuard Redis ClusterIP changed from
`192.168.194.64` → `192.168.194.7` after `make deploy-worker`, causing the E2E harness
to fail on Phase 1. The IP was patched manually — this is not acceptable for SRE
automation.

---

## 2. Current Tech Debt Inventory

### CRITICAL — Script breaks on pod restart

| File | Line | Issue |
|------|------|-------|
| `scripts/verify_e2e_crat_pipeline.py` | — | **Resolved:** kubectl ClusterIP resolve + `E2E_*` env overrides; optional `E2E_REDIS_FG_PASSWORD` when Secret unreadable. |
| `scripts/verify_e2e_crat_pipeline.py` | — | Dùng `./scripts/with_working_kube.sh` làm `kubectl` khi nhiều kubeconfig (ghi trong header script). |

### HIGH — Source code with localhost fallback

| File | Line | Issue |
|------|------|-------|
| `src/services/evidence_adapter/worker.py` | — | **Resolved:** bắt buộc `ADAPTER_REDIS_URL` hoặc `OMNI_REDIS_URL` (không default localhost). |

### LOW — Scripts/docs (acceptable for dev tooling)

- `scripts/prove_siem_capabilities.py` — env-var driven, localhost is documented fallback
- `scripts/backfill_playbooks.py` — reads `OMNI_REDIS_URL`, localhost is documented fallback

- `k8s/monitor/*.yaml` — `127.0.0.1` is pod-local sidecar pattern, correct

---

## 3. Proposed Changes

### 3.1 `scripts/verify_e2e_crat_pipeline.py` — Auto-resolve via kubectl + env vars

Replace hardcoded IPs with:
```python
import subprocess

def _resolve_svc_ip(namespace: str, svc_name: str, fallback_env: str) -> str:
    """Resolve ClusterIP via kubectl; fall back to env var."""
    env_val = os.getenv(fallback_env)
    if env_val:
        return env_val
    try:
        ip = subprocess.check_output(
            ["kubectl", "get", "svc", svc_name, "-n", namespace,
             "-o", "jsonpath={.spec.clusterIP}"],
            text=True, timeout=5
        ).strip()
        if ip and ip != "None":
            return ip
    except Exception:
        pass
    raise RuntimeError(f"Cannot resolve {svc_name} in {namespace}. Set {fallback_env}.")
```

For Kafka (not a ClusterIP service — use StatefulSet pod IP):
```bash
KAFKA_BOOTSTRAP = os.getenv(
    "E2E_KAFKA_BOOTSTRAP",
    _resolve_pod_ip("multi-agent", "kafka-0")
)
```

For FinGuard Redis password — read from K8s Secret instead of hardcoding.

### 3.2 `src/services/evidence_adapter/worker.py` — Use env properly

```python
# Before:
_REDIS_URL = os.getenv("ADAPTER_REDIS_URL", "redis://localhost:6379")

# After — no hardcoded fallback in source code:
_REDIS_URL = os.getenv("ADAPTER_REDIS_URL") or os.getenv("OMNI_REDIS_URL")
if not _REDIS_URL:
    raise RuntimeError("ADAPTER_REDIS_URL or OMNI_REDIS_URL must be set")
```

### 3.3 CLAUDE.md — Document new invariant

Add to INVARIANTS section:
```
- `kafka_evidence_loop` uses `auto_offset_reset="earliest"` — analyst catches messages
  that arrive during consumer group rebalance after pod restart.
```

### 3.4 Commit untracked diagnostic scripts

Stage and commit: `scripts/check_connectivity.sh`, `scripts/debug_telegram_flow.sh`,
`scripts/deploy_and_test_advisory.sh`, `k8s/deployments/omni-chaos-secret.yaml`

---

## 4. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `kubectl` not available in CI | Low | High | Env var override path always available |
| FinGuard Redis has no ClusterIP (headless) | Confirmed | Medium | Use pod IP resolver instead |
| `earliest` offset causes duplicate advisories on restart | Low | Low | CRAT chain deduplication is append-only; Telegram sends are idempotent per advisory |
| evidence_adapter breaks if neither env var set | Low | High | K8s deployment already sets `OMNI_REDIS_URL` |

---

## 5. Out of Scope (Phase 6.3+)

- Full service mesh / internal DNS (e.g., `kafka.multi-agent.svc.cluster.local`) — requires
  running scripts inside the cluster
- Kafka topic retention / compaction audit
- HITL dispatcher production hardening

---

## 6. Execution Order

1. Fix `src/services/evidence_adapter/worker.py` (no deploy needed — env already correct)
2. Fix `scripts/verify_e2e_crat_pipeline.py` (test harness only)
3. Update `CLAUDE.md` with new invariant
4. Commit untracked scripts
5. Run `make docker-worker && make deploy-worker` (only needed for 3.2)
6. Run `python scripts/verify_e2e_crat_pipeline.py` to confirm green

---

## 7. Approval Gate

**Awaiting:** User approval to proceed to Task Decomposition (Step 2 of protocol).

Approve as-is, or redirect with changes before execution begins.
