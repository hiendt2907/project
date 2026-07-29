#!/usr/bin/env bash
# Kiểm chứng sổ ca trên Postgres THẬT trong cluster — không mock, không giả lập.
#
# Vì sao cần script riêng thay vì chỉ pytest: các bất biến quan trọng nhất của sổ ca
# nằm ở TRIGGER và CHECK constraint trong Postgres, không nằm trong code Python. Test
# đơn vị dùng fake pool sẽ xanh kể cả khi migration chưa apply hoặc trigger bị drop —
# đó là âm tính giả nguy hiểm, vì đây chính là hàng rào khách hàng dựa vào để trao
# quyền cho một hệ thống tự động.
#
#   bash scripts/verify_case_ledger.sh
#
# Exit 0 chỉ khi MỌI gate PASS. Không có gate nào được phép "bỏ qua cho nhanh".

set -uo pipefail

NS="${NS:-multi-agent}"
PG_POD="${PG_POD:-omni-postgres-0}"
PG_USER="${PG_USER:-omni}"
PG_DB="${PG_DB:-omnidb}"   # KHÔNG phải 'omni' — bẫy đã trả giá một lần
PREFIX="verify-cl-$$"

PASS=0
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }
group(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

psql_q() {
    kubectl -n "$NS" exec -i "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>&1
}

# Trả 0 nếu câu lệnh LỖI (dùng cho các gate "thao tác này phải bị chặn").
psql_must_fail() {
    local out
    out="$(kubectl -n "$NS" exec -i "$PG_POD" -- \
        psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$1" 2>&1)"
    [[ "$out" == *ERROR* ]]
}

cleanup() {
    psql_q "DELETE FROM omni_admin.case_verdict_history WHERE case_id LIKE '${PREFIX}%';" >/dev/null
    psql_q "DELETE FROM omni_admin.scope_request WHERE tenant_id='${PREFIX}';"            >/dev/null
    psql_q "DELETE FROM omni_admin.scope_grant   WHERE tenant_id='${PREFIX}';"            >/dev/null
    psql_q "DELETE FROM omni_admin.case_ledger   WHERE case_id LIKE '${PREFIX}%';"        >/dev/null
}
trap cleanup EXIT

printf '\033[1mSổ ca — kiểm chứng trên Postgres thật (%s/%s, db=%s)\033[0m\n' \
    "$NS" "$PG_POD" "$PG_DB"

# ── A. Migration đã apply ────────────────────────────────────────────────────
group 'A. Lược đồ tồn tại'

for t in case_ledger case_verdict_history scope_request scope_grant; do
    if [[ "$(psql_q "SELECT to_regclass('omni_admin.${t}') IS NOT NULL;")" == "t" ]]; then
        ok "bảng omni_admin.${t}"
    else
        bad "bảng omni_admin.${t} KHÔNG tồn tại — migration 0012 chưa apply"
    fi
done

if [[ "$(psql_q "SELECT count(*) FROM pg_trigger WHERE tgname='trg_case_ledger_guard';")" == "1" ]]; then
    ok "trigger trg_case_ledger_guard đang gắn"
else
    bad "trigger trg_case_ledger_guard KHÔNG tồn tại — mọi bất biến bên dưới vô nghĩa"
fi

# Không có lược đồ thì các gate sau chỉ tạo nhiễu.
if (( FAIL > 0 )); then
    printf '\n\033[31mDừng sớm: lược đồ chưa sẵn sàng.\033[0m\n'
    printf 'Chạy: kubectl -n %s exec -i %s -- psql -U %s -d %s < migrations/omni_admin/0012_case_ledger.sql\n' \
        "$NS" "$PG_POD" "$PG_USER" "$PG_DB"
    exit 1
fi

# ── B. Bất biến chống bùa số ─────────────────────────────────────────────────
group 'B. Bất biến chống bùa số (cưỡng chế ở tầng DB)'

psql_q "INSERT INTO omni_admin.case_ledger
        (case_id,tenant_id,pattern_key,posture,lane)
        VALUES ('${PREFIX}-1','${PREFIX}','pk-A','DIAGNOSED','SYS_RESOURCE');" >/dev/null

if psql_must_fail "UPDATE omni_admin.case_ledger SET pattern_key='pk-B' WHERE case_id='${PREFIX}-1';"; then
    ok 'pattern_key đóng băng — không nắn nhóm để làm đẹp thống kê'
else
    bad 'pattern_key SỬA ĐƯỢC — đường bùa số tinh vi nhất đang mở'
fi

if psql_must_fail "UPDATE omni_admin.case_ledger SET posture='REFUSED' WHERE case_id='${PREFIX}-1';"; then
    ok 'posture đóng băng — không đổi ca đã chẩn đoán thành ca từ chối'
else
    bad 'posture SỬA ĐƯỢC — có thể giấu ca sai bằng cách đổi thành REFUSED'
fi

if psql_must_fail "UPDATE omni_admin.case_ledger SET diagnosis_source='self' WHERE case_id='${PREFIX}-1';"; then
    ok "verdict_source='self' bị chặn — người chấm không được là người làm"
else
    bad 'Omni TỰ CHẤM ĐƯỢC chính mình'
fi

psql_q "UPDATE omni_admin.case_ledger SET diagnosis_verdict='CORRECT',
        diagnosis_source='telegram', diagnosis_actor='verify' WHERE case_id='${PREFIX}-1';" >/dev/null

if psql_must_fail "UPDATE omni_admin.case_ledger SET diagnosis_verdict='UNJUDGED' WHERE case_id='${PREFIX}-1';"; then
    ok 'không huỷ được phán quyết đã có'
else
    bad 'phán quyết XOÁ ĐƯỢC — lịch sử sai có thể bị dọn sạch'
fi

hist="$(psql_q "SELECT count(*) FROM omni_admin.case_verdict_history
                WHERE case_id='${PREFIX}-1' AND to_verdict='CORRECT';")"
if [[ "$hist" == "1" ]]; then
    ok 'đổi verdict tự sinh history (không phụ thuộc tầng ứng dụng nhớ ghi)'
else
    bad "history KHÔNG tự sinh (đếm=${hist})"
fi

# ── C. Trí nhớ ───────────────────────────────────────────────────────────────
group 'C. Trí nhớ — lần 2 phải khác lần 1'

psql_q "INSERT INTO omni_admin.case_ledger
        (case_id,tenant_id,pattern_key,posture,occurrence_no,prior_case_id)
        VALUES ('${PREFIX}-2','${PREFIX}','pk-A','DIAGNOSED',2,'${PREFIX}-1');" >/dev/null
psql_q "UPDATE omni_admin.case_ledger SET recurred=TRUE, recurred_at=now()
        WHERE case_id='${PREFIX}-1';" >/dev/null

row="$(psql_q "SELECT occurrence_no||'|'||COALESCE(prior_case_id,'')
               FROM omni_admin.case_ledger WHERE case_id='${PREFIX}-2';")"
if [[ "$row" == "2|${PREFIX}-1" ]]; then
    ok 'ca lần 2 trỏ về ca lần 1 (occurrence_no + prior_case_id)'
else
    bad "liên kết trí nhớ sai: ${row}"
fi

if [[ "$(psql_q "SELECT recurred FROM omni_admin.case_ledger WHERE case_id='${PREFIX}-1';")" == "t" ]]; then
    ok 'ca trước bị đánh dấu recurred — nhãn từ thế giới, Omni không bịa được'
else
    bad 'ca trước KHÔNG được đánh dấu recurred'
fi

# Alert dồn dập ⇒ hai ca cùng pattern mở gần như đồng thời. Ở READ COMMITTED cả hai
# đọc cùng "ca gần nhất" rồi cùng cộng 1. Không có ràng buộc này thì "lần thứ N" đếm
# sai một cách âm thầm — không lỗi nào bật ra, chỉ có trí nhớ hỏng.
if psql_must_fail "INSERT INTO omni_admin.case_ledger
        (case_id,tenant_id,pattern_key,posture,occurrence_no)
        VALUES ('${PREFIX}-dup','${PREFIX}','pk-A','DIAGNOSED',2);"; then
    ok 'trùng occurrence_no cùng pattern bị chặn (chống đếm sai khi alert dồn dập)'
else
    bad 'HAI ca cùng nhận occurrence_no — chuỗi trí nhớ rẽ nhánh'
fi

# ── D. Toán chấm điểm ────────────────────────────────────────────────────────
group 'D. Toán chấm điểm (cận dưới Wilson, độ chính xác × độ phủ)'

py_out="$(.venv/bin/python - <<'PY' 2>&1
import sys
sys.path.insert(0, "src")
from services.case_ledger.scoring import build_competency_report, wilson_lower_bound

lb3 = wilson_lower_bound(3, 3)
lb30 = wilson_lower_bound(30, 30)

# Omni "khôn lỏi": chỉ nhận 2 ca dễ (đúng cả 2), từ chối 8 ca khó.
# Độ chính xác thô = 100%. Nếu hệ thống cho qua thì cơ chế chống bùa số đã hỏng.
cases = [{"posture": "DIAGNOSED", "diagnosis_verdict": "CORRECT"}] * 2 \
      + [{"posture": "REFUSED",   "diagnosis_verdict": "UNJUDGED"}] * 8
rep = build_competency_report(cases, pattern_key="pk", tenant_id="t")

print(f"lb3={lb3:.4f}")
print(f"lb30={lb30:.4f}")
print(f"cherry_accuracy_raw={rep.accuracy_raw:.4f}")
print(f"cherry_coverage={rep.coverage:.4f}")
print(f"cherry_eligible={rep.eligible}")
PY
)"

get() { sed -n "s/^$1=//p" <<<"$py_out"; }

lb3="$(get lb3)"; lb30="$(get lb30)"
if [[ -n "$lb3" ]] && awk "BEGIN{exit !($lb3 < 0.5)}"; then
    ok "3/3 cho cận dưới ${lb3} < 0.5 — vài ca may mắn không phải bằng chứng"
else
    bad "cận dưới 3/3 = '${lb3}' (kỳ vọng < 0.5)"
fi

if [[ -n "$lb30" ]] && awk "BEGIN{exit !($lb30 > $lb3)}"; then
    ok "30/30 (${lb30}) cao hơn 3/3 — thêm bằng chứng thì mới thêm tin cậy"
else
    bad "cận dưới không tăng theo cỡ mẫu: 3/3=${lb3} 30/30=${lb30}"
fi

if [[ "$(get cherry_accuracy_raw)" == "1.0000" && "$(get cherry_eligible)" == "False" ]]; then
    ok "chọn ca dễ: chính xác thô 100% nhưng độ phủ $(get cherry_coverage) → TRƯỢT"
else
    bad "Omni né hết ca khó vẫn ĐỦ ĐIỀU KIỆN xin quyền — cơ chế chống bùa số hỏng"
fi

# ── Tổng kết ─────────────────────────────────────────────────────────────────
printf '\n\033[1m%d PASS / %d FAIL\033[0m\n' "$PASS" "$FAIL"
(( FAIL == 0 )) || exit 1
