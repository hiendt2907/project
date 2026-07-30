#!/usr/bin/env bash
# Test chẩn đoán TÁC ĐỘNG VM THẬT — dừng một service trên VM lab, chờ Omni chẩn
# đoán, rồi TỰ ĐỘNG khôi phục. Chạy trên MÁY CHỦ lab (có `orb`), không phải trong pod.
#
#   bash scripts/diag-test-vm.sh cust-edge nginx
#   bash scripts/diag-test-vm.sh cust-edge nginx 90     # chờ 90s trước khi khôi phục
#
# Vì sao script này thay vì nút bấm trên UI: pod gateway KHÔNG có quyền chạm máy khách
# (không có `orb`, không SSH). Chỉ tiến trình trên host mới dừng được service thật. Nút
# "Test lại" trên /diagnostics chạy đúng pipeline nhưng với sự cố mẫu; script này mới là
# tác động vật lý.
#
# AN TOÀN: luôn khôi phục service khi thoát (kể cả Ctrl-C / lỗi giữa chừng) — VM không
# bao giờ bị bỏ ở trạng thái hỏng qua đêm.

set -euo pipefail

VM="${1:-cust-edge}"
SERVICE="${2:-nginx}"
WAIT_SEC="${3:-45}"

command -v orb >/dev/null || { echo "thiếu orb (OrbStack CLI)" >&2; exit 1; }

_restored=0
restore() {
    [[ "$_restored" == "1" ]] && return
    _restored=1
    echo "▸ Khôi phục $SERVICE trên $VM…"
    orb -m "$VM" sudo systemctl start "$SERVICE" 2>&1 || true
    sleep 2
    local state
    state="$(orb -m "$VM" systemctl is-active "$SERVICE" 2>&1 || true)"
    echo "  trạng thái sau khôi phục: $state"
    [[ "$state" == "active" ]] || echo "  ⚠️  KHÔNG active — kiểm tay: orb -m $VM systemctl status $SERVICE" >&2
}
trap restore EXIT INT TERM

echo "▸ Trạng thái trước: $(orb -m "$VM" systemctl is-active "$SERVICE" 2>&1 || true)"
echo "▸ Dừng $SERVICE trên $VM lúc $(date -u +%H:%M:%SZ) — Omni sẽ phát hiện qua chu kỳ thu kế tiếp"
orb -m "$VM" sudo systemctl stop "$SERVICE" 2>&1 || true
sleep 2
echo "  trạng thái sau khi dừng: $(orb -m "$VM" systemctl is-active "$SERVICE" 2>&1 || true)"

echo "▸ Chờ ${WAIT_SEC}s để agent thu số → gateway → vòng chẩn đoán chạy…"
sleep "$WAIT_SEC"

echo "▸ Xong chờ. Xem kết quả từng bước trên UI: /diagnostics (hoặc /pipeline)."
echo "  (restore sẽ chạy ngay sau đây qua trap EXIT)"
