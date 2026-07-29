#!/usr/bin/env bash
# Cài cloudflared thành LaunchAgent chạy lúc đăng nhập macOS.
#
# FAIL CLOSED: mọi điều kiện thiếu đều dừng script, không có giá trị mặc định
# "cho chạy tạm". Thà không public còn hơn public sai cấu hình.
#
#   bash cloudflare/tunnel/install-macos.sh
set -euo pipefail

LABEL="com.omnisre.cloudflared"
CONFIG_PATH="${CLOUDFLARED_CONFIG:-$HOME/.cloudflared/config.yml}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${LABEL}.plist.template"

die() { printf '✗ %s\n' "$*" >&2; exit 1; }
ok()  { printf '✓ %s\n' "$*"; }

# ── 1. Binary ────────────────────────────────────────────────────────────────
# Không hard-code /opt/homebrew: máy Intel dùng /usr/local.
CLOUDFLARED_BIN="$(command -v cloudflared || true)"
[ -n "$CLOUDFLARED_BIN" ] || die "không tìm thấy cloudflared trong PATH. Cài: brew install cloudflared"
ok "cloudflared: $CLOUDFLARED_BIN ($("$CLOUDFLARED_BIN" --version 2>&1 | head -1))"

[ -f "$TEMPLATE" ] || die "thiếu template: $TEMPLATE"

# ── 2. Config ────────────────────────────────────────────────────────────────
[ -f "$CONFIG_PATH" ] || die "thiếu config: $CONFIG_PATH
  Tạo từ mẫu: cp cloudflare/tunnel/config.example.yml $CONFIG_PATH
  rồi thay __CLOUDFLARE_TUNNEL_ID__ và __HOME__."

if grep -q '__CLOUDFLARE_TUNNEL_ID__\|__HOME__' "$CONFIG_PATH"; then
    die "$CONFIG_PATH còn placeholder chưa thay."
fi
ok "config: $CONFIG_PATH"

# ── 3. Credentials + quyền file ──────────────────────────────────────────────
CRED_PATH="$(awk '/^credentials-file:/ {print $2; exit}' "$CONFIG_PATH")"
[ -n "$CRED_PATH" ] || die "config thiếu 'credentials-file:' — named tunnel bắt buộc có."
[ -f "$CRED_PATH" ] || die "không thấy credentials: $CRED_PATH
  Tạo tunnel trước: cloudflared tunnel login && cloudflared tunnel create omnisre"

PERM="$(stat -f '%Lp' "$CRED_PATH")"
if [ "$PERM" != "600" ]; then
    printf '! credentials đang là %s, siết về 600\n' "$PERM"
    chmod 600 "$CRED_PATH"
fi
ok "credentials: $CRED_PATH (600)"

# ── 4. Validate ingress TRƯỚC khi cài ────────────────────────────────────────
# Không cần mạng. Bắt lỗi catch-all thiếu / hostname trùng / service sai cú pháp.
# `--config` là flag TOÀN CỤC, phải đứng TRƯỚC `tunnel`; đặt sau sẽ in help và
# exit 0 — tức là bỏ qua validation một cách im lặng (đã kiểm chứng với 2026.5.0).
"$CLOUDFLARED_BIN" --config "$CONFIG_PATH" tunnel ingress validate \
    || die "ingress config không hợp lệ — không cài."
ok "ingress config hợp lệ"

# ── 5. Sinh plist + nạp ──────────────────────────────────────────────────────
mkdir -p "$PLIST_DIR" "$LOG_DIR"

if launchctl list | grep -q "$LABEL"; then
    printf '! %s đang chạy, gỡ trước khi cài lại\n' "$LABEL"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

sed -e "s|__CLOUDFLARED_BIN__|$CLOUDFLARED_BIN|g" \
    -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$TEMPLATE" > "$PLIST_PATH"
chmod 644 "$PLIST_PATH"
ok "plist: $PLIST_PATH"

launchctl load "$PLIST_PATH"
ok "đã nạp $LABEL"

printf '\nKiểm tra:\n  bash cloudflare/tunnel/verify.sh\n  tail -f %s/omnisre-cloudflared.err.log\n' "$LOG_DIR"
