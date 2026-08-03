#!/usr/bin/env bash
# Restore an omni-postgres pg_dump (custom format, from omni-postgres-backup
# CronJob) into a target database. Task #13.
#
# Two modes:
#   verify (default target) — restores into a THROWAWAY database
#     (omnidb_restore_verify) so you can prove a dump is valid without
#     touching the real omnidb. Safe to run anytime; drops+recreates the
#     throwaway DB each time.
#   disaster-recovery — restores INTO the real omnidb, overwriting current
#     state. Requires --target-db=omnidb AND --i-understand-this-overwrites-omnidb
#     together — same "no landmine" pattern as teardown_omni_postgres.sh.
#
# Usage:
#   ./scripts/restore_omni_postgres.sh <dump-file>                       # dry-run, verify target
#   ./scripts/restore_omni_postgres.sh --apply <dump-file>                # apply, verify target
#   ./scripts/restore_omni_postgres.sh --apply <dump-file> \
#       --target-db=omnidb --i-understand-this-overwrites-omnidb          # apply, REAL restore
#
# List available dumps in the backup PVC first:
#   kubectl -n multi-agent exec deploy/omni-postgres-backup-shell -- ls -la /backup   (see Makefile)
set -euo pipefail

NS="multi-agent"
PG_POD="${PG_POD:-omni-postgres-0}"
PG_USER="${PG_USER:-omni}"
VERIFY_DB="omnidb_restore_verify"

APPLY=""
DUMP_FILE=""
TARGET_DB="$VERIFY_DB"
CONFIRM_OVERWRITE=""

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY="--apply" ;;
    --target-db=*) TARGET_DB="${arg#--target-db=}" ;;
    --i-understand-this-overwrites-omnidb) CONFIRM_OVERWRITE="1" ;;
    *) DUMP_FILE="$arg" ;;
  esac
done

if [[ -z "$DUMP_FILE" ]]; then
  echo "ERROR: missing <dump-file> argument (a filename inside the backup PVC, e.g. omnidb-20260803T020000Z.dump)." >&2
  exit 1
fi

if [[ "$TARGET_DB" != "$VERIFY_DB" && "$CONFIRM_OVERWRITE" != "1" ]]; then
  echo "ERROR: --target-db=${TARGET_DB} targets a non-throwaway database without" >&2
  echo "       --i-understand-this-overwrites-omnidb. Aborting without changing anything." >&2
  echo "       (Omit --target-db entirely to restore into the safe verify DB instead.)" >&2
  exit 1
fi

run() {
  if [[ "$APPLY" == "--apply" ]]; then
    echo "+ $*"
    "$@"
  else
    echo "DRY: $*"
  fi
}

echo "=== omni-postgres restore (ns=${NS}, pod=${PG_POD}) ==="
echo "Mode: $([[ "$APPLY" == "--apply" ]] && echo APPLY || echo DRY-RUN)"
echo "Dump: ${DUMP_FILE}"
echo "Target DB: ${TARGET_DB}$([[ "$TARGET_DB" == "$VERIFY_DB" ]] && echo " (throwaway verify DB)" || echo " (REAL — will overwrite)")"
echo

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach cluster. Aborting." >&2
  exit 1
fi

run_pod_job() {
  # $1=pod name  $2=image  $3=shell command  $4=1 if needs PGPASSWORD env
  local name="$1" image="$2" cmd="$3" needs_pw="${4:-0}"
  kubectl -n "$NS" delete pod "$name" --ignore-not-found >/dev/null 2>&1 || true
  local env_block=""
  if [[ "$needs_pw" == "1" ]]; then
    env_block='      env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: omni-pg-secret
              key: POSTGRES_PASSWORD'
  fi
  cat <<PODYAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${NS}
spec:
  restartPolicy: Never
  containers:
    - name: main
      image: ${image}
      command: ["sh", "-c", "${cmd}"]
${env_block}
      volumeMounts:
        - name: backup
          mountPath: /backup
  volumes:
    - name: backup
      persistentVolumeClaim:
        claimName: omni-postgres-backup-data
PODYAML
  kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$name" --timeout=90s 2>/dev/null \
    || kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Failed pod/"$name" --timeout=30s 2>/dev/null || true
  echo "--- logs: ${name} ---"
  kubectl -n "$NS" logs "$name" 2>&1
  local phase
  phase="$(kubectl -n "$NS" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")"
  kubectl -n "$NS" delete pod "$name" --ignore-not-found >/dev/null 2>&1 || true
  if [[ "$phase" != "Succeeded" ]]; then
    echo "ERROR: ${name} did not succeed (phase=${phase})." >&2
    exit 1
  fi
}

# 1. Confirm the dump file exists in the backup PVC.
echo "--- 1. Checking dump file exists in backup PVC ---"
if [[ "$APPLY" == "--apply" ]]; then
  run_pod_job "omni-pg-restore-check" "busybox:1.36" \
    "test -f /backup/${DUMP_FILE} && ls -la /backup/${DUMP_FILE}"
else
  echo "DRY: check /backup/${DUMP_FILE} exists (busybox pod mounting omni-postgres-backup-data)"
fi

# 2. Drop + recreate the target DB (verify mode) or restore straight into it
#    (real mode, --clean --if-exists already handles existing objects).
echo "--- 2. Restoring ---"
if [[ "$TARGET_DB" == "$VERIFY_DB" ]]; then
  # 2 separate -c statements — DROP/CREATE DATABASE cannot run inside 1 transaction block.
  run kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS ${TARGET_DB};" \
    -c "CREATE DATABASE ${TARGET_DB} OWNER ${PG_USER};"
fi
if [[ "$APPLY" == "--apply" ]]; then
  run_pod_job "omni-pg-restore-apply" "postgres:18" \
    "pg_restore --no-owner --role=${PG_USER} -h omni-postgres.${NS}.svc.cluster.local -U ${PG_USER} -d ${TARGET_DB} --clean --if-exists /backup/${DUMP_FILE}" \
    "1"
else
  echo "DRY: pg_restore /backup/${DUMP_FILE} -> ${TARGET_DB} (postgres:18 pod, PGPASSWORD from omni-pg-secret)"
fi

echo
echo "=== Done. Verify (manual): ==="
cat <<NOTE
  kubectl -n ${NS} exec -i ${PG_POD} -- psql -U ${PG_USER} -d ${TARGET_DB} -tAc \\
    "SELECT count(*) FROM pg_tables WHERE schemaname='omni_admin';"
  # Compare against the source DB's table count before trusting the restore.
NOTE
