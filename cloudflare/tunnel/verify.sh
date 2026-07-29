#!/usr/bin/env bash
# Kiểm chứng mặt public Omni Console. Exit code TRUNG THỰC: bất kỳ gate nào FAIL
# thì script exit 1. Gate cần Internet/Cloudflare mà chưa có thì báo SKIP — SKIP
# KHÔNG được tính là PASS.
#
#   bash cloudflare/tunnel/verify.sh
#
# Đọc kèm: docs/runbooks/cloudflare-public-access.md
set -uo pipefail

DOMAIN="${OMNISRE_DOMAIN:-omnisre.xyz}"
APP_HOST="app.$DOMAIN"
NS="${OMNI_NAMESPACE:-multi-agent}"
TRAEFIK_LB="${TRAEFIK_LB:-192.168.139.2}"
KUBECTL="kubectl -n $NS"

pass=0; fail=0; skip=0
P() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; pass=$((pass+1)); }
F() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
S() { printf '  \033[33mSKIP\033[0m  %s\n' "$*"; skip=$((skip+1)); }
H() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── A. Lab invariance — phải kiểm TRƯỚC, vì đây là thứ tuyệt đối không được vỡ ──
H "A. Lab invariance (provider.ai-agent.local)"

if curl -sf -m 5 -H 'Host: provider.ai-agent.local' "http://$TRAEFIK_LB/" -o /dev/null; then
    P "provider.ai-agent.local vẫn trả UI"
else
    F "provider.ai-agent.local KHÔNG trả UI — lab đã vỡ"
fi

lab_iss="$($KUBECTL get deploy aoip-provider-portal \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="AOIP_OIDC_PROVIDER_ISSUER")].value}' 2>/dev/null)"
if [ "$lab_iss" = "http://dex.ai-agent.local/dex" ]; then
    P "issuer lab giữ nguyên: $lab_iss"
else
    F "issuer lab đã bị đổi thành '$lab_iss' — vi phạm isolation"
fi

dex_iss="$($KUBECTL get cm aoip-dex-config -o jsonpath='{.data.config\.yaml}' 2>/dev/null \
    | awk '/^issuer:/ {print $2; exit}')"
if [ "$dex_iss" = "http://dex.ai-agent.local/dex" ]; then
    P "issuer aoip-dex (ConfigMap lab) giữ nguyên"
else
    F "issuer aoip-dex đã đổi thành '$dex_iss'"
fi

# ── B. Resource isolation ─────────────────────────────────────────────────────
H "B. Resource isolation"

# Phân biệt "chưa deploy" (SKIP) với "deploy rồi nhưng không tách" (FAIL). Báo FAIL
# khi public đơn giản là chưa tồn tại sẽ làm exit code mất ý nghĩa.
for pair in "aoip-dex aoip-dex-public" "aoip-provider-portal aoip-provider-portal-public" \
            "aoip-provider-web aoip-provider-web-public"; do
    set -- $pair
    lab_ok=0; pub_ok=0
    $KUBECTL get deploy "$1" >/dev/null 2>&1 && lab_ok=1
    $KUBECTL get deploy "$2" >/dev/null 2>&1 && pub_ok=1
    if [ "$lab_ok" = 1 ] && [ "$pub_ok" = 1 ]; then
        P "$1 và $2 là hai Deployment riêng"
    elif [ "$pub_ok" = 0 ]; then
        S "$2 chưa deploy"
    else
        F "$1 (lab) biến mất trong khi $2 tồn tại — public đã nuốt mất lab"
    fi
done

# Selector không được chồng lấn — nếu chồng, xoá public sẽ kéo pod lab đi theo.
lab_sel="$($KUBECTL get svc aoip-provider-portal -o jsonpath='{.spec.selector}' 2>/dev/null)"
pub_sel="$($KUBECTL get svc aoip-provider-portal-public -o jsonpath='{.spec.selector}' 2>/dev/null)"
if [ -z "$pub_sel" ]; then
    S "Service public chưa tồn tại — chưa kiểm tra được selector"
elif [ "$lab_sel" != "$pub_sel" ]; then
    P "Service selector tách biệt (lab=$lab_sel public=$pub_sel)"
else
    F "Service selector trùng nhau — xoá public sẽ ảnh hưởng lab"
fi

# ── C. Public plane sống ──────────────────────────────────────────────────────
H "C. Public plane"

if $KUBECTL get secret aoip-dex-public-config >/dev/null 2>&1; then
    P "Secret aoip-dex-public-config tồn tại"
    pub_iss="$($KUBECTL get secret aoip-dex-public-config -o jsonpath='{.data.config\.yaml}' \
        | base64 -d | awk '/^issuer:/ {print $2; exit}')"
    if [ "$pub_iss" = "https://$APP_HOST/dex" ]; then
        P "issuer public đúng: $pub_iss"
    else
        F "issuer public sai: '$pub_iss' (kỳ vọng https://$APP_HOST/dex)"
    fi
else
    S "Secret aoip-dex-public-config chưa được tạo (operator step)"
fi

if curl -sf -m 5 -H "Host: $APP_HOST" "http://$TRAEFIK_LB/dex/.well-known/openid-configuration" \
        2>/dev/null | grep -q "https://$APP_HOST/dex"; then
    P "Dex public phục vụ discovery với issuer public"
else
    S "Dex public chưa trả discovery (chưa deploy hoặc Secret thiếu)"
fi

if curl -sf -m 5 -H "Host: $APP_HOST" "http://$TRAEFIK_LB/" -o /dev/null 2>/dev/null; then
    P "Ingress public trả UI qua Traefik"
else
    S "Ingress public chưa trả UI (chưa apply hoặc pod chưa Ready)"
fi

# ── D. Không có gì bị public ngoài ý muốn ─────────────────────────────────────
H "D. Surface containment"

# Chỉ NodePort/LoadBalancer mới thực sự mở listener ra ngoài. ExternalName chỉ là
# bí danh DNS (ns multi-agent có `ollama-service` kiểu này, trỏ host.orb.internal) —
# tính nó là "public" sẽ cho báo động giả vĩnh viễn.
exposed="$($KUBECTL get svc -o jsonpath='{range .items[*]}{.metadata.name} {.spec.type}{"\n"}{end}' 2>/dev/null \
    | awk '$2=="NodePort" || $2=="LoadBalancer" {print $1"("$2")"}')"
if [ -z "$exposed" ]; then
    P "không Service nào trong $NS mở NodePort/LoadBalancer (Redis/PostgreSQL/Kafka không public)"
else
    F "Service mở ra ngoài cluster: $exposed"
fi

if [ -f "$HOME/.cloudflared/config.yml" ]; then
    if grep -qE '^\s*-\s*service:\s*http_status:404\s*$' "$HOME/.cloudflared/config.yml"; then
        P "tunnel có catch-all http_status:404"
    else
        F "tunnel THIẾU catch-all 404 — hostname lạ sẽ chạm dịch vụ nội bộ"
    fi
    if grep -qE 'hostname:\s*(api|agent)\.' "$HOME/.cloudflared/config.yml"; then
        F "tunnel đang route api./agent. — ngoài phạm vi iteration này"
    else
        P "tunnel không route api./agent."
    fi
else
    S "chưa có ~/.cloudflared/config.yml"
fi

# ── E. Lifecycle ──────────────────────────────────────────────────────────────
H "E. Lifecycle"

if launchctl list 2>/dev/null | grep -q com.omnisre.cloudflared; then
    P "LaunchAgent com.omnisre.cloudflared đã nạp"
else
    S "LaunchAgent chưa cài (bash cloudflare/tunnel/install-macos.sh)"
fi

# ── F. Edge — cần DNS + Cloudflare thật ──────────────────────────────────────
H "F. Cloudflare edge"

# Hỏi resolver CÔNG CỘNG, không dùng resolver hệ thống: DNS của ISP có thể còn cache
# nameserver cũ nhiều giờ sau khi delegation đã đổi, cho âm tính giả (đã cắn thật
# 2026-07-29: 1.1.1.1 và 8.8.8.8 trả Cloudflare trong khi resolver máy vẫn trả registrar cũ).
if dig @1.1.1.1 +short NS "$DOMAIN" 2>/dev/null | grep -qi cloudflare \
   || dig @8.8.8.8 +short NS "$DOMAIN" 2>/dev/null | grep -qi cloudflare; then
    P "NS $DOMAIN đã trỏ Cloudflare (theo resolver công cộng)"

    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$APP_HOST/" 2>/dev/null || echo 000)"
    loc="$(curl -s -o /dev/null -w '%{redirect_url}' -m 10 "https://$APP_HOST/" 2>/dev/null || true)"
    if printf '%s' "$loc" | grep -q cloudflareaccess.com; then
        # Chỉ in team domain. URL challenge đầy đủ chứa JWT `meta` dài — không phải
        # credential, nhưng đổ nguyên vào log/terminal là thói quen xấu và làm output
        # không đọc được.
        P "Access chặn ẩn danh (302 → $(printf '%s' "$loc" | sed -E 's#(https://[^/]+)/.*#\1/…#'))"
    elif [ "$code" = "200" ]; then
        F "https://$APP_HOST/ trả 200 cho ẩn danh — ACCESS CHƯA BẬT"
    else
        S "chưa kết luận được (HTTP $code)"
    fi
else
    S "NS $DOMAIN chưa trỏ Cloudflare — mọi gate edge bị chặn"
fi

# ── Tổng kết ─────────────────────────────────────────────────────────────────
printf '\n\033[1m%d PASS · %d FAIL · %d SKIP\033[0m\n' "$pass" "$fail" "$skip"
[ "$skip" -gt 0 ] && printf 'SKIP nghĩa là CHƯA CHẠY ĐƯỢC, không phải đã đạt.\n'
[ "$fail" -eq 0 ] || exit 1
