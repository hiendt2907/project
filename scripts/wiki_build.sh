#!/usr/bin/env bash
# Build static site from wiki/ (MkDocs Material). Used locally and in CI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Material for MkDocs: tắt banner đỏ “MkDocs 2.0” khi build (stderr).
export NO_MKDOCS_2_WARNING="${NO_MKDOCS_2_WARNING:-1}"
if [[ "${WIKI_INSTALL_DEPS:-0}" == "1" ]]; then
  if [[ -x "$ROOT/.venv/bin/pip" ]]; then
    "$ROOT/.venv/bin/pip" install -r wiki/requirements-docs.txt
  else
    python3 -m pip install -r wiki/requirements-docs.txt
  fi
fi
if [[ -n "${MKDOCS:-}" ]]; then
  :
elif [[ -x "$ROOT/.venv/bin/mkdocs" ]]; then
  MKDOCS="$ROOT/.venv/bin/mkdocs"
else
  MKDOCS="mkdocs"
fi
# Full repo docs/ embed links to src/, ../reports, etc. — not resolvable in MkDocs; skip --strict unless WIKI_STRICT=1
if [[ "${WIKI_STRICT:-0}" == "1" ]]; then
  exec "$MKDOCS" build -f wiki/mkdocs.yml --strict "$@"
fi
exec "$MKDOCS" build -f wiki/mkdocs.yml "$@"
