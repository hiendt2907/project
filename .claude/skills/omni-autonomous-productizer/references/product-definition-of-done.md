# Product Definition of Done

Một capability chỉ được gắn nhãn **DONE** khi TẤT CẢ mục sau đúng (thiếu 1 mục → dùng **PARTIAL**,
trừ khi mục đó thực sự không áp dụng và được giải thích rõ trong PRODUCT_PROOF):

- [ ] Domain behavior đúng
- [ ] Canonical runtime wiring (không phải legacy/dead-code path)
- [ ] Persistence đúng (đúng datastore, đúng key/schema, không mất dữ liệu)
- [ ] Tenant isolation (không cross-tenant contamination)
- [ ] Idempotency/concurrency phù hợp (chạy lại không phá state)
- [ ] Failure handling (không silent swallow, có log/retry/escalate rõ ràng)
- [ ] Observability (log/metric/trace đủ để debug production sau này)
- [ ] API/UI/operator visibility (không chỉ raw Redis/DB access)
- [ ] Targeted tests (unit cho logic mới)
- [ ] Integration/E2E test (khi golden-journey liên quan)
- [ ] Built from traceable HEAD (commit SHA truy vết được)
- [ ] Deployed (image digest verify trên pod thật, không chỉ build local)
- [ ] Runtime event cycle observed (ít nhất 1 full cycle thật, xem operating-model.md)
- [ ] Product scenario demonstrated (đi qua golden journey thật, không mock)
- [ ] Documentation synchronized (CLAUDE.md/CURRENT_SESSION/PRODUCT_PROOF khớp runtime)
- [ ] Rollback path rõ ràng (biết cách revert nếu sai)
- [ ] PRODUCT_PROOF.md updated (dòng mới hoặc cập nhật dòng cũ, có evidence cụ thể)
- [ ] Commit checkpoint (diff sạch, không lẫn file không liên quan)

## Không dừng ở các tín hiệu giả (false-positive DONE signals)

- Code tồn tại trong repo
- Unit test pass
- Image build thành công (local)
- Pod ở trạng thái `Running`
- Health endpoint trả 200
- Redis key xuất hiện (không đồng nghĩa với đúng nội dung/đúng tenant/đúng thời điểm)

Một capability chỉ thực sự DONE khi: runtime thật hoạt động, tenant isolation đúng, failure
observable, operator nhìn thấy, product journey demo được, evidence được lưu, documentation khớp,
rollback path rõ.
