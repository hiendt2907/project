#!/usr/bin/env bash
set -euo pipefail

# Enable privileged host-level execution profile for Omni executor (shadow mode).
# This script patches the live deployment and verifies nsenter can enter host PID namespaces.

NAMESPACE="${NAMESPACE:-multi-agent}"
DEPLOYMENT="${DEPLOYMENT:-omni-executor}"
VERIFY_POD="${VERIFY_POD:-omni-shadow-root-check}"

echo "[shadow-privileged] namespace=${NAMESPACE} deployment=${DEPLOYMENT}"

./scripts/with_working_kube.sh patch deployment "${DEPLOYMENT}" -n "${NAMESPACE}" --type json -p='[
  {"op":"add","path":"/spec/template/spec/hostPID","value":true},
  {"op":"add","path":"/spec/template/spec/hostNetwork","value":true},
  {"op":"add","path":"/spec/template/spec/hostIPC","value":true},
  {"op":"add","path":"/spec/template/spec/containers/0/securityContext","value":{
    "privileged": true,
    "allowPrivilegeEscalation": true
  }}
]'

./scripts/with_working_kube.sh rollout restart deployment/"${DEPLOYMENT}" -n "${NAMESPACE}"
./scripts/with_working_kube.sh rollout status deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=180s

./scripts/with_working_kube.sh delete pod "${VERIFY_POD}" -n "${NAMESPACE}" --ignore-not-found=true >/dev/null 2>&1 || true

./scripts/with_working_kube.sh apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${VERIFY_POD}
  namespace: ${NAMESPACE}
spec:
  restartPolicy: Never
  hostPID: true
  hostNetwork: true
  hostIPC: true
  containers:
    - name: root-check
      image: multi-agent-system:latest
      imagePullPolicy: Never
      command: ["sh","-lc","sleep 1800"]
      securityContext:
        privileged: true
        runAsUser: 0
        allowPrivilegeEscalation: true
        capabilities:
          add: ["SYS_ADMIN","SYS_PTRACE"]
        seccompProfile:
          type: Unconfined
EOF

./scripts/with_working_kube.sh wait --for=condition=Ready pod/"${VERIFY_POD}" -n "${NAMESPACE}" --timeout=120s
./scripts/with_working_kube.sh exec -n "${NAMESPACE}" "${VERIFY_POD}" -- sh -lc \
  'nsenter -t 1 -m -u -i -n -p -- sh -lc "echo NSENTER_OK && id && hostname && uname -a"'

echo "[shadow-privileged] DONE"
