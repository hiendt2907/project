# Current Session Handoff

## Deliverable hiện tại
**Sổ ca (Case Ledger)** — nền dữ liệu để Omni trở thành *nhân viên SRE* thay vì công cụ
giám sát. ĐANG LÀM. Thiết kế đầy đủ: `plans/case-ledger-design-2026-07-30.md`.

## Ý tưởng sản phẩm (đã brainstorm và chốt với user 2026-07-29/30)
Omni = **một nhân viên SRE senior** làm 95% việc hàng ngày của user, không phải "AI hỗ trợ".

- **Vòng đời = thử việc**: agent cài lên hệ thống khách → tự discover + hỏi tài liệu →
  `shadow` ~3 tháng (quan sát, không đụng gì) → admin **tenant** chuyển `minimal`
  (làm vài loại việc) → `autonomous` (toàn quyền **trong khuôn khổ**).
  Bất kể tier: hành động **xoá dữ liệu** luôn phải báo admin khách. Lằn ranh cứng.
- **Trí nhớ**: gặp lại vấn đề cũ KHÔNG chẩn đoán lại từ đầu — phải nói "đây là lần N,
  tôi đã báo ngày X, chưa ai xử lý". Điều tra lại từ đầu = vứt kinh nghiệm lần 1.
- **Chính kiến**: được phép nói "không", nhưng phải kèm bằng chứng + chẩn đoán đầy đủ.
- **Tham vọng**: **tự xin** mở rộng quyền theo từng loại việc, kèm số liệu tự chứng minh.
  Không chờ được đánh giá. Portal không phải form trống — Omni đề xuất, khách duyệt.
- **Ngoài quyền hạn** (code rò, thiếu index, kiến trúc sai): chẩn đoán **một lượt**,
  advise cho admin khách, hết phần nó. Nó là **người thực thi**, không quản lý backlog.
- **Trách nhiệm**: nó sai → user chịu. CRAT tồn tại để **truy nguyên nhân và cập nhật
  chính nó**, không phải để đổ lỗi.
- **Đo bằng**: hiểu hệ thống (discover) · kinh nghiệm xử lý · root cause + xử lý triệt
  để không tái diễn · biết đề xuất nâng/**giảm** size hạ tầng.

## Chống bùa số — yêu cầu riêng của user, KHÔNG được nới
Không phải chuyện Omni "nói dối" — chỉ cần nó tối ưu theo một con số là con số đó hỏng
(Goodhart). Giải bằng **tách vai** (tinh thần SOX §404 mà CRAT vốn đã xây theo).

1. **Mẫu số chốt trước khi biết kết quả** — ca mở lúc phát biểu, `pattern_key` đóng băng.
2. **Im lặng là im lặng** — 3 trạng thái, `UNJUDGED` không vào tử/mẫu số.
3. **Sự thật từ thế giới** — `recurred`, Omni không bịa được.
4. **Người chấm ≠ người làm** — `verdict_source` không có `self`/`system`.
5. **Cận dưới Wilson**, không dùng tỉ lệ thô (3/3 = 100% là con số nói dối hợp pháp).
6. **Hai số kéo ngược nhau**: độ chính xác × độ phủ. Chỉ đo chính xác thì chiến lược
   tối ưu là **từ chối mọi ca khó** — trông cẩn thận, thực chất vô dụng.
7. **Xin bị từ chối phải có giá** (cooldown); **FROZEN chỉ người gỡ được**.

## Đã xong (tôi tự làm, đã verify)
| | |
|---|---|
| `plans/case-ledger-design-2026-07-30.md` | thiết kế + lý do từng ràng buộc |
| `migrations/omni_admin/0012_case_ledger.sql` | 4 bảng + trigger; **đã apply lên cluster `omnidb`** |
| `src/services/case_ledger/scoring.py` | Wilson lower bound, CompetencyReport |
| `src/services/case_ledger/store.py` | CaseLedgerStore; open_case tự tính occurrence_no + tự đánh dấu ca trước `recurred` |
| `scripts/verify_case_ledger.sh` + `make verify-case-ledger` | **15 PASS / 0 FAIL trên Postgres thật** |

**Vì sao có script riêng ngoài pytest:** bất biến quan trọng nhất nằm ở TRIGGER Postgres,
không nằm trong Python. Test đơn vị dùng fake pool vẫn XANH kể cả khi migration chưa apply
hoặc trigger bị drop — âm tính giả nguy hiểm, vì đây là hàng rào khách hàng dựa vào để
trao quyền cho hệ thống tự động.

Ba số đo thật đáng nhớ:
- `wilson_lower_bound(3,3)` = **0.4385** — 3/3 trông hoàn hảo nhưng không qua cửa.
- Omni "khôn lỏi" (2 ca dễ đúng cả 2, từ chối 8 ca khó): chính xác thô **100%**, độ phủ
  **0.20** → **TRƯỢT**. Cơ chế chống bùa số đã có hiệu lực thật.
- 5 bất biến DB đều chặn đúng trên PG thật (không mock).

## Subagent nền
1. ⏳ Advisory verdict 3 nút (Đúng/Sai/Đúng-nhưng-thiếu) + mở ca lúc phát + trí nhớ lần-N
2. ✅ **HITL → sổ ca — XONG.** `src/services/case_ledger/hitl_link.py` (mới, đặt ở
   `services/` vì gateway KHÔNG được import `workers/`) + nối cả 2 bề mặt
   (`hitl_telegram.py`, `gateway/routes/autonomy.py`). 10 test, đã tự kiểm chứng lại:
   pass + `grep "from workers" src/gateway/` rỗng.
   **Quyết định đúng của agent:** approve → `diagnosis=CORRECT` nhưng `remedy` để
   **UNJUDGED** — lúc duyệt thì hành động CHƯA chạy, chưa ai biết nó có sửa được gì
   không. Ghi CORRECT ở đó là Omni tự chấm phần khắc phục của chính nó. Nhãn `remedy`
   thuộc về nguồn `world`/`mark_recurred`.
3. ⏳ Competency report + đơn xin quyền + route `/competency/*`
4. ⏳ Test lõi cho scoring/store

**Chưa giao**: portal UI (chờ API của agent 3), phát hiện tái diễn từ metric thật.

## Bug thật đã xác minh runtime (lý do phải làm việc này)
- `omni:learn:promo:*` = **0 key** — đường học qua thực thi chưa từng chạy (shadow ⇒
  không mutation ⇒ không VERIFIED_SUCCESS). **Đúng thiết kế**, đừng "sửa".
- `grep -rn "accepted=False" src/` = **RỖNG** → nhánh FROZEN trong
  `advisory_promoter.next_graduation_state()` là **code chết không thể chạm tới**.
  Vòng học chỉ nhận nhãn khen → tự tin dần lên bất kể đúng sai.
- `advisory_ack.py:28` nói nút đó "không phải approve/reject", nhưng dòng 186 truyền
  `accepted=True`. Đang học từ **sự chú ý** rồi coi là **sự đồng tình**.
- HITL approve/reject → CRAT rồi **vứt hoàn toàn**, không nối vòng học.
- `omni:kpi:z:*` chỉ có `rejected`, `playbook_graduation.fail_count` = 0 toàn bộ →
  bằng chứng trực tiếp: tín hiệu tiêu cực đang bị rơi.
- 3 hàng `playbook_graduation` hiện có là **dữ liệu test 29/7**, không phải lưu lượng thật.

## KHÔNG làm được (đã nói với user)
Subagent theo dõi quota token Claude rồi tự chạy lại sau reset — subagent không đọc được
hạn mức tài khoản và không có gì đánh thức phiên sau reset. Thay thế: commit theo mốc.

## Working tree — CHƯA COMMIT
Mới (untracked): `plans/case-ledger-design-2026-07-30.md` ·
`migrations/omni_admin/0012_case_ledger.sql` · `src/services/case_ledger/` ·
`scripts/verify_case_ledger.sh` · các file test/agent đang sinh.
Sửa: `Makefile` (target `verify-case-ledger`) · `docs/handoffs/CURRENT_SESSION.md`.
4 subagent đang ghi thêm vào `src/workers/`, `src/gateway/`, `tests/`.
Memory đã ghi: `project_omni_vision_employee_not_tool`, `project_learning_loop_broken_labels`.

## Next step
1. Chờ 4 agent xong → tích hợp, chạy full suite (baseline **6750 passed**) +
   `make verify-case-ledger` (15/15).
2. Test hành vi thật trên cluster: bấm nút Telegram → verdict vào PG; HITL reject →
   `INCORRECT`; pattern lặp lần 2 → thẻ báo "lần 2" + ca trước `recurred=TRUE`.
3. Portal UI: admin cấu hình khuôn khổ + duyệt đơn xin quyền.
4. Commit theo mốc.

## Bắt buộc chạy tay trước mỗi push (CI đã gỡ vì hết quota GitHub Actions)
```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.18.2 detect \
  --no-git --source=/repo --config=/repo/.gitleaks.toml
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
```

## Mặt public (xong từ phiên trước, đừng làm lại)
`www.omnisre.xyz` + `app.omnisre.xyz` sau Cloudflare Access, tunnel qua LaunchAgent.
`bash cloudflare/tunnel/verify.sh` → 17/17. `make sync-public*` để đồng bộ.
**INV_PUBLIC_PLANE_ISOLATED**: không đụng một biến nào của lab `provider.ai-agent.local`.

## Bẫy đã trả giá
1. `client_secret` phải `openssl rand -hex`, KHÔNG base64 (`+` bị URL-decode, RFC 6749
   §2.3.1). Triệu chứng "invalid or expired state" là **hệ quả** — đọc log callback ĐẦU TIÊN.
2. `rollout restart` KHÔNG build image (`IfNotPresent` + `:latest`) — dùng `make sync-public*`.
3. `.items[0]` chọn nhầm pod Terminating ngay sau `rollout status`.
4. Audit hết hạn nhanh: 2 finding CRITICAL ngày 22/7 kiểm lại đã đóng sẵn.
5. DB tên **`omnidb`**, không phải `omni`.
