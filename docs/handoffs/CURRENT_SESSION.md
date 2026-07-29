# Current Session Handoff

## ✅ XONG (deliverable 2) — `ccd9a77`, CHƯA PUSH — taxonomy domain + catalogue lệnh
Thiết kế: `plans/unify-domain-and-diagnostic-catalog-2026-07-30.md`.

**Hiện trạng đã khảo sát (runtime, không suy diễn):** BA từ vựng "domain" không cầu nối
(`kubernetes`/`k8s`/`container_logs` — ba tên cho K8s); `playbook_graduation.domain` chứa
cả `k8s` (kỹ thuật, writer Redis) lẫn `advisory` (nguồn học, writer PG) và **thiếu CHECK**
đúng chỗ cần nhất → `list_playbook_graduations()` (dùng để **đề xuất nâng tier**) đếm gộp
hai thứ khác bản chất. Whitelist lệnh hardcode **ba chỗ**, một chỗ (`collectors/`) bỏ qua
validator hoàn toàn và đang chạy `cat` — chính lệnh nằm trong `_CONTENT_READ_BLOCKED`.

**Đã xong (tôi tự làm, là hợp đồng cho agent):**
- `src/pkg/domain/taxonomy.py` — 9 domain canonical + alias map cả 3 từ vựng cũ;
  tách `LearningTrack` (advisory|playbook|execution) khỏi domain
- `src/pkg/diagnostics/command_catalog.py` — loader khai báo, fail-closed ở tầng LOAD
  (`WRITE_VERBS` → entry khai động từ ghi thì load LỖI), `is_path_readable()` chống
  path traversal + chặn cứng thư mục dữ liệu DB/home/backup/secret

**Kết quả:** 6999 passed (baseline 6872) · migration 0013 áp lên cluster · gateway +
worker pod xác minh bằng `import` thật (99 lệnh, 9 domain, 9/9 case chặn/cho phép đúng).

**Con số then chốt:** `tier_loops` từng đếm **3 playbook tốt nghiệp** vào việc đề xuất
nâng tier — số thật là **0**. Ba hàng đó là `track=advisory`, không phải playbook.

**Phủ domain:** os_host 26 · network 18 · storage 12 · security 12 · application 10 ·
database 7 · hardware 6 · kubernetes 4 · service 4 = **99 lệnh**.

**3 lỗ hổng tự phát hiện + vá trong cùng đợt** (chi tiết ở commit message `ccd9a77`):
`_SECRETISH` neo sai làm `server.key` đọc được · `curl` trỏ host bất kỳ (thêm
`local_targets_only`, KHÔNG resolve hostname vì DNS rebinding) · `mysql` thiếu `select`
làm ProxySQL collector chết (mở `select` nhưng giới hạn schema hệ thống).

**Còn treo, cần user quyết:** `cat /etc/passwd` nay đọc được (`/etc` trong phạm vi;
`/etc/shadow` + khoá vẫn chặn) — cố ý theo thiết kế, nêu rõ để bạn biết.

### Lỗ hổng trong loader TÔI viết, agent catalogue tìm ra — đã sửa
`_SECRETISH` neo `(^|/)` nên `\.key`/`\.pem` chỉ khớp file **tên là** `.key`, không khớp
`server.key` → `/etc/ssl/private/server.key` và `/etc/pki/tls/cert.pem` **đọc được** qua
`cat` (vì `read_allow` có `/etc`). Khoá TLS là thứ đắt nhất trên host.
Sửa: tách **hai họ mẫu, hai cách neo** — `_SECRET_NAME` neo `(^|/)` cho tên, `_SECRET_EXT`
neo `$` cho đuôi, cộng hậu tố `_key` (OpenSSH đặt `ssh_host_rsa_key` không có đuôi), cộng
`_SECRET_DIRS` cho cây pki. Cố ý **không** chặn cả `/etc/ssh`: `sshd_config` có giá trị
chẩn đoán, chặn thừa cũng là hỏng vì đẩy người vận hành đi tìm đường lách.
`tests/test_diagnostic_catalog.py` **45 passed**.

### 3 rủi ro còn lại — thuộc tầng GỌI, agent rewire phải xử lý
1. **`psql` buộc có `select`** trong `statement_verbs`: chỉ số chẩn đoán Postgres nằm
   trong view (`pg_stat_activity`, `pg_locks`), không sau `SHOW`. Hàng rào phải là giới
   hạn schema `pg_catalog`/`pg_stat_*`/`information_schema`. MySQL KHÔNG có `select`.
2. **`awk` gọi được `system()` và `|`** — `deny_flags` không đủ, phải từ chối script
   chứa hai thứ đó.
3. **`ip`/`route`/`service` đặt động từ ghi ở slot POSITIONAL THỨ HAI** (`ip route add`)
   — whitelist token đầu không đủ, phải quét `deny_subcommands` ở MỌI vị trí.

### Agent catalogue chủ động loại (lý do đáng lưu)
`md5sum`/`sha256sum` → **oracle so nội dung** (đoán từng phần file bị chặn bằng cách so
hash) · `strace`/`gdb`/`bpftrace` → `ptrace` đọc secret plaintext trong RAM tiến trình ·
`socat EXEC:` chạy lệnh tuỳ ý · `wget` mặc định LÀ ghi file · `mysqldump`/`pg_dump` vi
phạm `INV_DATA_RESIDENCY` · trình thông dịch (`bash`/`python`/`xargs`) vô hiệu hoá cả
catalogue trong một dòng.

### ⚠️ ĐÁNH ĐỔI CẦN USER BIẾT
"Chạy **toàn bộ** lệnh chẩn đoán" **không tương thích** với `INV_NO_DATA_EXFIL` dạng cũ
(chặn cả `cat`/`tail`/`grep`/`mysql`). Chẩn đoán thật cần đọc nội dung. Đã thay **chặn
theo tên lệnh** bằng **chặn theo phạm vi đọc** → invariant mới `INV_DIAG_SCOPE_BOUNDED`:
đọc được nhưng chỉ trong `/proc /sys /etc /var/log /run`; chặn cứng `/var/lib/mysql`,
`/home`, `/root`, backup, file kiểu secret. Đây là **nới lỏng có thật**, đổi lấy năng lực
chẩn đoán tầng app. Nếu user muốn giữ chặt như cũ thì Omni sẽ mãi không đọc được log
ứng dụng.

## Deliverable 1 (XONG)
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

## 6 subagent — TẤT CẢ ĐÃ XONG, đã tự kiểm chứng lại từng cái
1. ✅ Advisory 3 nút phán quyết + mở ca lúc phát + trí nhớ lần-N
2. ✅ HITL → sổ ca (`services/case_ledger/hitl_link.py`, cả Telegram lẫn HTTP).
   **Quyết định đúng của agent, tôi đã sai:** approve → `diagnosis=CORRECT` nhưng
   `remedy` để **UNJUDGED** — lúc duyệt thì hành động CHƯA chạy. Ghi CORRECT ở đó là
   Omni tự chấm phần khắc phục của mình. Nhãn `remedy` thuộc nguồn `world`.
3. ✅ Competency + đơn xin quyền + `/competency/*`
4. ✅ Test lõi — **tìm ra 6 lỗi trong file tôi viết**, xem mục dưới
5. ✅ Portal tenant: 2 trang + 3 route BFF. Agent PHẢI sửa Python và báo cáo rõ —
   nó đúng: tenant portal gọi thẳng BFF `src/aoip/console/app.py`, không qua gateway.
   Nhưng nó **không viết test** cho 3 route mới → tôi tự viết 9 test (đây đúng là bề
   mặt từng chứa lỗ hổng cross-tenant của `/hitl/{id}/decide`).
6. ✅ Vòng lặp tự xin quyền định kỳ (`scope_advocacy_loop`)

### 6 lỗi trong code TÔI viết, do agent test tìm ra — đã sửa hết
| | |
|---|---|
| **Race `occurrence_no`** (nặng nhất) | READ COMMITTED: 2 ca cùng pattern mở đồng thời đều đọc cùng "ca gần nhất" rồi cùng +1. Xảy ra đúng lúc alert dồn dập, hỏng **âm thầm**. Sửa: `pg_advisory_xact_lock` + unique index + gate mới trong verify script |
| Nguồn/actor dùng chung 1 cột | Chấm `remedy` sau **xoá dấu vết** ai chấm `diagnosis`. Sửa: tách `diagnosis_*`/`remedy_*` |
| posture lạ → tính là DIAGNOSED | Phồng mẫu số độ chính xác. Sửa: đếm riêng + chặn eligible |
| verdict không validate ở Python | Ném asyncpg thô. Sửa: ValueError tại chỗ |
| blocker trùng lặp + sai nguyên nhân | Nhiễu cho admin khách. Sửa |

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

## ✅ ĐÃ COMMIT — `cb63b0b` · `497612b` · `f40f1c7`. **CHƯA PUSH** (chờ user xem).
Full suite **6872 passed** (baseline 6750, +122) · `make verify-case-ledger` **16/16 trên
PG thật** · tenant-portal build xanh · gitleaks sạch.

### Vòng lặp tự xin quyền — đã chứng minh chạy thật trên cluster
Bật cờ trên `omni-fullstack`, nạp 35 ca (29/30 đúng + 5 từ chối), khởi động lại →
Omni **tự đánh giá và tự nộp đơn**, không ai gọi:
```
scope_advocacy: tenant=default patterns=1 requests=1 (pat-LOOP->HITL_REQUIRED)
scope_request: pat-LOOP HITL_REQUIRED PENDING lb=0.8333 cov=0.8571
```
**Đã dọn sạch dữ liệu thử VÀ gỡ env override** — `OMNI_SCOPE_ADVOCACY_ENABLED` nay
không đặt trên Deployment (fail-closed, đúng bài học post-mortem kill-switch bị bỏ quên
ở trạng thái bật). Muốn nghiệm thu lại phải bật có ý thức.
⚠️ Loop **im lặng khi chạy thành công mà không có gì để xin** — vắng log KHÔNG chứng
minh nó không chạy. Kiểm bằng hành vi (bảng `scope_request`), đừng kiểm bằng log.

### Hành vi thật đã chứng minh trên cluster (không phải mock)
Nạp 4 pattern vào Postgres, hỏi **gateway đã deploy**:
| pattern | dữ liệu | kết quả |
|---|---|---|
| pat-B | 3/3 đúng | cận dưới 0.44 → **TRƯỢT** (vài ca may mắn ≠ bằng chứng) |
| pat-C | 4 đúng / 20 từ chối | chính xác thô **100%** nhưng phủ 0.17 → **TRƯỢT 2 lý do** |
| pat-A | 11/12 đúng | cận dưới 0.65 → TRƯỢT (sát ngưỡng 0.70) |
| pat-D | 29/30 + 6 từ chối | cận dưới 0.83, phủ 0.83 → **ĐỦ ĐIỀU KIỆN** |

Chuỗi tự xin quyền chạy thật trong pod: Omni xin đúng 1 pattern, đúng **1 bậc**
(SUGGEST_ONLY→HITL_REQUIRED) → admin từ chối qua HTTP → **cooldown chặn xin lại** →
hết khoá xin lại được → duyệt → ghi `scope_grant`. Dữ liệu thử đã dọn sạch.

Worker pod: 3 nút phán quyết (`✅ Đúng` / `❌ Sai` / `🟡 Đúng nhưng thiếu`) sống thật,
callback cũ 1-nút vẫn parse được (trả verdict=None, không học).

### Bẫy mới trả giá
`make deploy-gateway` báo **"rollout successful" nhưng pod KHÔNG có `services.case_ledger`**
— `Dockerfile.gateway` copy TỪNG thư mục con của `services/`, không copy cả cây. Chỉ lộ ra
khi `import` module trong pod đang chạy. Cùng lớp bẫy với `COPY src/aoip/` thiếu trước đây.
**Thêm module mới dưới `src/services/` ⇒ phải sửa `Dockerfile.gateway`.**

## Working tree — phần CHƯA COMMIT
Mới (untracked): `plans/case-ledger-design-2026-07-30.md` ·
`migrations/omni_admin/0012_case_ledger.sql` · `src/services/case_ledger/` ·
`scripts/verify_case_ledger.sh` · các file test/agent đang sinh.
Sửa: `Makefile` (target `verify-case-ledger`) · `docs/handoffs/CURRENT_SESSION.md`.
4 subagent đang ghi thêm vào `src/workers/`, `src/gateway/`, `tests/`.
Memory đã ghi: `project_omni_vision_employee_not_tool`, `project_learning_loop_broken_labels`.

## Next step
1. **User xem rồi quyết có push không** — 3 commit nằm local trên `main`.
2. Chưa làm (cố ý, cần quyết định sản phẩm):
   - Thẻ Telegram "lần thứ N" chưa được nghiệm thu bằng một sự cố **thật** đi qua
     pipeline — mới verify ở mức hàm trong pod. Cần một lần alert lặp thật.
   - `remedy_verdict` hiện chỉ có thể do `world` chấm (qua `recurred`). Chưa có
     nguồn nào chấm "làm theo rồi có hết không" ngoài việc sự cố tái diễn.
   - Ngưỡng `min_accuracy_lb=0.70` / `min_coverage=0.50` / cooldown 14 ngày đang
     hard-code trong `scoring.py`/`advocacy.py`. Nên cho tenant cấu hình.
   - Thang quyền `SUGGEST_ONLY→HITL_REQUIRED→AUTO_EXECUTE` do agent tự đặt (migration
     không CHECK giá trị). Nếu sản phẩm đã có tên bậc khác thì sửa `SCOPE_LADDER`.
   - Chưa nối sổ ca với `posture=OUT_OF_SCOPE` (chẩn đoán một lượt rồi giao admin).

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
