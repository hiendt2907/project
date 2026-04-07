#!/usr/bin/env bash
# LEGACY MONOLITH CHAOS SCRIPT: default targets `deployment/omni-worker`.
# Keep only for controlled comparison; split topology should use dedicated prober/core flows.
# Chaos smoke: verify autonomous_decider (Redis manifest → Ollama → allowlist → tool + [AUTONOMOUS_FIX]).
# Optional --god: bật OMNI_GOD_MODE (lab_unchained) + full autonomous_safe_tools như production (có k8s_rollout_restart — chỉ multi-agent).
#
# Safety: CHAOS_CONFIRM=1. Với --god cần thêm CHAOS_GOD_CONFIRM=1.
#
# Usage:
#   CHAOS_CONFIRM=1 ./scripts/chaos_autonomous_smoke.sh
#   CHAOS_CONFIRM=1 CHAOS_GOD_CONFIRM=1 ./scripts/chaos_autonomous_smoke.sh --god
#   CHAOS_CONFIRM=1 ./scripts/chaos_autonomous_smoke.sh --skip-restore
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
K="${K:-${ROOT}/scripts/with_working_kube.sh}"
NS="multi-agent"
CM="omni-worker-config"
CANONICAL_CM="${ROOT}/k8s/deployments/omni-worker-configmap.yaml"
BACKUP="${TMPDIR:-/tmp}/omni-worker-config.${USER:-user}.backup.yaml"

GOD_MODE=0
SKIP_RESTORE=0
for a in "$@"; do
  case "$a" in
    --god) GOD_MODE=1 ;;
    --skip-restore) SKIP_RESTORE=1 ;;
  esac
done

if [[ "${CHAOS_CONFIRM:-}" != "1" ]]; then
  echo "Refusing to run: set CHAOS_CONFIRM=1 (this patches live ConfigMap and restarts omni-worker)."
  exit 2
fi

if [[ "$GOD_MODE" -eq 1 ]]; then
  if [[ "${CHAOS_GOD_CONFIRM:-}" != "1" ]]; then
    echo "Refusing --god: set CHAOS_GOD_CONFIRM=1 (enables OMNI_GOD_MODE + k8s_rollout_restart in allowlist)."
    exit 2
  fi
fi

echo "== Backup $CM -> $BACKUP"
"$K" get configmap "$CM" -n "$NS" -o yaml > "$BACKUP"

cleanup() {
  if [[ "$SKIP_RESTORE" -eq 1 ]]; then
    echo "== Skip restore (--skip-restore)"
    return 0
  fi
  echo "== Restore ConfigMap from repo (god_mode=false + decider off): $CANONICAL_CM"
  if [[ -f "$CANONICAL_CM" ]]; then
    "$K" apply -f "$CANONICAL_CM"
  else
    echo "WARN: missing $CANONICAL_CM — apply backup $BACKUP manually"
    "$K" apply -f "$BACKUP" 2>/dev/null || true
  fi
  "$K" rollout restart deployment/omni-worker -n "$NS" || true
}
trap cleanup EXIT

if [[ "$GOD_MODE" -eq 1 ]]; then
  echo "== GOD chaos: OMNI_GOD_MODE=true + full OMNI_AUTONOMOUS_SAFE_TOOLS (decider 30s, baseline 600s)"
  "$K" patch configmap "$CM" -n "$NS" --type merge -p "$(cat <<'PATCH'
{"data":{
  "OMNI_GOD_MODE":"true",
  "OMNI_AUTONOMOUS_DECIDER_ENABLED":"true",
  "OMNI_AUTONOMOUS_DECIDER_INTERVAL_SEC":"30",
  "OMNI_AUTONOMOUS_SAFE_TOOLS":"k8s_rollout_restart,redis_health,redis_expert_check,sandbox_cleanup",
  "OMNI_BASELINE_SNAPSHOT_INTERVAL_SEC":"600"
}}
PATCH
)"
else
  echo "== Safe chaos: decider + redis_health only + baseline 600s (no god mode)"
  "$K" patch configmap "$CM" -n "$NS" --type merge -p "$(cat <<'PATCH'
{"data":{
  "OMNI_GOD_MODE":"false",
  "OMNI_AUTONOMOUS_DECIDER_ENABLED":"true",
  "OMNI_AUTONOMOUS_DECIDER_INTERVAL_SEC":"30",
  "OMNI_AUTONOMOUS_SAFE_TOOLS":"redis_health",
  "OMNI_BASELINE_SNAPSHOT_INTERVAL_SEC":"600"
}}
PATCH
)"
fi

echo "== Rollout omni-worker"
"$K" rollout restart deployment/omni-worker -n "$NS"
"$K" rollout status deployment/omni-worker -n "$NS" --timeout=180s

echo "== Wait for worker warm-up (scout + loops)"
sleep 25

TS="$(date +%s)"
MANIFEST=$(printf '{"t":%s,"dr":false,"evt":[{"type":"Warning","reason":"ChaosSmoke","message":"injected-chaos-test"}],"z_cpu":null,"z_mem":null,"cpu":0.1,"mem":0.5,"net":{"rx":0,"tx":0},"dsk":{"u":0},"rp":{"c":0,"m":0}}' "$TS")

echo "== Inject omni:baseline:snapshot (redis-cli -x)"
echo -n "$MANIFEST" | "$K" exec -i -n "$NS" deploy/redis -- redis-cli -x SET omni:baseline:snapshot

echo "== Wait for autonomous_decider tick + Ollama (up to 120s)"
sleep 120

echo "== Recent omni-worker logs ([AUTONOMOUS_FIX])"
set +e
"$K" logs -n "$NS" -l app=omni-worker --tail=500 2>&1 | grep '\[AUTONOMOUS_FIX\]'
RC=$?
set -e
if [[ "$RC" -ne 0 ]]; then
  echo "WARN: no [AUTONOMOUS_FIX] lines — decider may not have ticked, Ollama down, or model silent."
  "$K" logs -n "$NS" -l app=omni-worker --tail=120
  exit 1
fi

if [[ "$GOD_MODE" -eq 1 ]]; then
  echo "== GOD chaos done (worker had god_mode + full allowlist during run). Restoring ConfigMap on exit."
else
  echo "== Safe chaos done. Restoring ConfigMap on exit."
fi
