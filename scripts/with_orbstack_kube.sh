#!/usr/bin/env bash
# Alias: dùng cluster nào trả lời được (OrbStack ưu tiên). Xem with_working_kube.sh.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/with_working_kube.sh" "$@"
