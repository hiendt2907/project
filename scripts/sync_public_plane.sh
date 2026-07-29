#!/usr/bin/env bash
# Đồng bộ code local → public plane (app.omnisre.xyz).
#
#   bash scripts/sync_public_plane.sh            # cả UI và backend
#   bash scripts/sync_public_plane.sh --ui       # chỉ Next.js shell
#   bash scripts/sync_public_plane.sh --backend  # chỉ FastAPI console
#   bash scripts/sync_public_plane.sh --with-lab # đồng bộ luôn lab .local
#
# VÌ SAO CẦN SCRIPT NÀY thay vì `kubectl rollout restart`:
# `imagePullPolicy: IfNotPresent` + tag `:latest` ⇒ restart KHÔNG build lại gì cả.
# "deployment successfully rolled out" là tín hiệu giả — pod có thể vẫn chạy image cũ.
# Đã cắn thật với `make deploy-gateway` (commit cc66c4e). Script này build trước, rồi
# **so imageID của pod với image local** sau khi rollout. Đó là bằng chứng thật.
#
# BLAST RADIUS: lab và public dùng chung tag `aoip-provider-web:latest` và
# `multi-agent-system:latest`. Build là image cũ bị thay ngay lập tức, nhưng pod lab
# vẫn giữ bản cũ tới khi nó restart. Mặc định script CHỈ restart public — muốn lab đổi
# theo phải truyền `--with-lab` một cách có ý thức.
set -euo pipefail

NS="${NS:-multi-agent}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DO_UI=0; DO_BACKEND=0; WITH_LAB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --ui)       DO_UI=1 ;;
        --backend)  DO_BACKEND=1 ;;
        --with-lab) WITH_LAB=1 ;;
        -h|--help)  sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) printf '✗ tham số lạ: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done
# Không truyền --ui/--backend ⇒ làm cả hai.
if [ "$DO_UI" = 0 ] && [ "$DO_BACKEND" = 0 ]; then DO_UI=1; DO_BACKEND=1; fi

die() { printf '✗ %s\n' "$*" >&2; exit 1; }
ok()  { printf '✓ %s\n' "$*"; }
step(){ printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

# So imageID pod đang chạy với image local. Đây là gate chống "rollout successful"
# nhưng code cũ vẫn nằm trong pod.
verify_image() {
    local deploy="$1" image="$2"
    local want have i
    want="$(docker image inspect "$image" --format '{{.Id}}' 2>/dev/null)" \
        || die "không tìm thấy image local $image"

    # Chỉ xét pod Running và đòi MỌI pod Running đều đúng image. Dùng `.items[0]` là
    # sai: ngay sau `rollout status`, pod cũ còn ở trạng thái Terminating nên jsonpath
    # có thể trả về nó và cho báo động giả (đã cắn thật ngay lần chạy đầu tiên).
    # Vẫn cần vòng lặp ngắn vì pod cũ mất vài giây mới biến khỏi API.
    for i in 1 2 3 4 5 6 7 8 9 10; do
        have="$(kubectl -n "$NS" get pod -l app="$deploy" \
            --field-selector=status.phase=Running \
            -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' 2>/dev/null \
            | sed 's|^docker://||' | sort -u | grep -v '^$')"
        [ "$have" = "$want" ] && { ok "$deploy chạy đúng image vừa build (${want:7:19}…)"; return 0; }
        sleep 2
    done

    printf '  local : %s\n  pod   : %s\n' "$want" "${have:-<không có pod Running>}" >&2
    die "$deploy KHÔNG chạy image vừa build — rollout là tín hiệu giả"
}

roll() {
    local deploy="$1"
    kubectl -n "$NS" rollout restart "deploy/$deploy" >/dev/null
    kubectl -n "$NS" rollout status "deploy/$deploy" --timeout=180s >/dev/null \
        || die "$deploy rollout thất bại"
}

# ── Build ────────────────────────────────────────────────────────────────────
if [ "$DO_UI" = 1 ]; then
    step "Build aoip-provider-web:latest"
    # Build context PHẢI là ui/ — Dockerfile phụ thuộc workspace monorepo (packages/).
    ( cd "$ROOT/ui" && docker build -q -t aoip-provider-web:latest \
        -f apps/provider-portal/Dockerfile . ) >/dev/null
    ok "image UI đã build"
fi

if [ "$DO_BACKEND" = 1 ]; then
    step "Build multi-agent-system:latest"
    ( cd "$ROOT" && docker build -q -t multi-agent-system:latest -f Dockerfile . ) >/dev/null
    ok "image backend đã build"
fi

# ── Rollout public ───────────────────────────────────────────────────────────
step "Rollout public plane"
[ "$DO_UI" = 1 ]      && { roll aoip-provider-web-public;      verify_image aoip-provider-web-public      aoip-provider-web:latest; }
[ "$DO_BACKEND" = 1 ] && { roll aoip-provider-portal-public;   verify_image aoip-provider-portal-public   multi-agent-system:latest; }

# ── Rollout lab (chỉ khi được yêu cầu rõ ràng) ───────────────────────────────
if [ "$WITH_LAB" = 1 ]; then
    step "Rollout lab (.local) — được yêu cầu bằng --with-lab"
    [ "$DO_UI" = 1 ]      && { roll aoip-provider-web;    verify_image aoip-provider-web    aoip-provider-web:latest; }
    [ "$DO_BACKEND" = 1 ] && { roll aoip-provider-portal; verify_image aoip-provider-portal multi-agent-system:latest; }
else
    step "Lab KHÔNG bị đụng"
    printf '  Lab vẫn chạy image cũ cho tới lần restart tiếp theo của chính nó.\n'
    printf '  Muốn đồng bộ lab: chạy lại với --with-lab\n'
fi

# ── Smoke test ───────────────────────────────────────────────────────────────
step "Smoke test"
TRAEFIK="${TRAEFIK_LB:-192.168.139.2}"

# Retry: sau `rollout status`, Traefik còn mất vài giây để cập nhật endpoint sang pod
# mới — request đầu tiên có thể timeout. `|| true` là bắt buộc: không có nó, `set -e`
# giết script bằng exit code của curl (28) thay vì thông báo đọc được.
http_probe() {
    local host="$1" code i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $host" "http://$TRAEFIK/" || true)"
        [ "$code" = "200" ] && { printf '%s' "$code"; return 0; }
        sleep 3
    done
    printf '%s' "${code:-timeout}"
}

code="$(http_probe app.omnisre.xyz)"
[ "$code" = "200" ] && ok "app.omnisre.xyz trả 200 qua Traefik" || die "app.omnisre.xyz trả $code"

code="$(http_probe provider.ai-agent.local)"
[ "$code" = "200" ] && ok "lab provider.ai-agent.local vẫn 200" || die "lab trả $code — đã làm hỏng lab"

printf '\n\033[1mĐồng bộ xong.\033[0m Kiểm chứng đầy đủ: bash cloudflare/tunnel/verify.sh\n'
