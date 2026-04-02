#!/usr/bin/env bash
# Alias: kiểm tra cluster đang dùng (probe tự động). Xem check_kube.sh.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_kube.sh" "$@"
