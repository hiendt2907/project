#!/usr/bin/env bash
# Đặt lại mật khẩu console public (Dex `staticPasswords`) — an toàn, tự kiểm.
#
#   bash scripts/rotate_dex_public_password.sh
#   bash scripts/rotate_dex_public_password.sh --hash '$2a$10$....'
#
# Vì sao có script này thay vì gõ tay từng bước:
#   1. Mật khẩu/hash KHÔNG được đi qua chỗ thứ ba (chat, ảnh chụp, lịch sử lệnh của
#      người khác). Không có --hash thì script tự đọc mật khẩu bằng `read -s` và tự
#      sinh bcrypt — plaintext không bao giờ nằm trong argv hay history.
#   2. Chép hash bằng mắt là rủi ro thật: bcrypt dùng cả `l/1/I` và `O/0`, đọc sai một
#      ký tự là tự khoá mình ra ngoài mà không có thông báo lỗi nào.
#   3. Chỉ thay ĐÚNG dòng `hash:`. Client secret / userID / email / issuer giữ nguyên —
#      đổi client secret đòi tạo lại CẢ HAI Secret và restart cả hai deployment.
#
# Dex public dùng `storage: memory`: restart làm MẤT mọi phiên OIDC đang dở. Đừng chạy
# lúc có người đang đăng nhập.

set -euo pipefail

NS="${OMNI_NAMESPACE:-multi-agent}"
SECRET="aoip-dex-public-config"
DEPLOY="aoip-dex-public"
HASH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hash) HASH="$2"; shift 2 ;;
        --namespace) NS="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Tham số không hiểu: $1" >&2; exit 1 ;;
    esac
done

command -v kubectl >/dev/null || { echo "thiếu kubectl" >&2; exit 1; }

if [[ -z "$HASH" ]]; then
    command -v htpasswd >/dev/null || {
        echo "thiếu htpasswd (brew install httpd) — hoặc truyền sẵn --hash" >&2; exit 1; }
    # `read -s`: không hiện lên màn hình, không vào history, không vào argv.
    read -rs -p "Mật khẩu console mới: " pw1; echo
    read -rs -p "Nhập lại: " pw2; echo
    [[ "$pw1" == "$pw2" ]] || { echo "Hai lần nhập không khớp." >&2; exit 1; }
    [[ ${#pw1} -ge 12 ]] || { echo "Tối thiểu 12 ký tự — đây là endpoint ra Internet." >&2; exit 1; }
    # `sed s/\$2y/\$2a/`: htpasswd sinh tiền tố $2y, Dex chỉ nhận $2a/$2b.
    HASH="$(htpasswd -bnBC 10 "" "$pw1" | tr -d ':\n' | sed 's/\$2y/\$2a/')"
    unset pw1 pw2
fi

# Kiểm hình dạng TRƯỚC khi ghi: hash hỏng ⇒ Dex vẫn khởi động nhưng không ai đăng
# nhập được, và lỗi không hiện ra ở đâu ngoài lúc thử đăng nhập.
if [[ ! "$HASH" =~ ^\$2[ab]\$[0-9]{2}\$.{53}$ ]]; then
    echo "Hash không đúng dạng bcrypt 60 ký tự (\$2a\$10\$ + 53). Nhận được ${#HASH} ký tự." >&2
    exit 1
fi

echo "▸ Đọc config hiện tại từ Secret $SECRET"
CUR="$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.config\.yaml}' | base64 -d)"
[[ -n "$CUR" ]] || { echo "Secret rỗng hoặc không tồn tại." >&2; exit 1; }

# Mã Python truyền qua `-c`, config truyền qua STDIN. Cố ý KHÔNG dùng `python3 -` với
# heredoc: khi đó cả mã lẫn dữ liệu đều tranh stdin, lần chuyển hướng sau ghi đè lần
# trước và Python nhận CONFIG làm mã nguồn (`SyntaxError: invalid syntax` ở dòng
# `issuer:`). Đã trả giá đúng lỗi này lúc viết script.
_PY_REPLACE_HASH='
import os, re, sys
cur = sys.stdin.read()
new_hash = os.environ["HASH"]
# Thay theo DÒNG có khoá `hash:`, không regex trên cả file: bcrypt chứa `$` và `/`,
# đưa vào phép thay toàn cục rất dễ phá ký tự khác.
out, seen = [], 0
for line in cur.splitlines(keepends=True):
    if re.match(r"^\s*hash:\s*", line):
        indent = re.match(r"^(\s*)", line).group(1)
        out.append(indent + "hash: \"" + new_hash + "\"\n")
        seen += 1
    else:
        out.append(line)
if seen != 1:
    sys.exit("mong doi dung 1 dong hash:, thay %d — dung tay, dung doan" % seen)
sys.stdout.write("".join(out))
'
NEW="$(printf '%s' "$CUR" | HASH="$HASH" python3 -c "$_PY_REPLACE_HASH")"

# Bằng chứng thay đổi đúng phạm vi: chỉ 1 dòng khác, và không phải dòng client secret.
diffcount="$(diff <(printf '%s' "$CUR") <(printf '%s' "$NEW") | grep -c '^[<>]' || true)"
echo "▸ Số dòng thay đổi: $diffcount (mong đợi 2 — một cũ, một mới)"
if [[ "$diffcount" != "2" ]]; then
    echo "Thay đổi ngoài phạm vi mong đợi — DỪNG, không ghi." >&2; exit 1
fi
if diff <(printf '%s' "$CUR") <(printf '%s' "$NEW") | grep -qiE "clientSecret|issuer|redirectURIs"; then
    echo "Diff chạm clientSecret/issuer/redirectURIs — DỪNG, không ghi." >&2; exit 1
fi

echo "▸ Áp Secret"
kubectl -n "$NS" create secret generic "$SECRET" \
    --from-literal=config.yaml="$NEW" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "▸ Restart $DEPLOY (mất phiên OIDC đang dở — đúng thiết kế storage: memory)"
kubectl -n "$NS" rollout restart "deployment/$DEPLOY"
kubectl -n "$NS" rollout status "deployment/$DEPLOY" --timeout=120s

echo "▸ Kiểm chứng"
POD="$(kubectl -n "$NS" get pod -l "app=$DEPLOY" --field-selector=status.phase=Running \
       -o jsonpath='{.items[0].metadata.name}')"
# Hash trong pod phải khớp hash vừa đặt — chứng minh Secret đã tới container, không
# chỉ "apply thành công".
if kubectl -n "$NS" exec "$POD" -- sh -c 'cat /etc/dex/config.yaml 2>/dev/null || cat /etc/dex/cfg/config.yaml 2>/dev/null' \
   | grep -qF "$HASH"; then
    echo "  ✓ hash mới đã có trong pod đang chạy"
else
    echo "  ! không xác nhận được hash trong pod (đường mount config có thể khác) — kiểm tay" >&2
fi

# Thử lại có backoff: `rollout status` trả về khi Pod READY, nhưng Dex mất thêm ~1s để
# mở cổng 5556. Kiểm một lần rồi kết luận sẽ báo FAIL oan trên một lần xoay THÀNH CÔNG
# — và một script báo sai sẽ khiến người vận hành thôi tin nó. Đã trả giá 2026-07-30.
iss=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    iss="$(kubectl -n "$NS" exec "$POD" -- sh -c \
        'wget -qO- http://127.0.0.1:5556/dex/.well-known/openid-configuration 2>/dev/null' \
        2>/dev/null | sed -n 's/.*"issuer":"\([^"]*\)".*/\1/p')"
    [[ -n "$iss" ]] && break
    sleep 2
done
if [[ "$iss" == "https://app.omnisre.xyz/dex" ]]; then
    echo "  ✓ Dex phục vụ discovery, issuer đúng: $iss"
else
    echo "  ! issuer không như mong đợi: '$iss'" >&2; exit 1
fi

echo
echo "Xong. Đăng nhập lại tại https://app.omnisre.xyz (qua Cloudflare Access trước)."
echo "Kiểm chứng đầy đủ mặt public: bash cloudflare/tunnel/verify.sh"
