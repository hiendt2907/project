# Evidence Policy

## Taxonomy — mỗi capability chỉ dùng đúng MỘT nhãn

| Nhãn | Ý nghĩa |
|---|---|
| `VERIFIED_RUNTIME` | Quan sát trực tiếp hành vi thật đang chạy (log/API/datastore đúng lúc đúng chỗ) |
| `VERIFIED_DEPLOYMENT` | Image/digest/config verify đã deploy đúng, nhưng chưa quan sát hành vi runtime đầy đủ |
| `VERIFIED_TEST` | Test tự động pass, chưa deploy/chưa quan sát runtime |
| `CODE_ONLY` | Code tồn tại, chưa test/chưa deploy |
| `PARTIAL` | Một phần chuỗi có evidence, phần còn lại thiếu |
| `CONTRADICTED` | Evidence runtime mâu thuẫn với tài liệu/claim trước đó |
| `BLOCKED` | Không thể verify vì thiếu quyền/tài nguyên/thời gian — ghi rõ lý do |
| `ABSENT` | Không tồn tại |
| `UNKNOWN` | Chưa kiểm tra |

Không được ghi "đã kiểm tra" mà không nêu evidence cụ thể đi kèm.

## Evidence hợp lệ (một hoặc nhiều)

file:symbol · commit SHA · tên test + kết quả · rendered manifest · image digest · running process
(pid/pgrep) · log pattern (dòng log thật, có timestamp) · Kafka offset/lag số liệu · Redis
key/revision giá trị thật · database row/migration · API response (curl output thật) · VM command
output (`orb -m ...`) · UI/operator view screenshot hoặc mô tả cụ thể.

## Testing order

1. formatter/linter
2. type check
3. targeted unit
4. contract
5. persistence/concurrency
6. integration
7. end-to-end lab
8. relevant regression
9. full suite khi phù hợp

Mỗi test report phải ghi rõ: điều gì ĐÃ chứng minh, điều gì CHƯA chứng minh.

### Flaky test handling

Tái chạy có kiểm soát (isolate, chạy riêng lẻ). Xác định pre-existing bằng bằng chứng cụ thể
(`git stash` + chạy lại trên commit trước, hoặc chạy trên `main`) — không gọi "pre-existing" theo
cảm tính. Nếu flaky che critical path golden journey → fix hoặc quarantine rõ ràng, không lờ đi.

## Build/deployment checklist

1. Build từ đúng HEAD (`git rev-parse HEAD` khớp commit build).
2. Tag artifact bằng commit SHA hoặc identifier truy vết được.
3. Deploy qua Makefile/Helm/Kustomize/source-of-truth canonical (không `kubectl set image` tay).
4. Chờ rollout hoàn tất (`kubectl rollout status`).
5. Verify image digest thật trên pod (`kubectl get pod -o jsonpath='{.spec.containers[*].image}'`
   hoặc `imageID`).
6. Verify effective env (`kubectl exec ... env` hoặc tương đương) — không giả định từ manifest.
7. Verify role/entrypoint đúng (`OMNI_WORKER_ROLE` etc).
8. Verify migrations đã chạy (nếu có).
9. Verify health/readiness (`/healthz`, `/readyz`).
10. Verify Kafka/Redis consumer không duplicate (consumer group count).
11. Verify `OMNI_AUTO_EXECUTE_ENABLED` vẫn `false`.

Ghi lại: Source commit / Artifact / Image tag / Image digest / Deployment / Rollout result /
Effective config / Rollback command.

## Runtime validation — full event cycle

Xem `operating-model.md` mục "Runtime validation". Nếu bất kỳ mũi tên nào trong chuỗi event không
có evidence → capability đó là `PARTIAL`.

## Template

Dùng `templates/runtime-evidence.md` để ghi evidence có cấu trúc cho mỗi bottleneck đã fix, và
`templates/product-proof-row.md` khi thêm dòng mới vào `docs/product/PRODUCT_PROOF.md`.
