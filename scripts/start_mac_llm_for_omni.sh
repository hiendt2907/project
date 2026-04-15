#!/usr/bin/env bash
# Chạy omni-embed (OpenAI /v1/embeddings) trên Mac :8001 — K8s có thể gọi host.docker.internal:8001.
# vLLM đã gỡ; chat/LLM để Ollama / cấu hình khác (Claude).
#
# Prereqs: ~/omni-embed-m4-native/.venv + deps (fastapi uvicorn sentence-transformers)
# Usage: bash scripts/start_mac_llm_for_omni.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:${PATH}"

_listen() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

if _listen 8001; then
  echo "Port 8001 already in use — skip omni-embed."
  exit 0
fi

if [[ ! -d "${HOME}/omni-embed-m4-native/.venv" ]]; then
  echo "FAIL: missing ${HOME}/omni-embed-m4-native/.venv" >&2
  exit 1
fi

echo "Starting omni-embed on 0.0.0.0:8001 …"
cd "${ROOT}/infra/embed"
# shellcheck disable=SC1090
source "${HOME}/omni-embed-m4-native/.venv/bin/activate"
nohup env PYTHONUNBUFFERED=1 MODEL_ID="${MODEL_ID:-nomic-ai/nomic-embed-text-v1.5}" \
  uvicorn server:app --host 0.0.0.0 --port 8001 \
  >>/tmp/omni-embed-mac.log 2>&1 &
disown || true
echo "  log: /tmp/omni-embed-mac.log"
sleep 2
curl -sf --max-time 2 http://127.0.0.1:8001/health >/dev/null && echo "  embed :8001 OK" || echo "  embed :8001 not ready — see /tmp/omni-embed-mac.log"
exit 0
