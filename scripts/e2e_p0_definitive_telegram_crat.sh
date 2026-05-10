#!/usr/bin/env bash
# P0: gateway inject -> omni advisory -> Telegram getUpdates (strict) -> CRAT phase4 for same trace_id.
# Requires: cluster (Omni + secrets), TELEGRAM from env or secret telegram-bot, Python venv at repo root.
#
# Env:
#   E2E_TELEGRAM_STRICT_GETUPDATES   default 0 (Telegram supergroups usually omit bot-sent messages in getUpdates).
#                                    Set 1 for private chat / channel where updates include the advisory.
#   E2E_TELEGRAM_VERIFY_DELETE_MESSAGE  default 1 (delivery proof via deleteMessage when getUpdates cannot see the text).
#   E2E_OMNI_KUBE_NAMESPACE / E2E_KUBE_NS  passed through to Python CRAT helper
# Usage:
#   bash scripts/e2e_p0_definitive_telegram_crat.sh [alert.json]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export E2E_TELEGRAM_STRICT_GETUPDATES="${E2E_TELEGRAM_STRICT_GETUPDATES:-0}"
export E2E_TELEGRAM_VERIFY_DELETE_MESSAGE="${E2E_TELEGRAM_VERIFY_DELETE_MESSAGE:-1}"
export E2E_ASSERT_TELEGRAM_BOT_API=1
export E2E_TELEGRAM_POLL_SEC="${E2E_TELEGRAM_POLL_SEC:-300}"
export E2E_TELEGRAM_POLL_INTERVAL="${E2E_TELEGRAM_POLL_INTERVAL:-5}"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "FAIL: missing venv python: $PY" >&2
  exit 2
fi

LOGF="$(mktemp "${TMPDIR:-/tmp}/e2e-p0.XXXXXX.log")"
trap 'rm -f "${LOGF}"' EXIT

set +o pipefail
bash "${ROOT}/scripts/e2e_one_alert_full_advisory_path.sh" "${1:-}" 2>&1 | tee "${LOGF}"
rc="${PIPESTATUS[0]}"
set -o pipefail
if [[ "${rc}" -ne 0 ]]; then
  echo "FAIL: advisory path exited ${rc}" >&2
  exit "${rc}"
fi

TRACE="$(grep -E '^trace_id=' "${LOGF}" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
if [[ -z "${TRACE}" ]]; then
  echo "FAIL: could not parse trace_id from log" >&2
  exit 3
fi

echo ""
echo "=== P0 CRAT phase4 for trace=${TRACE} ==="
export PYTHONPATH="${ROOT}/src"
export REPO_ROOT="${ROOT}"
export E2E_P0_TRACE="${TRACE}"
if ! "${PY}" <<'PY'
import asyncio
import os
import sys

ROOT = os.environ["REPO_ROOT"]
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from verify_e2e_crat_pipeline import phase4_crat

async def _run() -> None:
    trace = os.environ["E2E_P0_TRACE"]
    ok = await phase4_crat(trace)
    sys.exit(0 if ok else 1)

asyncio.run(_run())
PY
then
  echo "FAIL: CRAT phase4 for ${TRACE}" >&2
  exit 4
fi

echo ""
echo "P0_OK trace=${TRACE}"
