#!/usr/bin/env bash
# Local preview: tắt banner MkDocs 2.0 + dùng cùng validation như mkdocs.yml
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export NO_MKDOCS_2_WARNING="${NO_MKDOCS_2_WARNING:-1}"
if [[ -n "${MKDOCS:-}" ]]; then
  :
elif [[ -x "$ROOT/.venv/bin/mkdocs" ]]; then
  MKDOCS="$ROOT/.venv/bin/mkdocs"
else
  MKDOCS="mkdocs"
fi
exec "$MKDOCS" serve -f wiki/mkdocs.yml "$@"
