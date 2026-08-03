# Omni v2 — Engineering Process Rules

**Có hiệu lực từ:** 2026-08-03, ngay sau khi `OMNI_V2_FINAL_EXECUTION_GATE.md` được CTO ký duyệt
(APPROVED WITH BLOCKERS). Tài liệu này KHÔNG phải vòng review kiến trúc mới — đây là 3 quy tắc
vận hành bổ sung trước khi engineering kickoff, do CTO yêu cầu ghi lại thành văn bản trước khi
coding bắt đầu.

> Tham chiếu bắt buộc: `OMNI_V2_FINAL_EXECUTION_GATE.md` (kiến trúc + roadmap đã khoá),
> `OMNI_V2_FINAL_SHIP_REVIEW.md` (lý do từng finding còn/mất).

---

## A. Definition of Done — bắt buộc cho mọi WS / must-fix

Không WS/task nào được đánh dấu "xong" nếu thiếu bất kỳ mục nào dưới đây. Đây là checklist tối
thiểu chung; từng task có thể có thêm điều kiện riêng (ghi trong `blockedBy`/description của task
đó), nhưng không được ít hơn checklist này.

```
Definition of Done (mọi WS/must-fix)
- [ ] Code merged vào main (qua PR đúng quy tắc B bên dưới)
- [ ] Unit test cho phần logic mới/sửa — xanh
- [ ] Integration test nếu chạm Kafka/Postgres/Redis/K8s — xanh
- [ ] Docs cập nhật: CLAUDE.md / docs liên quan nếu invariant hoặc topology đổi
- [ ] Rollout verified — chạy thật trên cluster lab (không chỉ code review), có bằng chứng
      (log/kubectl output), KHÔNG chấp nhận "test pass" làm bằng chứng deploy
      (xem project_deploy_gateway_no_build_gotcha — bài học cũ)
- [ ] Rollback verified — xác nhận cách lùi lại nếu sai (git revert / feature flag / migration
      down), ghi rõ trong PR description
```

Áp dụng cụ thể cho từng hạng mục trong Implementation Order đã khoá:

| Task | Rollout verified nghĩa là gì | Rollback verified nghĩa là gì |
|---|---|---|
| `#14` resolve_tier fail-closed | Redis bị cắt tay (`kubectl exec redis-0 -- redis-cli SHUTDOWN NOSAVE` trên lab hoặc drop network policy tạm thời) → xác nhận tier trả về SHADOW, không throw/crash | `git revert` đơn thuần, không có state cần dọn |
| `#12` teardown-postgres landmine | Chạy thử script đã sửa trên lab, xác nhận KHÔNG xoá cluster `omni-postgres` còn đang phục vụ `omni_admin` | Script cũ giữ trong git history, không cần rollback runtime (đây là sửa 1 script, không phải runtime code) |
| WS1 (`#2`) sửa import ngược | `import-linter` chưa cần chạy (đó là WS0) — verify bằng cách import module đã sửa trong pod thật, không ImportError | `git revert` từng import fix riêng lẻ nếu 1 module gãy |
| WS0 (`#1`) import-linter CI | CI pipeline thật chạy `import-linter`, fail đúng khi cố tình thêm 1 import ngược thử nghiệm | Xoá bước CI khỏi pipeline nếu false-positive tràn lan |
| WS5 (`#6`) Capability Registry | Toàn bộ loop hiện có (evidence/actions/feedback/kpi/knowledge/siem-chains/siem-correlation/tier) chạy lại đúng như trước qua `kubectl logs` sau khi đổi composition root — không loop nào biến mất | Có nhánh git riêng, merge 1 lần duy nhất; rollback = revert merge commit đó (đây là lý do bắt buộc PR riêng — xem mục B) |
| `#21` timeout blast_radius | Test cố tình cho K8s API treo (network policy chặn tạm) → xác nhận request trả lỗi có kiểm soát trong X giây thay vì treo vô hạn | `git revert`, không có state |
| `#15` tách audit_chain khỏi allkeys-lru | `redis-cli INFO memory` xác nhận audit_chain nằm ở vùng không bị evict (DB riêng hoặc instance riêng) | Tuỳ phương án chọn — nếu logical DB riêng: đổi lại `SELECT` index; nếu instance riêng: xoá deployment mới |
| `#13` backup/restore omni-postgres | Restore thử từ backup trên 1 cluster lab phụ, xác nhận `omni_admin` schema toàn vẹn | N/A (đây là bổ sung capability, không thay runtime hiện có) |
| WS2 (`#3`) Decision Transparency | Bắn 1 evidence thật qua pipeline, xác nhận `DECISION_RENDERED` event xuất hiện đúng chỗ | `git revert`, đã xác nhận ở Đ19 không cần feature flag |

---

## B. Một PR chỉ implement một WS hoặc một must-fix

**Quy tắc cứng, áp dụng cho toàn bộ roadmap, không riêng WS5:**

```
PR-N   →  đúng 1 WS hoặc đúng 1 must-fix task, không trộn
```

Ví dụ đúng:
```
PR-31   WS1 (#2) only — 5 import fix
PR-32   WS0 (#1) only — import-linter CI
PR-33   WS5 (#6) only — Capability Registry (milestone riêng, xem Đ19 amendment)
```

Ví dụ SAI (không được làm):
```
PR-34   "fix resolve_tier + tiện thể dọn vài import thừa"   ❌ trộn #14 với việc khác
```

Lý do: review dễ, revert dễ, và mỗi PR có đúng 1 Definition of Done (mục A) để đối chiếu — trộn
nhiều WS làm mất khả năng verify rollback độc lập.

---

## C. Freeze Acceptance Criteria — không đổi Definition of Done giữa chừng

```
Không thay đổi Definition of Done của một WS/task sau khi implementation đã bắt đầu
(đã có PR mở), trừ khi có ADR mới ghi rõ lý do.
```

Đây là hệ quả trực tiếp của Scope Freeze đã ghi trong `OMNI_V2_FINAL_EXECUTION_GATE.md` mục 7:
Scope Freeze chặn việc **mở rộng phạm vi** WS; mục C này chặn việc **hạ hoặc nâng tiêu chuẩn hoàn
thành** của WS sau khi đã bắt tay vào code — hai rủi ro khác nhau nhưng cùng nguồn gốc (roadmap bị
phình/mờ dần sau khi review kết thúc).

Nếu phát sinh nhu cầu đổi DoD giữa chừng (vd phát hiện cần thêm 1 test integration mà lúc lập kế
hoạch không lường tới): ghi ADR ngắn giải thích lý do, KHÔNG âm thầm sửa checklist.

---

## Verdict cuối cùng (chốt kickoff)

| Hạng mục | Trạng thái |
|---|---|
| Architecture | ✅ Freeze |
| Roadmap | ✅ Freeze |
| Engineering kickoff | ✅ Cho phép bắt đầu |
| Review kiến trúc tiếp theo | ❌ Dừng |

Từ thời điểm này, giá trị lớn nhất đến từ code review, integration review, và production
validation theo từng WS — không phải thêm tài liệu kiến trúc. Bước kế tiếp: bắt đầu `#14` theo
Implementation Order đã khoá trong `OMNI_V2_FINAL_EXECUTION_GATE.md` mục 6, PR đầu tiên chỉ chứa
`#14`, đối chiếu Definition of Done ở mục A phía trên trước khi coi là xong.
