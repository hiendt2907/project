# Runbook — di trú lane → domain (Phase 3)

Điểm không quay lại của `plans/lane-to-domain-and-omni-decides-2026-07-30.md`.
**Đọc hết trước khi chạy lệnh đầu tiên.**

Rủi ro duy nhất đáng sợ ở đây: `pattern_key = sha256("lane|alertname")[:32]` là khoá
của `omni_admin.scope_grant` — **quyền khách hàng đã duyệt**. Đổi lane sang domain mà
khoá không đi theo thì grant ngừng khớp **không có exception, không có log**. Omni chỉ
đơn giản mất quyền và quay lại xin. Vì thế mọi bước dưới đây đều có bước kiểm ngay sau.

Điều kiện tiên quyết: Phase 1–2 đã xanh trên cluster thật. PostgreSQL ≥ 11.

---

## 0. Dump trước, luôn luôn

```bash
NS=multi-agent
kubectl -n $NS exec omni-postgres-0 -- \
  pg_dump -U omni -d omnidb -n omni_admin --data-only \
  -t omni_admin.scope_grant -t omni_admin.scope_request -t omni_admin.case_ledger \
  > /tmp/omni_admin_pre_0014.sql
wc -l /tmp/omni_admin_pre_0014.sql   # phải > 0

# Redis: chỉ cần danh sách khoá KPI theo lane (dữ liệu là ZSET cửa sổ 24h)
kubectl -n $NS exec redis-0 -- redis-cli --scan --pattern 'omni:kpi:detected:*' \
  > /tmp/kpi_keys_pre_0014.txt
kubectl -n $NS exec redis-0 -- redis-cli --scan --pattern 'omni:kpi:resolved:*' \
  >> /tmp/kpi_keys_pre_0014.txt
```

## 1. Postgres — migration 0014

`run_migrations()` apply mọi `*.sql` mỗi lần worker `full/analyst/onboarding` khởi
động, nên **không phải chạy tay** trong trường hợp thường:

```bash
kubectl -n $NS rollout restart deployment/omni-fullstack
kubectl -n $NS logs deployment/omni-fullstack | grep '0014_lane_to_domain'
```

Muốn chạy tay (khuyến nghị lần đầu, để đọc `NOTICE`):

```bash
kubectl -n $NS exec -i omni-postgres-0 -- psql -U omni -d omnidb \
  --single-transaction -v ON_ERROR_STOP=1 \
  < migrations/omni_admin/0014_lane_to_domain.sql
# NOTICE: 0014: scope_grant viet lai=N giu nguyen khoa cu=M
```

> ⚠️ **`--single-transaction` là bắt buộc, không phải tuỳ chọn cho đẹp** (đã trả giá
> 2026-07-30). `0013_graduation_track_and_domain_check.sql` dùng
> `CREATE TEMP TABLE _domain_alias ... ON COMMIT DROP`: chạy `psql` ở chế độ autocommit
> thì bảng tạm bị xoá ngay sau câu `CREATE`, câu `INSERT` kế tiếp báo
> `relation "_domain_alias" does not exist`, và migration **đã áp một phần** trước khi
> vỡ — trạng thái nửa vời, khó lần. `run_migrations()` lúc worker khởi động không gặp
> lỗi này vì nó tự bọc transaction; chỉ đường chạy tay mới vướng.
> `ON_ERROR_STOP=1` để không âm thầm chạy tiếp sau câu lỗi đầu tiên.

Migration làm gì:

| bảng | thay đổi |
|---|---|
| `case_ledger` | +`domain` (backfill từ `lane`), +`pattern_key_legacy` (=`pattern_key`), +`pattern_key_domain` (khoá mới). `pattern_key` **không bị sửa** |
| `scope_grant` | +`pattern_key_legacy`; `pattern_key` **được viết lại** sang khoá domain khi bản đồ phục hồi được từ `case_ledger` |
| `scope_request` | +`pattern_key_legacy` (để cooldown còn khớp) |
| mới | `scope_grant_premigration_0014` (ảnh chụp), `migration_0014_state` (mốc chạy một lần) |

Không xoá cột `lane` — đó là Phase 4.

## 2. Kiểm ngay: không mất grant nào

```bash
make verify-case-ledger
```

Nhóm **E** là gate quan trọng nhất: `grant khớp được TRƯỚC=N SAU=N`. Nếu con số SAU
nhỏ hơn → **dừng, rollback ngay** (mục 5). Gate cũng đối chiếu bản đồ lane→domain và
hàm hash trong SQL với `pkg.domain.taxonomy` / `advisory_pattern_key()`: lệch một ký
tự là khoá mới không bao giờ khớp khoá Python sinh lúc chạy.

## 3. Redis — khoá KPI

Dry-run trước, **luôn**:

```bash
kubectl -n $NS port-forward svc/redis 16379:6379 &
.venv/bin/python scripts/kpi_lane_to_domain_migrate.py
```

Đọc báo cáo. Chú ý dòng `⚠️ GỘP VÀO 'unknown'`: `SYS_HARD_FAIL` và
`ONBOARDING_DISCOVERY` mất thông tin domain và **không tách lại được** — cố ý không
phân bổ đoán sang `database`/`storage`/`service`. Nếu số bản ghi bị gộp lớn và số liệu
đó đang được dùng để báo cáo cho khách, quyết định "chấp nhận gộp" phải là quyết định
của người, không phải mặc định của script.

```bash
.venv/bin/python scripts/kpi_lane_to_domain_migrate.py --apply
```

Kiểm sau:

```bash
kubectl -n $NS exec redis-0 -- redis-cli --scan --pattern 'omni:kpi:detected:*'
# không còn khoá nào kết thúc bằng SYS_RESOURCE/APP_HTTP/SIEM_SECURITY/...
curl -s localhost:8090/kpi | head   # hoặc trang KPI của portal: số không được về 0
```

## 4. RAG + Prometheus (bước 3–4 của Phase 3)

Chưa nằm trong runbook này. RAG: 4 file JSONL + `omni:rag:sop`, verify `HLEN` sau ≥
trước (1019). Prometheus: nhãn `lane` trong `k8s/monitor/prometheus.yaml`.

## 5. Rollback

Postgres — khôi phục đúng khoá cũ từ ảnh chụp, rồi xoá mốc để có thể chạy lại:

```sql
UPDATE omni_admin.scope_grant g
   SET pattern_key = g.pattern_key_legacy
  FROM omni_admin.scope_grant_premigration_0014 s
 WHERE s.tenant_id = g.tenant_id
   AND s.pattern_key = g.pattern_key_legacy
   AND g.pattern_key <> g.pattern_key_legacy;

DELETE FROM omni_admin.migration_0014_state;
```

Các cột thêm vào có thể **để nguyên** — chúng chỉ mô tả, không ảnh hưởng đường đọc cũ
(`ScopeStore.get_grant` / `CaseLedgerStore` tự lùi về tra một khoá khi thiếu cột).
Nếu vẫn muốn dọn sạch: `ALTER TABLE ... DROP COLUMN IF EXISTS pattern_key_legacy;`
(và `domain`, `pattern_key_domain` ở `case_ledger`), rồi `DROP TABLE
omni_admin.scope_grant_premigration_0014, omni_admin.migration_0014_state;`.
**Đừng** rollback bằng cách restore toàn bộ `case_ledger` từ dump: sổ ca là bằng
chứng append-mostly, restore đè sẽ mất những ca mở sau lúc dump.

Redis: không rollback được (ZSET đã gộp, cửa sổ 24h). Chấp nhận, hoặc chờ 24h cho
cửa sổ tự trôi. Đây là lý do dry-run là mặc định.

## 6. Cửa sổ chuyển tiếp đóng khi nào

`ScopeStore.get_grant` và `CaseLedgerStore.list_cases_for_pattern` /
`last_case_for_pattern` đang tra **cả hai** khoá. Chỉ bỏ nhánh legacy khi:

1. `SELECT count(*) FROM omni_admin.scope_grant WHERE pattern_key <> pattern_key_legacy`
   ổn định (không còn hàng nào chờ di trú), và
2. Phase 4 đã cắt lane trục A khỏi `build_envelope`.

Bỏ sớm là mất quyền âm thầm — đúng thứ toàn bộ Phase 3 dựng lên để chặn.
