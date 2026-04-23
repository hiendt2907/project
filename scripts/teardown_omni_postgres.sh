#!/usr/bin/env bash
# Teardown Omni-side Postgres cluster + PGPool after RAG migration to Redis Stack.
# IDEMPOTENT — safe to re-run. Does NOT touch smart-siem/** Postgres (kept intentionally).
#
# Usage:
#   ./scripts/teardown_omni_postgres.sh          # dry-run (prints what would be deleted)
#   ./scripts/teardown_omni_postgres.sh --apply  # actually delete
set -euo pipefail

APPLY="${1:-}"
NS="multi-agent"

run() {
  if [[ "$APPLY" == "--apply" ]]; then
    echo "+ $*"
    "$@" || true
  else
    echo "DRY: $*"
  fi
}

echo "=== Omni Postgres teardown (ns=${NS}) ==="
echo "Mode: $([[ "$APPLY" == "--apply" ]] && echo APPLY || echo DRY-RUN)"
echo

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach cluster. Aborting." >&2
  exit 1
fi

# 1. Stop workloads that might still have stale connections.
echo "--- 1. Scaling down legacy consumers ---"
run kubectl -n "$NS" scale deploy/omni-worker --replicas=0 --timeout=30s
run kubectl -n "$NS" scale deploy/omni-watchdog --replicas=0 --timeout=30s

# 2. Delete CNPG Cluster CR (cascades to StatefulSet, Pods, Services, PDBs).
echo "--- 2. Deleting CNPG Postgres cluster ---"
run kubectl -n "$NS" delete cluster.postgresql.cnpg.io/omni-postgres --ignore-not-found --timeout=60s

# 3. Delete PGPool gateway + configs.
echo "--- 3. Deleting PGPool gateway ---"
run kubectl -n "$NS" delete deploy/pgpool-gateway svc/pgpool-gateway --ignore-not-found --timeout=30s
run kubectl -n "$NS" delete cm/pgpool-conf --ignore-not-found
run kubectl -n "$NS" delete secret/pgpool-users --ignore-not-found

# 4. Delete CNPG builder job (if still around).
echo "--- 4. Deleting CNPG builder job ---"
run kubectl -n "$NS" delete job/cnpg-builder --ignore-not-found

# 5. Delete Postgres-related secrets (auth, TLS, app creds).
echo "--- 5. Deleting Postgres secrets ---"
for s in omni-postgres-app omni-postgres-superuser omni-postgres-ca omni-postgres-server; do
  run kubectl -n "$NS" delete secret "$s" --ignore-not-found
done

# 6. Delete PVCs (DATA LOSS — intentional, RAG is on Redis now).
echo "--- 6. Deleting Postgres PVCs (DATA LOSS) ---"
for pvc in $(kubectl -n "$NS" get pvc -l cnpg.io/cluster=omni-postgres -o name 2>/dev/null || true); do
  run kubectl -n "$NS" delete "$pvc" --ignore-not-found
done
# Also catch any manually-created pgdata PVC.
for pvc in $(kubectl -n "$NS" get pvc -o name 2>/dev/null | grep -E "pgdata|omni-postgres" || true); do
  run kubectl -n "$NS" delete "$pvc" --ignore-not-found
done

# 7. Remove Prom/Grafana PG rules picked up by provisioning (already removed from repo manifests).
echo "--- 7. Restarting Grafana to re-provision alert rules (drops PG rules) ---"
run kubectl -n monitor rollout restart deploy/grafana

# 8. Scale workers back up.
echo "--- 8. Scaling workloads back up ---"
run kubectl -n "$NS" scale deploy/omni-worker --replicas=1 --timeout=30s
run kubectl -n "$NS" scale deploy/omni-watchdog --replicas=1 --timeout=30s

echo
echo "=== Done. Residual check: ==="
if [[ "$APPLY" == "--apply" ]]; then
  kubectl -n "$NS" get pods,svc,pvc,secret 2>/dev/null | grep -iE "postgres|pgpool|cnpg" || echo "(clean)"
fi

cat <<'NOTE'

NEXT STEPS (manual):
  - Rotate any Postgres password that appeared in git history:
      make secret-history-audit
  - Verify Omni RAG still serves queries via Redis:
      kubectl -n multi-agent exec deploy/omni-worker -- python -c "from rag.redis_vector_store import RedisVectorStore; print('ok')"
  - Smart SIEM Postgres (smart-siem/**) remains untouched by design.
NOTE
