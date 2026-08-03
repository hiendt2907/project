#!/usr/bin/env bash
# Teardown Omni-side Postgres cluster + PGPool. Written when RAG lived on this
# cluster and had migrated to Redis Stack — that justification is STALE. The
# same cluster (cluster.postgresql.cnpg.io/omni-postgres) is now the
# source-of-truth for the omni_admin schema (agent_credential, tenant config,
# autonomy tier, 14+ migrations) — see OMNI_ADMIN_PG_DSN in omni-fullstack/
# omni-gateway. Deleting it deletes that data too. Guarded below: refuses to
# proceed if omni_admin looks live unless --force-data-loss is also passed.
# IDEMPOTENT — safe to re-run. Does NOT touch smart-siem/** Postgres (kept intentionally).
#
# Usage:
#   ./scripts/teardown_omni_postgres.sh                             # dry-run
#   ./scripts/teardown_omni_postgres.sh --apply                     # apply, aborts if omni_admin is live
#   ./scripts/teardown_omni_postgres.sh --apply --force-data-loss    # apply even if omni_admin is live
set -euo pipefail

APPLY="${1:-}"
FORCE="${2:-}"
NS="multi-agent"
PG_POD="${PG_POD:-omni-postgres-0}"
PG_USER="${PG_USER:-omni}"
PG_DB="${PG_DB:-omnidb}"

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

# --- Guard: refuse to delete a cluster that still serves omni_admin ---------
echo "--- 0. Checking whether omni_admin is live on this cluster ---"
if kubectl -n "$NS" get pod "$PG_POD" >/dev/null 2>&1; then
  admin_check="$(kubectl -n "$NS" exec -i "$PG_POD" -- \
    psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "SELECT to_regclass('omni_admin.agent_credential') IS NOT NULL;" 2>/dev/null || echo "")"
  if [[ "$admin_check" == "t" && "$APPLY" == "--apply" && "$FORCE" != "--force-data-loss" ]]; then
    echo "ERROR: omni_admin.agent_credential exists on ${PG_POD} — this cluster is the" >&2
    echo "       source-of-truth for Admin config (tenant registry, agent credentials," >&2
    echo "       autonomy tier), NOT leftover RAG storage. Deleting it now would delete" >&2
    echo "       that data. If you really mean to tear down this cluster, re-run with:" >&2
    echo "         ./scripts/teardown_omni_postgres.sh --apply --force-data-loss" >&2
    echo "       Aborting without changing anything." >&2
    exit 1
  fi
  if [[ "$admin_check" == "t" ]]; then
    echo "WARNING: omni_admin.agent_credential exists — --apply without --force-data-loss will abort."
  else
    echo "omni_admin.agent_credential not found (or pod unreachable) — no live admin data detected."
  fi
else
  echo "${PG_POD} not found in ns=${NS} — nothing to check, nothing to guard against."
fi
echo

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
