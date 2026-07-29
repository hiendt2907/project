#!/usr/bin/env bash
# Gỡ LaunchAgent cloudflared. Đây là BƯỚC 1 của rollback: chạy xong là mặt public
# tắt ngay, trong khi core trên MacBook và lab .local vẫn chạy nguyên vẹn.
#
# KHÔNG xoá credentials, KHÔNG xoá tunnel trên Cloudflare, KHÔNG đụng Kubernetes.
# Những thứ đó là các bước rollback riêng, có chủ đích — xem
# docs/runbooks/cloudflare-public-access.md.
set -euo pipefail

LABEL="com.omnisre.cloudflared"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    printf '✓ đã gỡ %s\n' "$LABEL"
else
    printf '· %s chưa được cài — không có gì để gỡ\n' "$LABEL"
fi

if launchctl list | grep -q "$LABEL"; then
    printf '✗ %s VẪN còn trong launchctl — kiểm tra thủ công\n' "$LABEL" >&2
    exit 1
fi

printf '✓ tunnel đã dừng. Core + lab .local không bị ảnh hưởng.\n'
printf '  Log giữ lại tại: %s/Library/Logs/omnisre-cloudflared.*.log\n' "$HOME"
