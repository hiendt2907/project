#!/usr/bin/env bash
# inject_real_fault.sh — Real chaos fault injection via Kubernetes Secret rotation.
#
# Fault: rotates APP_PASSWORD in chaos-pg-secret to a wrong value, then forces
# chaos-victim to restart. The victim runs real psql → "password authentication
# failed for user chaos_app" → exits 1 → CrashLoopBackOff.
#
# Prometheus detects kube_pod_container_status_waiting_reason=CrashLoopBackOff
# after 30s → fires KubePodCrashLoopVictim → Alertmanager → Omni Gateway
# POST /webhook/prometheus → Kafka → Analyst ReAct loop.
#
# Success criteria (NOT executor exit_code alone):
#   1) omni-analyst logs: autonomy_transition transition=VERIFIED_SUCCESS component=autonomous_feedback_loop
#      (post-mutate SDK verify + feedback closed-loop — see autonomous_feedback_loop.py).
#   2) Kubernetes: newest chaos-victim pod reaches Ready (actual workload state).
#
# Usage:
#   bash scripts/inject_real_fault.sh           # inject fault
#   bash scripts/inject_real_fault.sh --restore # undo: restore correct password

set -euo pipefail

NAMESPACE="multi-agent"
DEPLOYMENT="chaos-victim"
SECRET="chaos-pg-secret"

# Correct password (must match what chaos-pg was initialized with)
CORRECT_PASSWORD="chaos-app-pass-2025"
# Wrong password (causes psql auth failure)
WRONG_PASSWORD="STALE-ROTATED-PASSWORD-$(date +%Y%m%d)"

ts()      { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
section() { echo ""; echo "══════════════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════════════"; }
info()    { echo "[$(ts)] $*"; }
b64()     { echo -n "$1" | base64; }

# ── restore mode ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--restore" ]]; then
    section "RESTORE: Reverting chaos-pg-secret to correct password"
    kubectl patch secret "${SECRET}" -n "${NAMESPACE}" \
        --type=merge \
        --patch "{\"data\":{\"APP_PASSWORD\":\"$(b64 "${CORRECT_PASSWORD}")\"}}"
    info "Secret restored: APP_PASSWORD=<correct>"
    kubectl rollout restart deployment/"${DEPLOYMENT}" -n "${NAMESPACE}"
    kubectl rollout status deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=90s
    echo ""
    info "chaos-victim restarted with correct credentials — should return to Running."
    exit 0
fi

# ── pre-fault verification ────────────────────────────────────────────────────
section "PRE-FAULT: Verifying chaos-victim is Running and DB is healthy"

POD_PHASE=$(kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "not found")

if [[ "${POD_PHASE}" != "Running" ]]; then
    echo "ERROR: No Running chaos-victim pod found."
    echo "       Deploy first: kubectl apply -f k8s/chaos-test/victim-app.yaml"
    exit 1
fi

kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim
echo ""
info "Showing last 3 healthy heartbeat lines:"
kubectl logs -n "${NAMESPACE}" -l app=chaos-victim \
    --tail=5 2>/dev/null | grep -E "heartbeat|Starting" | tail -3 || true

# ── inject fault ─────────────────────────────────────────────────────────────
section "FAULT INJECTION: Rotating APP_PASSWORD in Secret '${SECRET}'"
info "Correct password → STALE/wrong (simulates DB password rotation without app update)"
info "Wrong value: ${WRONG_PASSWORD}"

kubectl patch secret "${SECRET}" -n "${NAMESPACE}" \
    --type=merge \
    --patch "{\"data\":{\"APP_PASSWORD\":\"$(b64 "${WRONG_PASSWORD}")\"}}"

info "Secret patched. Forcing pod restart (picks up new PGPASSWORD from Secret)..."
kubectl rollout restart deployment/"${DEPLOYMENT}" -n "${NAMESPACE}"
info "Fault injected at $(ts)"
# Omni verify below only reads logs **after** this fault window (avoids stale SUGGEST_REMEDIATION in tail).
VERIFY_LOGS_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── watch for CrashLoopBackOff ────────────────────────────────────────────────
section "MONITORING: Waiting for CrashLoopBackOff"
echo ""
echo "  New pod will start, read PGPASSWORD='${WRONG_PASSWORD}' from Secret,"
echo "  attempt: psql -U chaos_app -d chaosdb → password authentication failed"
echo "  → exit 1 → CrashLoopBackOff"
echo ""
echo "  Prometheus detects kube_pod_container_status_waiting_reason=CrashLoopBackOff"
echo "  after 30s → KubePodCrashLoopVictim fires → Alertmanager → Omni Gateway."
echo ""

FAULT_CONFIRMED=0
for i in $(seq 1 30); do
    # Find the newest pod (the crashing one from rollout restart)
    REASON=$(kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim \
        --sort-by='.metadata.creationTimestamp' \
        -o jsonpath='{.items[-1].status.containerStatuses[0].state.waiting.reason}' \
        2>/dev/null || echo "")
    RESTARTS=$(kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim \
        --sort-by='.metadata.creationTimestamp' \
        -o jsonpath='{.items[-1].status.containerStatuses[0].restartCount}' \
        2>/dev/null || echo "0")
    NEW_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim \
        --sort-by='.metadata.creationTimestamp' \
        -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || echo "unknown")

    info "[${i}/30] pod=${NEW_POD} waitReason=${REASON:-Running/Init} restarts=${RESTARTS}"

    if [[ "${REASON}" == "CrashLoopBackOff" ]]; then
        echo ""
        echo "  ✓ CrashLoopBackOff CONFIRMED at $(ts)"
        FAULT_CONFIRMED=1
        break
    fi
    sleep 5
done

# ── show pod state and last logs ──────────────────────────────────────────────
section "CURRENT STATE: chaos-victim pods"
kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim -o wide
echo ""
echo "--- Last logs from crashing pod ---"
kubectl logs -n "${NAMESPACE}" -l app=chaos-victim \
    --previous --tail=10 2>/dev/null || \
    kubectl logs -n "${NAMESPACE}" "${NEW_POD:-chaos-victim}" \
    --tail=10 2>/dev/null || true

if [[ "${FAULT_CONFIRMED}" -eq 0 ]]; then
    echo ""
    echo "  ! CrashLoopBackOff not yet confirmed — checking Prometheus alert state:"
fi

# ── check Prometheus alert state ─────────────────────────────────────────────
section "PROMETHEUS: Alert state check"
kubectl exec -n monitor prometheus-0 -- \
    wget -qO- 'http://localhost:9090/api/v1/alerts' 2>/dev/null | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
for a in d.get('data', {}).get('alerts', []):
    if a['labels'].get('alertname') == 'KubePodCrashLoopVictim':
        print(f\"  KubePodCrashLoopVictim state={a.get('state','?')} pod={a['labels'].get('pod','')} reason={a['labels'].get('reason','')}\")
        print(f\"  activeAt={a.get('activeAt','?')}\")
" 2>/dev/null || echo "  (prometheus not reachable from host)"

# ── verify autonomous state machine + cluster ground truth ────────────────────
section "VERIFY: executor fail-fast + analyst VERIFIED_SUCCESS (autonomous_feedback_loop)"

OMNI_PASS=0
SUGGEST_ONLY_SEEN=0
for j in $(seq 1 90); do
    ELOG="$(
        kubectl logs -n "${NAMESPACE}" -l app=omni-executor --since-time="${VERIFY_LOGS_SINCE}" --tail=12000 2>/dev/null || true
    )"
    if echo "${ELOG}" | grep -q 'event=omni_actions_audit_only action=SUGGEST_REMEDIATION (no execute)'; then
        SUGGEST_ONLY_SEEN=1
    fi
    if echo "${ELOG}" | grep -qE '\[.*\] EXECUTE_MUTATE skipped \(auto_execute disabled\)'; then
        echo "  ✗ Omni verification FAILED: EXECUTE_MUTATE skipped (auto_execute disabled)"
        kubectl logs -n "${NAMESPACE}" -l app=omni-executor --since-time="${VERIFY_LOGS_SINCE}" --tail=80 2>/dev/null || true
        exit 1
    fi

    ALOG="$(
        kubectl logs -n "${NAMESPACE}" -l app=omni-analyst --since-time="${VERIFY_LOGS_SINCE}" --tail=25000 2>/dev/null || true
    )"
    # autonomy_contract.emit_transition: transition=VERIFIED_SUCCESS, component=autonomous_feedback_loop
    if echo "${ALOG}" | grep -F 'transition=VERIFIED_SUCCESS' | grep -qF 'component=autonomous_feedback_loop'; then
        OMNI_PASS=1
        break
    fi
    info "[verify ${j}/90] waiting for analyst autonomy_transition VERIFIED_SUCCESS (autonomous_feedback_loop)…"
    sleep 5
done

echo ""
if [[ "${OMNI_PASS}" -ne 1 ]]; then
    if [[ "${SUGGEST_ONLY_SEEN}" -eq 1 ]]; then
        echo "  ✗ Omni verification FAILED: only SUGGEST_REMEDIATION observed (no VERIFIED_SUCCESS)."
        kubectl logs -n "${NAMESPACE}" -l app=omni-executor --since-time="${VERIFY_LOGS_SINCE}" --tail=80 2>/dev/null || true
    fi
    echo "  ✗ Omni verification FAILED: no transition=VERIFIED_SUCCESS for component=autonomous_feedback_loop within timeout"
    echo "    (executor exit_code=0 alone is not sufficient — need closed-loop verify in analyst logs)"
    kubectl logs -n "${NAMESPACE}" -l app=omni-analyst --since-time="${VERIFY_LOGS_SINCE}" --tail=120 2>/dev/null || true
    exit 1
fi

echo "  ✓ Detected autonomy closed-loop: VERIFIED_SUCCESS (autonomous_feedback_loop)"

section "VERIFY C: Kubernetes — chaos-victim pod Ready (actual state machine)"

VIC_POD="$(
    kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim \
        --sort-by='.metadata.creationTimestamp' \
        -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || echo ""
)"
if [[ -z "${VIC_POD}" ]]; then
    echo "  ✗ No chaos-victim pod found after remediation"
    exit 1
fi
info "Waiting for pod/${VIC_POD} condition=Ready (workload healthy after credential restore)…"
if ! kubectl wait --for=condition=Ready "pod/${VIC_POD}" -n "${NAMESPACE}" --timeout=240s 2>/dev/null; then
    echo "  ✗ Pod ${VIC_POD} did not become Ready within 240s — cluster state does not match success"
    kubectl get pods -n "${NAMESPACE}" -l app=chaos-victim -o wide 2>/dev/null || true
    kubectl describe pod -n "${NAMESPACE}" "${VIC_POD}" 2>/dev/null | tail -40 || true
    exit 1
fi
echo "  ✓ Pod ${VIC_POD} is Ready (K8s state matches healthy workload)"

# Analyst / gateway may still show REQUIRES_HUMAN — optional strict check on analyst logs
ALOG="$(
    kubectl logs -n "${NAMESPACE}" -l app=omni-analyst --since-time="${VERIFY_LOGS_SINCE}" --tail=8000 2>/dev/null || true
)"
# Do not match autonomy_transition=REQUIRES_HUMAN — it can appear on mixed paths; require explicit human-escalation copy.
if echo "${ALOG}" | grep -qiE 'escalate_to_human|\[ESCALATED\]'; then
    echo "  ✗ Analyst escalation detected — treating as failure for chaos gate"
    kubectl logs -n "${NAMESPACE}" -l app=omni-analyst --since-time="${VERIFY_LOGS_SINCE}" --tail=60 2>/dev/null || true
    exit 1
fi

section "NEXT: manual follow-up (optional)"
echo "  kubectl logs -n multi-agent -l app=omni-analyst -f --tail=80 2>&1"
echo "  Restore: bash scripts/inject_real_fault.sh --restore"
