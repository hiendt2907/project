#!/usr/bin/env bash
# Chạy Ở NHÀ trước khi đi cafe demo — không chạy lần đầu ngoài quán, vì nếu
# thiếu 1 trong các điều kiện dưới đây (Tailscale rớt, VM tắt, agent lệch
# heartbeat...) thì demo trực tiếp sẽ đứng hình trước mặt CTO. Script này
# CHỈ kiểm tra, không sửa gì — thấy FAIL thì tự xử lý rồi chạy lại.
set -uo pipefail

FAIL=0
ok()   { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; FAIL=1; }

echo "── 1. OrbStack VM lab ──────────────────────────────────────────"
for vm in cust-app cust-edge cust-db; do
  state=$(orb list 2>/dev/null | awk -v v="$vm" '$1==v{print $2}')
  if [ "$state" = "running" ]; then ok "$vm running"; else bad "$vm KHÔNG running (state=$state)"; fi
done

echo "── 2. aoip-agent.service trên cả 3 VM ──────────────────────────"
for vm in cust-app cust-edge cust-db; do
  st=$(orb -m "$vm" systemctl is-active aoip-agent 2>/dev/null || echo "unknown")
  if [ "$st" = "active" ]; then ok "$vm: aoip-agent active"; else bad "$vm: aoip-agent = $st"; fi
done

echo "── 3. payment-api.service trên cust-app (mục tiêu demo) ────────"
st=$(orb -m cust-app systemctl is-active payment-api 2>/dev/null || echo "unknown")
if [ "$st" = "active" ]; then ok "payment-api active (sẵn sàng để dừng khi demo)"; else bad "payment-api = $st — cần start lại trước"; fi

echo "── 4. kubectl reach cluster GCP qua Tailscale ──────────────────"
if kubectl get ns multi-agent >/dev/null 2>&1; then
  ok "kubectl context sống, namespace multi-agent thấy được"
else
  bad "kubectl KHÔNG kết nối được cluster — kiểm tra Tailscale"
fi

echo "── 5. Pod Omni đang chạy (đúng, không CrashLoop) ───────────────"
ready=$(kubectl get deploy omni-fullstack -n multi-agent -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
if [ "${ready:-0}" -ge 1 ]; then ok "omni-fullstack ready=$ready"; else bad "omni-fullstack chưa ready"; fi
# omni-gateway là Argo Rollout, không phải Deployment thường (CLAUDE.md) — field khác.
ready=$(kubectl get rollout omni-gateway -n multi-agent -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
if [ "${ready:-0}" -ge 1 ]; then ok "omni-gateway (rollout) ready=$ready"; else bad "omni-gateway (rollout) chưa ready"; fi

echo "── 6. Cả 3 agent heartbeat gần đây (< 60s) ─────────────────────"
kubectl exec -n multi-agent deploy/omni-fullstack -c omni-fullstack -- python -c "
import asyncio, redis.asyncio as redis, json, time
async def main():
    r = redis.from_url('redis://redis.multi-agent.svc.cluster.local:6379', decode_responses=True)
    bad = False
    for host in ('cust-app','cust-edge','cust-db'):
        k = f'omni:remote_agent:registry:loyalty-uat_{host}'
        v = await r.get(k)
        if not v:
            print(f'  FAIL {host}: khong co registry key')
            bad = True
            continue
        age = time.time() - json.loads(v).get('last_seen', 0)
        status = 'OK' if age < 60 else 'FAIL'
        print(f'  {status} {host}: last_seen {round(age)}s truoc')
        if status == 'FAIL':
            bad = True
    raise SystemExit(1 if bad else 0)
asyncio.run(main())
" && echo "  ✅ tất cả agent heartbeat tươi" || { echo "  ❌ có agent heartbeat cũ/mất"; FAIL=1; }

echo
echo "── 7. Auto-execute allowlist đúng 3 agent Loyalty-UAT ──────────
"
allow=$(kubectl get deploy omni-fullstack -n multi-agent -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="OMNI_LAB_AUTO_EXECUTE_AGENTS")].value}' 2>/dev/null)
if echo "$allow" | grep -q "loyalty-uat_cust-app"; then
  ok "cust-app nằm trong allowlist tự thực thi ($allow)"
else
  bad "cust-app KHÔNG có trong allowlist — demo sẽ chỉ dừng ở mức đề xuất, không tự sửa"
fi

echo "── 8. Cooldown fingerprint (Đ52, 900s) cho đúng drill payment-api ──────
"
# Đ55: phát hiện thật — chạy đúng kịch bản 2 lần trong <15 phút khiến cooldown
# chặn hoàn toàn lần 2, Telegram im lặng dù hệ thống đúng thiết kế. Script này
# CHỈ BÁO cáo còn bao lâu — không tự xoá (đúng hợp đồng "chỉ kiểm tra, không
# sửa gì" của cả file). Fingerprint dưới đây đã quan sát ỔN ĐỊNH qua 3 lần
# drill payment-api/cust-app hôm nay 2026-08-11 (cùng probe+nội dung evidence
# → cùng hash) — nếu nội dung log agent gửi đổi khác đi, fingerprint có thể
# đổi theo và bước này sẽ không còn đúng key để kiểm; không phải lỗi nghiêm
# trọng (rơi về báo "không thấy cooldown cũ" = coi như sạch), chỉ là giả định
# cần biết.
kubectl exec -n multi-agent deploy/omni-fullstack -c omni-fullstack -- python -c "
import asyncio, redis.asyncio as redis, json, time
FP = 'service_systemd_units:86b3cca77b44'
async def main():
    r = redis.from_url('redis://redis.multi-agent.svc.cluster.local:6379', decode_responses=True)
    raw = await r.get(f'omni:evcluster:seen:{FP}')
    if not raw:
        print('  OK khong co ban ghi cu — sach, khong bi cooldown chan')
        return
    d = json.loads(raw)
    ld = d.get('last_diagnosis')
    if not ld:
        print('  OK chua tung chan doan fingerprint nay — sach')
        return
    age = time.time() - ld.get('ts', 0)
    remain = 900 - age
    if remain > 0:
        print(f'  FAIL con cooldown ~{int(remain)}s nua — demo live se IM LANG neu chay ngay bay gio')
        raise SystemExit(1)
    print(f'  OK cooldown da het tu {int(-remain)}s truoc — an toan de demo')
asyncio.run(main())
" && ok "cooldown fingerprint drill payment-api: sạch" || bad "cooldown còn hiệu lực — đợi hết giờ hoặc đổi VM/unit khác cho demo"

echo
if [ "$FAIL" -eq 0 ]; then
  echo "════ TẤT CẢ PASS — sẵn sàng đi cafe demo ════"
else
  echo "════ CÓ MỤC FAIL — xử lý xong rồi chạy lại script này TRƯỚC khi đi ════"
  exit 1
fi
