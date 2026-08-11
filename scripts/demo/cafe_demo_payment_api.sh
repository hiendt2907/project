#!/usr/bin/env bash
# Demo live cho buổi gặp CTO (Đ55, 2026-08-11) — KHÔNG phải drill nội bộ, đây là
# script chạy TRƯỚC MẶT NGƯỜI XEM để chứng minh vòng khép kín có thật, không phải
# slide. Dùng đúng target đã verify sống hôm nay (tenant loyalty-uat, VM cust-app,
# payment-api.service) — không bịa kịch bản mới chưa test.
#
# Yêu cầu trước khi rời nhà:
#   - MacBook có OrbStack chạy (VM cust-app/cust-db/cust-edge phải "running")
#   - Tailscale kết nối được cluster GCP (kubectl context đúng, xem CLAUDE.md)
#   - Điện thoại đã mở sẵn Telegram, đăng nhập đúng tài khoản admin
#   - Đã chạy `bash scripts/demo/cafe_demo_preflight.sh` ở nhà, mọi thứ PASS
#
# Cách dùng tại quán cafe:
#   bash scripts/demo/cafe_demo_payment_api.sh
# Script sẽ dừng ở từng bước, chờ Enter — để presenter kiểm soát nhịp độ nói
# chuyện, không bị terminal chạy nhanh hơn lời giải thích.
set -euo pipefail

VM="cust-app"
UNIT="payment-api"
NS="multi-agent"

pause() {
  echo
  read -rp "  [Enter để tiếp tục] " _
  echo
}

banner() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "════════════════════════════════════════════════════════════════"
}

banner "BƯỚC 0 — Trạng thái BÌNH THƯỜNG (trước khi có sự cố)"
echo "Đây là 1 trong 3 server thật (VM lab, mô phỏng hạ tầng khách hàng)."
echo "payment-api đang chạy khoẻ mạnh, Omni đang âm thầm theo dõi (chu kỳ 20s):"
echo
orb -m "$VM" systemctl status "$UNIT" --no-pager | head -6
pause

banner "BƯỚC 1 — GÂY SỰ CỐ THẬT (không phải giả lập)"
echo "Lệnh sắp chạy sẽ dừng THẬT service payment-api trên VM $VM — đúng như"
echo "một sự cố production thật lúc rạng sáng, không ai đứng đó bấm gì cả."
pause
T0=$(date +%s)
orb -m "$VM" sudo systemctl stop "$UNIT"
echo "$(date '+%H:%M:%S') — đã dừng $UNIT trên $VM. Đồng hồ bắt đầu chạy."
echo "Từ giờ, KHÔNG ai chạm vào terminal nữa — mọi thứ dưới đây do Omni tự làm."

banner "BƯỚC 2 — CHỜ OMNI TỰ PHÁT HIỆN (xem điện thoại, Telegram)"
echo "Agent trên VM báo về mỗi ~20s. Đang chờ dòng log 'FAILED' xuất hiện phía"
echo "server thật (không phải terminal của mình bịa ra) — mở điện thoại lên xem"
echo "Telegram song song với terminal này."
echo
echo "Đang tail log thật từ pod xử lý (Ctrl+C để dừng theo dõi khi thấy thẻ"
echo "chẩn đoán đã tới Telegram):"
echo
kubectl logs -n "$NS" deploy/omni-fullstack -c omni-fullstack -f --since=10s 2>/dev/null \
  | grep --line-buffered -E "\[RAP\]|known_fix_reflex|AUTO_RECOVERY|diagnosis_loop" &
LOGPID=$!
trap 'kill $LOGPID 2>/dev/null || true' EXIT

# macOS mặc định bash 3.2 (không có `wait -n`) — dùng sleep chặn đơn giản thay
# vì poll/race hai job. Tự dừng tail sau tối đa 4 phút, đủ cho cả vòng chẩn
# đoán (~2-4 phút theo comment trong remote_agent_pipeline.py) lẫn reflex dispatch.
echo "(đang chờ tối đa 4 phút cho vòng phát hiện+chẩn đoán+tự sửa hoàn tất...)"
sleep 240
kill "$LOGPID" 2>/dev/null || true
trap - EXIT

banner "BƯỚC 3 — XÁC NHẬN ĐÃ TỰ PHỤC HỒI"
echo "Kiểm tra trạng thái thật trên VM (không phải Omni tự báo cáo là xong):"
echo
orb -m "$VM" systemctl status "$UNIT" --no-pager | head -6
T1=$(date +%s)
echo
echo "Tổng thời gian từ lúc sự cố tới lúc kiểm tra: $((T1 - T0)) giây."
pause

banner "BƯỚC 4 — BẰNG CHỨNG KIỂM TOÁN (không phải 'AI nói vậy thì tin vậy')"
echo "Mọi quyết định tự sửa đều ghi vào audit chain trước khi thực thi (CRAT,"
echo "hash-chain kiểu blockchain nội bộ, không sửa được sau khi ghi). Xem entry"
echo "gần nhất trên chính server Omni đang chạy, không phải file demo:"
echo
kubectl exec -n "$NS" deploy/omni-fullstack -c omni-fullstack -- python -c "
import asyncio, redis.asyncio as redis
async def main():
    r = redis.from_url('redis://redis.multi-agent.svc.cluster.local:6379', decode_responses=True)
    entries = await r.lrange('omni:audit:chain', -3, -1)
    for e in entries:
        print(e[:300])
asyncio.run(main())
" 2>&1 || echo "(nếu lệnh này lỗi tại quán — bỏ qua, không phải phần quan trọng nhất của demo)"

banner "XONG — DỌN DẸP"
echo "Đảm bảo VM về trạng thái sạch cho lần demo sau:"
orb -m "$VM" systemctl is-active "$UNIT" || orb -m "$VM" sudo systemctl start "$UNIT"
echo "payment-api trạng thái cuối: $(orb -m "$VM" systemctl is-active "$UNIT")"
