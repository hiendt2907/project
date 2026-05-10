#!/usr/bin/env bash
# Omni-compatible /v1/embeddings on 0.0.0.0:8001 — run under launchd (KeepAlive).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
: "${OMNI_REPO_ROOT:?Set OMNI_REPO_ROOT to repo path (e.g. /Users/you/project)}"
cd "${OMNI_REPO_ROOT}/infra/embed"
# shellcheck disable=SC1091
source "${HOME}/omni-embed-m4-native/.venv/bin/activate"
export PYTHONUNBUFFERED=1
export MODEL_ID="${MODEL_ID:-nomic-ai/nomic-embed-text-v1.5}"
exec uvicorn server:app --host 0.0.0.0 --port "${OMNI_EMBED_HTTP_PORT:-8001}"
