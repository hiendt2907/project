#!/usr/bin/env bash
# Lab: nginx-test — tải trong cluster qua Pod curlimages/curl (song song N vòng curl → Service).
# rakyll/hey trên Docker Hub thường ImagePullBackOff; không dùng làm mặc định.
# kubectl port-forward trên laptop vẫn là legacy (dễ không đạt ngưỡng CPU).
#
# Hai kịch bản E2E:
#   (A) Synthetic (pipeline evidence + RAG/LLM): NS=<ns> SKIP_STRESS=1 bash scripts/nginx_test_cpu_alert_lab.sh
#   (B) CPU thật — tải xong rồi mới POST: NS=<ns> bash scripts/nginx_test_cpu_alert_lab.sh
#   (C) CPU thật — giữ tải trong lúc POST + verify (true alarm, SDK PodMetrics còn cao):
#         NS=<ns> STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15 SLEEP_SEC=60 bash scripts/nginx_test_cpu_alert_lab.sh
#
# Env:
#   STRESS_MODE=curl        # mặc định: Pod curlimages + N worker curl → LOAD_TARGET
#   STRESS_MODE=portforward # legacy: port-forward host + curl
#   CURL_IMAGE=curlimages/curl:8.5.0
#   LOAD_TARGET — default http://nginx-test.<NS>.svc.cluster.local/ from env NS
#   NS=                          **required**
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
if [[ -z "${NS:-}" ]]; then
  echo "nginx_test_cpu_alert_lab.sh: set NS (no default)." >&2
  exit 2
fi
MANIFEST="${ROOT}/scripts/nginx-test-deployment.yaml"
SLEEP_SEC="${SLEEP_SEC:-25}"
STRESS_CPU="${STRESS_CPU:-1}"
STRESS_SEC="${STRESS_SEC:-15}"
SKIP_STRESS="${SKIP_STRESS:-0}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://prometheus.monitor.svc.cluster.local:9090}"
WAIT_PROM_CPU="${WAIT_PROM_CPU:-0}"
WAIT_PROM_CPU_MIN="${WAIT_PROM_CPU_MIN:-0.04}"
PROM_WAIT_SEC="${PROM_WAIT_SEC:-240}"
E2E_EXEC_DEPLOY="${E2E_EXEC_DEPLOY:-omni-prober}"

STRESS_MODE="${STRESS_MODE:-curl}"
LOAD_TARGET="${LOAD_TARGET:-http://nginx-test.${NS}.svc.cluster.local/}"
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.5.0}"
LOAD_CONCURRENCY="${LOAD_CONCURRENCY:-256}"
STRESS_OVERLAP_ALERT="${STRESS_OVERLAP_ALERT:-0}"
OVERLAP_STRESS_SEC="${OVERLAP_STRESS_SEC:-300}"
WARMUP_SEC="${WARMUP_SEC:-10}"
POD_LOAD=""

PF_HOST="${PF_HOST:-127.0.0.1}"
PF_LOCAL_PORT="${PF_LOCAL_PORT:-18080}"

TMP="$(mktemp -d)"
PF_PID=""
cleanup() {
  if [[ -n "${POD_LOAD:-}" ]]; then
    "${KUBE}" delete pod "${POD_LOAD}" -n "${NS}" --ignore-not-found=true --wait=false 2>/dev/null || true
    POD_LOAD=""
  fi
  if [[ "${STRESS_MODE}" == "portforward" ]]; then
    # shellcheck disable=SC2046
    kill $(jobs -p) 2>/dev/null || true
    if [[ -n "${PF_PID:-}" ]]; then
      kill "${PF_PID}" 2>/dev/null || true
      wait "${PF_PID}" 2>/dev/null || true
    fi
  fi
  rm -rf "${TMP}" || true
}
trap cleanup EXIT

echo "=== 1) Apply Deployment + Service nginx-test (${NS}) — CPU limit 50m (see manifest) ==="
"${KUBE}" apply -f "${MANIFEST}"
"${KUBE}" rollout status "deployment/nginx-test" -n "${NS}" --timeout=120s
"${KUBE}" wait --for=condition=Ready pod -n "${NS}" -l app=nginx-test --timeout=180s

POD="$("${KUBE}" get pods -n "${NS}" -l app=nginx-test -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "${POD}" ]]; then
  echo "FAIL: no pod for app=nginx-test" >&2
  exit 1
fi
echo "nginx-test pod: ${POD}"

if [[ "${SKIP_STRESS}" != "1" && "${STRESS_CPU}" == "1" ]]; then
  if [[ "${LOAD_CONCURRENCY}" =~ ^[0-9]+$ ]] && [[ "${LOAD_CONCURRENCY}" -gt 2000 ]]; then
    echo "WARN: LOAD_CONCURRENCY=${LOAD_CONCURRENCY} rất cao — mỗi worker là một tiến trình nền; dễ OOM Pod load (curl). Khuyến nghị 128–512." >&2
  fi
  echo ""
  if [[ "${STRESS_MODE}" == "curl" ]]; then
    if [[ "${STRESS_OVERLAP_ALERT}" == "1" ]]; then
      LOAD_SLEEP="${OVERLAP_STRESS_SEC}"
      echo "=== 2) In-cluster load (OVERLAP alert): ${CURL_IMAGE} — ${LOAD_CONCURRENCY}× curl → ${LOAD_TARGET} (sleep ${LOAD_SLEEP}s, pod giữ chạy đến khi xong bước 3) ==="
    else
      LOAD_SLEEP="${STRESS_SEC}"
      echo "=== 2) In-cluster load: ${CURL_IMAGE} — ${LOAD_CONCURRENCY}× curl loop → ${LOAD_TARGET} (${LOAD_SLEEP}s) ==="
    fi
    POD_LOAD="nginx-load-$(date +%s)"
    export POD_LOAD
    # Heredoc bash: \$ để biến chạy trong container, không expand ở máy gọi kubectl.
    cat <<EOF | "${KUBE}" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${POD_LOAD}
  namespace: ${NS}
  labels:
    app: nginx-load-curl
spec:
  restartPolicy: Never
  containers:
    - name: load
      image: ${CURL_IMAGE}
      command: ["/bin/sh", "-c"]
      args:
        - |
          URL='${LOAD_TARGET}'
          i=0
          while [ \$i -lt ${LOAD_CONCURRENCY} ]; do
            (while true; do curl -fsS -o /dev/null "\$URL" 2>/dev/null || true; done) &
            i=\$((i+1))
          done
          sleep ${LOAD_SLEEP}
          echo load_done
      resources:
        requests:
          cpu: "2"
          memory: 2Gi
        limits:
          cpu: "4"
          memory: 4Gi
EOF
    if [[ "${STRESS_OVERLAP_ALERT}" == "1" ]]; then
      if ! "${KUBE}" wait --for=condition=Ready "pod/${POD_LOAD}" -n "${NS}" --timeout=180s; then
        echo "FAIL: load pod không Ready." >&2
        exit 1
      fi
      echo "=== 2a) Warmup ${WARMUP_SEC}s — để CPU nginx và PodMetrics tăng trước POST alert ==="
      sleep "${WARMUP_SEC}"
    else
      if ! "${KUBE}" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/${POD_LOAD}" -n "${NS}" --timeout="$((STRESS_SEC + 180))s"; then
        echo "FAIL: load pod không Succeeded." >&2
        "${KUBE}" get "pod/${POD_LOAD}" -n "${NS}" -o wide || true
        "${KUBE}" describe "pod/${POD_LOAD}" -n "${NS}" | tail -40 || true
        "${KUBE}" logs "pod/${POD_LOAD}" -n "${NS}" --tail=80 || true
        "${KUBE}" delete pod "${POD_LOAD}" -n "${NS}" --ignore-not-found=true --wait=false 2>/dev/null || true
        exit 1
      fi
      "${KUBE}" logs "pod/${POD_LOAD}" -n "${NS}" --tail=30 || true
      "${KUBE}" delete pod "${POD_LOAD}" -n "${NS}" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
      POD_LOAD=""
    fi
  elif [[ "${STRESS_MODE}" == "portforward" ]]; then
    echo "=== 2) LEGACY port-forward ${PF_HOST}:${PF_LOCAL_PORT} + curl workers (host) ==="
    "${KUBE}" port-forward -n "${NS}" --address "${PF_HOST}" "deploy/nginx-test" "${PF_LOCAL_PORT}:80" &
    PF_PID=$!
    for _ in $(seq 1 40); do
      if curl -fsS -o /dev/null --connect-timeout 1 "http://${PF_HOST}:${PF_LOCAL_PORT}/" 2>/dev/null; then
        echo "tunnel OK"
        break
      fi
      sleep 0.5
    done
    i=0
    while [[ "${i}" -lt "${LOAD_CONCURRENCY}" ]]; do
      (
        while true; do
          curl -fsS -o /dev/null --connect-timeout 2 "http://${PF_HOST}:${PF_LOCAL_PORT}/" 2>/dev/null || true
        done
      ) &
      i=$((i + 1))
    done
    echo "Started ${LOAD_CONCURRENCY} curl workers; waiting ${STRESS_SEC}s..."
    sleep "${STRESS_SEC}"
  else
    echo "FAIL: STRESS_MODE=${STRESS_MODE} (chỉ curl | portforward)" >&2
    exit 1
  fi

  if curl -fsS "${PROMETHEUS_URL}/api/v1/query" \
    --data-urlencode "query=sum(rate(container_cpu_usage_seconds_total{namespace=\"${NS}\",pod=\"${POD}\",container=\"nginx\"}[3m]))" \
    2>/dev/null | head -c 900; then
    echo ""
  else
    echo "(Prometheus instant query skipped or unreachable — check DNS/rbac)"
  fi

  if [[ "${WAIT_PROM_CPU}" == "1" ]]; then
    echo ""
    echo "=== 2b) Poll Prometheus until CPU rate >= ${WAIT_PROM_CPU_MIN} (timeout ${PROM_WAIT_SEC}s) ==="
    deadline=$(( $(date +%s) + ${PROM_WAIT_SEC} ))
    prom_ok=0
    while [[ $(date +%s) -lt $deadline ]]; do
      qry="sum(rate(container_cpu_usage_seconds_total{namespace=\"${NS}\",pod=\"${POD}\",container=\"nginx\"}[3m]))"
      val="$("${KUBE}" exec -n "${NS}" "deploy/${E2E_EXEC_DEPLOY}" -- python3 -c "
import json, sys, urllib.parse, urllib.request
u = '${PROMETHEUS_URL}/api/v1/query?' + urllib.parse.urlencode({'query': '''${qry}'''})
try:
    r = json.loads(urllib.request.urlopen(u, timeout=15).read().decode())
    v = r.get('data', {}).get('result') or []
    if not v:
        print('0')
    else:
        print(v[0].get('value', [None, '0'])[1])
except Exception as e:
    print('0')
" 2>/dev/null || echo "0")"
      if python3 -c "import sys; v=float(sys.argv[1]); sys.exit(0 if v>=float('${WAIT_PROM_CPU_MIN}') else 1)" "${val}" 2>/dev/null; then
        echo "prom_cpu_rate=${val} (>= ${WAIT_PROM_CPU_MIN})"
        prom_ok=1
        break
      fi
      echo "prom_cpu_rate=${val} (waiting...)"
      sleep 10
    done
    if [[ "${prom_ok}" != "1" ]]; then
      echo "WARN: Prometheus chưa thấy CPU rate >= ${WAIT_PROM_CPU_MIN} — vẫn POST để debug."
    fi
  fi
else
  echo "=== 2) SKIP_STRESS — synthetic gateway POST only ==="
fi

ALERT_JSON="${TMP}/alert_cpu90.json"

python3 <<PY
import json
pod = "${POD}"
out = {
    "receiver": "omni-webhook",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {
            "alertname": "HighCPUUsage",
            "severity": "critical",
            "namespace": "${NS}",
            "pod": pod,
            "deployment": "nginx-test",
            "container": "nginx",
        },
        "annotations": {
            "summary": "nginx-test pod CPU ~90% sustained (lab inject)",
            "description": f"Container nginx in pod {pod} CPU utilization ~90% vs 50m limit; investigate load.",
        },
        "startsAt": "2026-04-02T15:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
    }],
    "groupLabels": {},
    "commonLabels": {},
    "commonAnnotations": {},
    "externalURL": "http://alertmanager:9093",
}
with open("${ALERT_JSON}", "w") as f:
    json.dump(out, f, indent=2)
print("wrote", "${ALERT_JSON}")
PY

echo ""
echo "=== 3) POST alert + trace (gateway_alert_loki_verify.sh) ==="
SLEEP_SEC="${SLEEP_SEC}" bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "${ALERT_JSON}"
