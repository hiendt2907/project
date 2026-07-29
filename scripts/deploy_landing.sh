#!/usr/bin/env bash
# Deploy landing page → Cloudflare Pages bằng Direct Upload.
#
#   make deploy-landing
#
# CỐ Ý KHÔNG nối repo vào Cloudflare: repo private chứa manifest RBAC, tên topic
# Kafka, mẫu DSN và lịch sử commit liên quan pentest. Cấp quyền đọc toàn bộ repo cho
# một dịch vụ bên ngoài chỉ để phục vụ 5 file HTML tĩnh là đánh đổi tệ. Direct Upload
# chỉ đẩy đúng thư mục PAGES_DIR.
#
# XÁC THỰC — hai cách, script tự dò theo thứ tự:
#   1. CLOUDFLARE_API_TOKEN trong môi trường
#   2. File token ở ~/.config/cloudflare/omnisre-pages.token (chmod 600)
#   3. Phiên `wrangler login` sẵn có
#
# Vì sao ưu tiên API token hơn `wrangler login`: OAuth của wrangler callback về
# http://localhost:8976 nên PHẢI authorize bằng browser trên chính máy này. Authorize
# ở máy/điện thoại khác sẽ không bao giờ hoàn tất (đã cắn thật 2026-07-29).
set -euo pipefail

PAGES_DIR="${PAGES_DIR:-cloudflare/pages}"
PROJECT="${CF_PAGES_PROJECT:-omnisre}"
TOKEN_FILE="${CF_TOKEN_FILE:-$HOME/.config/cloudflare/omnisre-pages.token}"

die(){ printf '✗ %s\n' "$*" >&2; exit 1; }
ok(){ printf '✓ %s\n' "$*"; }

[ -d "$PAGES_DIR" ] || die "không thấy $PAGES_DIR"

# ── Gate: mọi file trong PAGES_DIR đều được phục vụ CÔNG KHAI ────────────────
# Tài liệu nội bộ lọt vào đây sẽ đọc được từ Internet. Đã suýt xảy ra với
# cloudflare/pages/README.md (nhắc cấu trúc repo + commit hash).
stray="$(find "$PAGES_DIR" -type f \
    \( -name '*.md' -o -name '.DS_Store' -o -name '*.map' -o -name '*.bak' \
       -o -name '.env*' -o -name '*.key' -o -name '*.pem' \) 2>/dev/null || true)"
[ -z "$stray" ] || die "file không nên public nằm trong $PAGES_DIR:
$stray
Mọi file trong thư mục này đều truy cập được từ Internet. Xem cloudflare/PAGES.md."

# Gate: không tài nguyên từ host ngoài (CSP default-src 'none' sẽ chặn im lặng).
ext="$(grep -rohE '(src|href)="https?://[^"]*"' "$PAGES_DIR" --include='*.html' 2>/dev/null \
       | grep -vE 'omnisre\.xyz|github\.com' || true)"
[ -z "$ext" ] || die "tài nguyên từ host ngoài — CSP sẽ chặn im lặng:
$ext"

# Gate: không script (CSP không có script-src; default-src 'none' chặn sạch).
if grep -rqE '<script|\son[a-z]+=' "$PAGES_DIR" --include='*.html' 2>/dev/null; then
    die "phát hiện <script> hoặc inline event handler — CSP sẽ chặn im lặng."
fi
ok "gate nội dung: $(find "$PAGES_DIR" -type f | wc -l | tr -d ' ') file, không có gì bất thường"

# ── Xác thực ─────────────────────────────────────────────────────────────────
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
    ok "dùng CLOUDFLARE_API_TOKEN từ môi trường"
elif [ -f "$TOKEN_FILE" ]; then
    perm="$(stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || stat -c '%a' "$TOKEN_FILE")"
    [ "$perm" = "600" ] || die "$TOKEN_FILE đang là $perm — cần 600. Sửa: chmod 600 $TOKEN_FILE"
    CLOUDFLARE_API_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    export CLOUDFLARE_API_TOKEN
    [ -n "$CLOUDFLARE_API_TOKEN" ] || die "$TOKEN_FILE rỗng"
    ok "dùng token từ $TOKEN_FILE"
else
    printf '! không có API token — thử dùng phiên wrangler login sẵn có\n'
    printf '  Nếu thất bại, tạo token (làm được từ điện thoại):\n'
    printf '    dash.cloudflare.com → My Profile → API Tokens → Create Token\n'
    printf '    → Custom token → Permissions: Account · Cloudflare Pages · Edit\n'
    printf '  Rồi lưu:\n'
    printf '    mkdir -p ~/.config/cloudflare\n'
    printf '    printf %%s "<TOKEN>" > %s\n' "$TOKEN_FILE"
    printf '    chmod 600 %s\n\n' "$TOKEN_FILE"
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
npx --yes wrangler@latest pages deploy "$PAGES_DIR" \
    --project-name="$PROJECT" --branch=main --commit-dirty=true

printf '\nGắn custom domain (một lần, trên Dashboard):\n'
printf '  Workers & Pages → %s → Custom domains → www.omnisre.xyz\n' "$PROJECT"
printf 'Kiểm chứng:\n'
printf '  curl -sI https://www.omnisre.xyz/ | head -1\n'
printf '  curl -sI https://www.omnisre.xyz/vi/ | head -1\n'
