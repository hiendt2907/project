#!/usr/bin/env bash
# Cập nhật A record *.omnisre.xyz trỏ về IP ngoài hiện tại của VM GCP `omni-k3s-vm`.
#
# Vì sao cần: IP ngoài của VM là ephemeral (`external-nat`, không reserve static),
# nên mỗi lần VM restart là DNS trỏ vào IP chết → toàn bộ provider/dex/gateway 000.
#
# Token: KHÔNG dán vào chat/commit. Đặt ở file 600:
#   printf %s '<TOKEN>' > ~/.config/cloudflare/omnisre-dns.token && chmod 600 $_
# Token cần quyền: Zone → DNS → Edit trên zone `omnisre.xyz`.
#
# Dùng:  bash scripts/cf_update_gcp_dns.sh          # xem trước, không đổi gì
#        bash scripts/cf_update_gcp_dns.sh --apply  # ghi thật
set -euo pipefail

ZONE_NAME="${CF_ZONE_NAME:-omnisre.xyz}"
TOKEN_FILE="${CF_DNS_TOKEN_FILE:-$HOME/.config/cloudflare/omnisre-dns.token}"
GCP_VM="${GCP_VM:-omni-k3s-vm}"
GCP_ZONE="${GCP_ZONE:-asia-southeast1-c}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

die() { printf '✗ %s\n' "$*" >&2; exit 1; }
ok()  { printf '✓ %s\n' "$*"; }

# --- token ---
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
    TOKEN="$CLOUDFLARE_API_TOKEN"; ok "token từ CLOUDFLARE_API_TOKEN"
elif [ -f "$TOKEN_FILE" ]; then
    perm="$(stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || stat -c '%a' "$TOKEN_FILE")"
    [ "$perm" = "600" ] || die "$TOKEN_FILE đang là $perm — cần 600: chmod 600 $TOKEN_FILE"
    TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
    [ -n "$TOKEN" ] || die "$TOKEN_FILE rỗng"
    ok "token từ $TOKEN_FILE"
else
    die "không có token. Tạo ở dash.cloudflare.com → My Profile → API Tokens (Zone:DNS:Edit), rồi:
    printf %%s '<TOKEN>' > $TOKEN_FILE && chmod 600 $TOKEN_FILE"
fi

cf() { curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }
API=https://api.cloudflare.com/client/v4

# --- IP thật của VM (nguồn sự thật, không hardcode) ---
IP="$(gcloud compute instances describe "$GCP_VM" --zone "$GCP_ZONE" \
        --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "không lấy được IP VM (got: '$IP')"
ok "IP hiện tại của $GCP_VM = $IP"

ZONE_ID="$(cf "$API/zones?name=$ZONE_NAME" | python3 -c \
  'import sys,json;d=json.load(sys.stdin);r=d.get("result") or [];print(r[0]["id"] if r else "")')"
[ -n "$ZONE_ID" ] || die "không đọc được zone $ZONE_NAME — token thiếu quyền Zone:DNS:Read?"
ok "zone $ZONE_NAME"

# --- A record cần đồng bộ: mọi A record DNS-only trỏ vào hạ tầng GCP ---
# Bỏ qua record proxied (omnisre.xyz/www → Cloudflare Pages, độc lập với GCP).
# NOTE: không pipe curl vào `python3 - <<PY` được — heredoc đã chiếm stdin. Ghi ra file tạm.
RECORDS_JSON="$(mktemp)"; trap 'rm -f "$RECORDS_JSON"' EXIT
cf "$API/zones/$ZONE_ID/dns_records?type=A&per_page=100" > "$RECORDS_JSON"

python3 - "$IP" "$APPLY" "$ZONE_ID" "$TOKEN" "$RECORDS_JSON" <<'PY'
import json, sys, urllib.request

with open(sys.argv[5]) as fh:
    data = json.load(fh)
if not data.get("success"):
    sys.exit(f"✗ list DNS lỗi: {data.get('errors')}")
ip, apply_, zone_id, token = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4]

todo = []
for r in data["result"]:
    if r["proxied"]:
        print(f"  skip  {r['name']:30} (proxied — Cloudflare Pages, không phải GCP)")
    elif r["content"] == ip:
        print(f"  ok    {r['name']:30} đã đúng {ip}")
    else:
        todo.append(r)
        print(f"  ĐỔI  {r['name']:30} {r['content']} → {ip}")

if not todo:
    print("\nKhông có gì phải đổi.")
    sys.exit(0)
if not apply_:
    print(f"\n(xem trước) {len(todo)} record sẽ đổi. Chạy lại với --apply để ghi thật.")
    sys.exit(0)

for r in todo:
    body = json.dumps({"type": "A", "name": r["name"], "content": ip,
                       "ttl": r["ttl"], "proxied": False}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{r['id']}",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        res = json.load(resp)
    print(("  ✓ " if res.get("success") else "  ✗ ") + r["name"] +
          ("" if res.get("success") else f" {res.get('errors')}"))
PY
