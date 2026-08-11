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

echo
if [ "$FAIL" -eq 0 ]; then
  echo "════ TẤT CẢ PASS — sẵn sàng đi cafe demo ════"
else
  echo "════ CÓ MỤC FAIL — xử lý xong rồi chạy lại script này TRƯỚC khi đi ════"
  exit 1
fi
