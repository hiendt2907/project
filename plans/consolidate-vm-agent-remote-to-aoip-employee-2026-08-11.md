# Consolidate VM Agent: remote_agent → aoip.agent.employee (dứt điểm double-agent)

**Ngày tạo:** 2026-08-11
**Trạng thái:** DRAFT — chưa thực thi phase nào
**Phạm vi:** 3 VM lab OrbStack (`cust-app`, `cust-db`, `cust-edge`) + docs/scripts liên quan trong repo.
**KHÔNG đụng:** GCP k3s (Omni core), domain public `omnisre.xyz`, không build `aoip.agent.daemon`.

## Bối cảnh (đọc trước khi thực thi bất kỳ phase nào)

Hiện tại cả 3 VM lab đang chạy **2 process/unit song song**, cùng đọc chung
`/opt/omni-remote-agent/run.env`:

| Unit | Binary | Vai trò | Trạng thái sống (2026-08-11) |
|---|---|---|---|
| `omni-remote-agent.service` | `remote_agent.agent` | Runtime gốc, đã lỗi thời | `enabled + active` — SAI, phải là `disabled`/không tồn tại |
| `aoip-agent.service` | `aoip.agent.employee` (gọi `remote_agent.run_agent()` làm **library**, cộng thêm durable command daemon) | Runtime đích đã chốt (ADR-001, deploy thật từ Sprint IT-7) | `enabled + active` — ĐÚNG |

Hệ quả đang sống: double-fire toàn bộ evidence lane (metrics/logs/discovery) — mỗi probe
gửi 2 lần/chu kỳ từ 2 process độc lập cùng `agent_id`.

**Đây KHÔNG phải lần đầu bug này bị phát hiện.** `docs/architecture/AUDIT_autonomous_sre_team_2026_07_22.md`
dòng 34 và dòng 146 (mục Lane B) ghi nhận đã **"FIXED"** bằng đúng 1 lệnh
`systemctl disable --now omni-remote-agent.service` trên cả 3 VM, verify lúc đó là
"registry Redis chỉ còn 1 key/host". Bug đã **regress** — cả 2 unit đang `enabled + active`
trở lại. Phase 1 dưới đây điều tra vì sao, và Phase 4 thiết kế để bug này **không thể regress
lần nữa** (gỡ hẳn thay vì chỉ disable).

**⚠️ CẢNH BÁO SỐNG CÒN — đọc kỹ trước Phase 4:** `aoip.agent.employee` (`aoip-agent.service`)
và `remote_agent.agent` (`omni-remote-agent.service`) **CHIA SẺ CHUNG một thư mục cài đặt**:
`/opt/omni-remote-agent/` (chứa `remote_agent/`, `aoip/`, `pkg/`, `config/`, `venv/`, `run.env`).
`aoip.agent.employee` **import trực tiếp code `remote_agent`** làm thư viện
(`aoip/agent/employee.py:86-88` gọi `remote_agent.agent.run_agent()`). **"Gỡ omni-remote-agent"
nghĩa là gỡ SYSTEMD UNIT, KHÔNG BAO GIỜ được `rm -rf /opt/omni-remote-agent`** hay xoá thư mục
con `remote_agent/` bên trong — làm vậy sẽ phá luôn `aoip-agent.service` (nó cần đúng code đó
để chạy) và gây outage cả 3 VM cùng lúc. Toàn bộ Phase 4 chỉ thao tác trên
`/etc/systemd/system/omni-remote-agent.service` (file unit), không chạm thư mục cài đặt.

## Dependency graph

```
Phase 1 (điều tra root-cause regression, read-only)
   │
   ▼
Phase 2 (canary: stop omni-remote-agent trên 1 VM, verify aoip-agent đủ sống)
   │
   ▼
Phase 3 (lặp lại stop trên 2 VM còn lại, verify từng VM)
   │
   ▼
Phase 4 (gỡ hẳn unit omni-remote-agent.service cả 3 VM — CHỈ SAU KHI Phase 2+3 xanh)
   │
   ├──▶ Phase 5a (sửa CLAUDE.md dòng 411 + ADR-001)          ─┐
   ├──▶ Phase 5b (sửa comment rollback-drill trong unit       │  có thể chạy song song
   │              aoip-agent.service + omni-agent-update-      │  với nhau, đều phụ thuộc
   │              fleet.sh)                                    │  Phase 4 xong
   └──▶ Phase 5c (ghi nợ kỹ thuật ADR-001 §5, không fix)      ─┘
             │
             ▼
        Phase 6 (verify tổng — exit criteria toàn bộ migration)
```

Phase 5a/5b/5c độc lập file, có thể giao cho 3 agent chạy song song sau khi Phase 4 xanh.
Phase 1-4 và Phase 6 phải chạy tuần tự (mỗi phase là tiền đề an toàn của phase sau).

---

## Phase 1 — Điều tra root-cause regression (read-only, không mutate VM)

**Mục tiêu:** Xác nhận (không suy đoán) vì sao fix 2026-07-22 (`disable --now`) không đứng vững.

**Bối cảnh cho agent lạnh:** Nghiên cứu trước khi viết plan này đã tìm thấy **3 nghi phạm cụ
thể** cho việc `omni-remote-agent.service` bị `enable`+`start` lại sau fix 2026-07-22 — liệt kê
theo độ khả tín giảm dần, KHÔNG được giả định nghi phạm #1 là đúng mà không kiểm chứng:

1. **`scripts/e2e_onboarding_full_flow.py` dòng 345-347** — chạy vô điều kiện:
   ```
   _orb("systemctl", "daemon-reload")
   _orb("systemctl", "enable", "omni-remote-agent")
   _orb("systemctl", "start", "omni-remote-agent", timeout=15)
   ```
   Đây là script E2E test — nếu từng chạy nhắm vào 1 trong 3 VM lab thật (thay vì VM test dùng
   một lần), nó **enable + start lại chính xác cả 2 thứ** đang thấy hôm nay trong 1 lần chạy.
   Đây là nghi phạm khả tín NHẤT vì không cần ai "quên" gì — chỉ cần script này chạy nhầm mục
   tiêu 1 lần.
2. **`scripts/enroll_remote_agent.py` dòng 41-42, 114** — `UNIT = "omni-remote-agent"`, chạy
   `systemctl restart` unit này mỗi lần enroll/re-enroll 1 agent. `restart` không tự set
   `enabled=true` nếu unit đang `disabled`, nhưng NẾU unit đã bị #1 enable lại trước đó, mọi lần
   enroll sau đó qua script này sẽ tiếp tục giữ nó `active`.
3. **Quy trình rollback thủ công** ghi trong comment của `/etc/systemd/system/aoip-agent.service`
   trên VM thật (đọc qua `orb -m cust-app sudo systemctl cat aoip-agent`): *"Rollback drill:
   `systemctl disable --now aoip-agent && systemctl enable --now omni-remote-agent` — unit cũ
   giữ nguyên trên VM, không xóa trong pilot."* — giả thuyết YẾU HƠN #1/#2 vì đòi hỏi 1 người vận
   hành chủ động chạy VÀ quên bước hoàn tác.

**Việc cần làm:**
1. Kiểm tra xem `scripts/e2e_onboarding_full_flow.py` có từng được chạy nhắm vào 1 trong 3 VM lab
   thật hay không — tìm trong git log/commit history của repo (message nhắc script này), lịch sử
   phiên làm việc (`docs/handoffs/`), hoặc `orb -m <vm> sudo journalctl -u
   omni-remote-agent.service --no-pager | grep -E "Started|Enabled"` để lấy mốc thời gian
   enable/start gần nhất trên từng VM — đối chiếu mốc đó với thời điểm bất kỳ ai từng chạy script
   #1 hoặc #2. Journald có thể đã rotate mất log cũ — nếu không tìm được bằng chứng thời gian
   trực tiếp, ghi rõ "không xác nhận được bằng log, chỉ xác nhận được CÓ code-path khả dĩ gây ra
   lỗi này" — KHÔNG được khẳng định nghi phạm nào là "root cause xác nhận" nếu chỉ có bằng chứng
   gián tiếp (code tồn tại), phải phân biệt rõ "có khả năng gây ra" vs "đã xác nhận đã gây ra".
2. Grep toàn repo — **không giới hạn cụm `--now`**, vì nghi phạm #2 dùng `restart` chứ không
   phải `enable --now`: `grep -rln "omni-remote-agent" scripts/ docs/architecture/ docs/product/
   tests/` — liệt kê MỌI file match (đã biết trước tối thiểu: `scripts/enroll_remote_agent.py`,
   `scripts/e2e_onboarding_full_flow.py`, `scripts/omni-agent-update-fleet.sh`,
   `scripts/e2e_orbstack_fleet.py`, `scripts/aoip-agent.service`, `scripts/aoip-agent-guard.sh`,
   `scripts/deploy_aoip_gate_config.sh` — kiểm tra từng file xem có thao tác `enable`/`start`/
   `restart` unit `omni-remote-agent` hay chỉ nhắc tên suông) — để Phase 5b xử lý toàn bộ, không
   chỉ 1-2 file đã biết.
3. Viết phát hiện vào file mới `docs/audit/regression_agent_dual_process_2026-08-11.md` —
   với từng nghi phạm: có bằng chứng thời gian trực tiếp hay chỉ có code-path khả dĩ, và tại sao
   Phase 4 (gỡ hẳn thay vì chỉ disable) triệt tiêu được toàn bộ lớp bug này về mặt cấu trúc — kể
   cả nếu nghi phạm #1/#2 chạy lại trong tương lai, `systemctl enable/start/restart
   omni-remote-agent` sẽ lỗi thẳng "unit not found" thay vì âm thầm hồi sinh process.

**Verify (bằng chứng, không phải "đã đọc xong"):**
- File `docs/audit/regression_agent_dual_process_2026-08-11.md` tồn tại, có trích dẫn cụ thể
  (đường dẫn file + số dòng hoặc log timestamp) cho từng nhận định.
- Không có thay đổi nào trên VM ở phase này (`git diff` trên VM run.env/unit files = không áp
  dụng, đây là repo-only phase; nhưng nếu agent lỡ tay chạy `systemctl` mutate gì trên VM ở
  phase này thì đó là SAI phạm vi).

**Rollback:** Không cần — phase này không mutate gì.

---

## Phase 2 — Canary: dừng omni-remote-agent trên 1 VM, verify aoip-agent tự sống đủ

**Phụ thuộc:** Phase 1 xong (không bắt buộc phase 1 phải "thành công" tuyệt đối, chỉ cần đã chạy
và có báo cáo — Phase 2 không phụ thuộc kết quả điều tra root-cause).

**VM canary:** `cust-app` (đã có drill thật gần nhất trong phiên trước — payment-api.service —
nên có baseline behavior để so sánh).

**Việc cần làm:**
1. Baseline TRƯỚC khi đổi gì: `orb -m cust-app sudo tail -5 /var/log/omni-agent.log`, ghi lại
   agent_id/tenant đang dùng, và đếm số dòng "emitted evidence" trong 60s để có baseline
   tần suất (nếu double-fire, baseline sẽ gần gấp đôi tần suất chu kỳ thật của
   `OMNI_AGENT_COLLECT_INTERVAL` — hiện là 20s theo `run.env`).
2. `orb -m cust-app sudo systemctl stop omni-remote-agent.service` — **CHỈ `stop`, KHÔNG
   `disable`, KHÔNG xoá gì** ở bước này (đảo ngược tức thời bằng `systemctl start` nếu có vấn đề).
3. Verify aoip-agent MỘT MÌNH vẫn đủ chức năng, tối thiểu:
   - `systemctl is-active aoip-agent` → `active`, không restart bất thường
     (`systemctl show aoip-agent -p NRestarts`)
   - Log `/var/log/omni-agent.log` vẫn có `registered agent_id=...` + `emitted evidence` đều đặn,
     tần suất giảm còn ĐÚNG 1 lần/chu kỳ (không còn double)
   - Redis registry `omni:remote_agent:registry:<agent_id>` (qua
     `kubectl exec -n multi-agent redis-0 -- redis-cli GET ...` trên GCP UAT) vẫn cập nhật
     `last_seen` liên tục
   - **Drill thật tối thiểu 1 lần**: dừng 1 service thật trên `cust-app` (vd `payment-api.service`
     — đã có tiền lệ trong phiên trước) → xác nhận evidence tới Omni, `diagnosis_loop` khởi động
     với `agent_online=True` cho đúng agent_id (không phải facts-only) — đây là bằng chứng
     "employee một mình vẫn tiếp nhận/vận hành/xử lý" đúng như mục tiêu đã chốt.
4. Soak tối thiểu: theo dõi liên tục **30 phút** không có `Restart`, không có gap `last_seen`
   > 60s. (Ngắn hơn 24h soak của IT-7 gốc vì đây không phải cài mới — chỉ tắt 1 process trùng
   lặp mà aoip-agent chưa từng phụ thuộc runtime-wise; nếu muốn soak dài hơn cho chắc, tăng
   thời gian ở bước này trước khi sang Phase 3, đây là quyết định vận hành có thể điều chỉnh).

**Verify:**
- `systemctl is-active omni-remote-agent` = `inactive`, `aoip-agent` = `active`
- Không có dòng log lỗi mới trong `/var/log/omni-agent.log` liên quan tới việc thiếu
  `remote_agent.agent` process (aoip-agent chỉ IMPORT code đó, không cần process riêng chạy)
- 1 drill thật (service down) → thấy trace `diagnosis_loop` với `agent_online=True` trong log
  `kubectl logs -n multi-agent deploy/omni-fullstack`

**Rollback:** `orb -m cust-app sudo systemctl start omni-remote-agent.service` — khôi phục tức
thời, không mất dữ liệu (chưa `disable`/xoá gì).

---

## Phase 3 — Lặp lại trên 2 VM còn lại (cust-db, cust-edge)

**Phụ thuộc:** Phase 2 xanh hoàn toàn trên `cust-app` (không có bất kỳ dấu hiệu bất thường nào
trong 30 phút soak).

**Việc cần làm:** Lặp lại đúng bước 2-3 của Phase 2 (stop, verify, soak 30 phút) cho `cust-db`
rồi `cust-edge`. Lưu ý khác biệt theo VM:
- `cust-db`: verify thêm collector `database` (MySQL/Redis local) vẫn hoạt động qua aoip-agent
  một mình — không có gì đặc biệt về logic stop, chỉ khác domain evidence để verify.
- `cust-edge`: verify thêm collector `network`/`security` tương tự.

**Verify:** Giống Phase 2, lặp cho từng VM. Cả 3 VM đồng thời phải xanh trước khi sang Phase 4.

**Rollback:** Như Phase 2, per-VM (`systemctl start omni-remote-agent.service` trên VM tương ứng).

---

## Phase 4 — Gỡ hẳn unit `omni-remote-agent.service` (KHÔNG xoá code dùng chung)

**Phụ thuộc:** Phase 2 + Phase 3 xanh trên CẢ 3 VM. Không được chạy phase này nếu bất kỳ VM nào
còn dấu hiệu bất thường từ soak. **Gate bắt buộc trước khi làm gì khác:** với mỗi VM, chạy
`orb -m <vm> sudo systemctl is-active omni-remote-agent.service` — PHẢI ra `inactive` (không
phải "active", không phải lỗi khác). Nếu bất kỳ VM nào không phải `inactive`, DỪNG — quay lại
Phase 2/3 cho VM đó, không được tự ý stop rồi tiếp tục luôn trong Phase 4.

**⚠️ Nhắc lại cảnh báo đầu file:** chỉ xoá **unit file**, không đụng
`/opt/omni-remote-agent/remote_agent/` hay bất kỳ thư mục con nào khác trong
`/opt/omni-remote-agent/` — `aoip-agent.service` vẫn cần nguyên thư mục này để chạy.

**Việc cần làm, trên từng VM (`cust-app`, `cust-db`, `cust-edge`):**
```bash
# Bước 0 — BẮT BUỘC trước rm: backup unit file thật (không có template sẵn trong repo khớp
# 100% unit đang chạy — scripts/omni-agent-install.sh KHÔNG chứa định nghĩa unit
# omni-remote-agent.service, chỉ scripts/aoip-agent.service tồn tại và đó là unit KHÁC).
orb -m <vm> sudo systemctl is-active omni-remote-agent.service
# Kỳ vọng: "inactive" (xác nhận đã stop từ Phase 2/3 — KHÔNG được tiếp tục nếu ra "active")
orb -m <vm> sudo systemctl cat omni-remote-agent.service > /tmp/omni-remote-agent-<vm>.service.bak
# Copy file backup này về máy điều khiển (không để lại duy nhất trên VM) VÀ commit 1 bản vào
# repo tại docs/audit/backup-units/omni-remote-agent-<vm>-2026-08-11.service.bak — đây là
# đường rollback thật duy nhất sau bước rm dưới đây.

orb -m <vm> sudo systemctl disable omni-remote-agent.service
orb -m <vm> sudo rm -f /etc/systemd/system/omni-remote-agent.service
orb -m <vm> sudo systemctl daemon-reload
orb -m <vm> sudo systemctl reset-failed
```
KHÔNG chạy bất kỳ lệnh `rm -rf` nào nhắm vào `/opt/omni-remote-agent` hay thư mục con của nó.

**Verify (bắt buộc từng VM):**
- `orb -m <vm> sudo systemctl status omni-remote-agent.service` → `Unit omni-remote-agent.service
  could not be found.` (không phải "inactive", phải là "not-found")
- `orb -m <vm> ls /opt/omni-remote-agent/remote_agent/` → thư mục VẪN CÒN NGUYÊN (bằng chứng
  code dùng chung không bị xoá nhầm)
- `orb -m <vm> sudo systemctl is-active aoip-agent` → vẫn `active`, không bị ảnh hưởng
- Lặp lại 1 drill thật (service down) trên từng VM sau khi gỡ — xác nhận pipeline vẫn end-to-end
  đúng như Phase 2 đã verify (không có gì khác biệt về hành vi so với lúc mới `stop`, vì
  `disable`+xoá unit file không đổi runtime, chỉ ngăn future-start).

**Rollback (nếu phát hiện vấn đề SAU khi đã gỡ):** Copy lại file backup đã commit ở Bước 0
(`docs/audit/backup-units/omni-remote-agent-<vm>-2026-08-11.service.bak`) về
`/etc/systemd/system/omni-remote-agent.service`, `daemon-reload`, `enable --now`. KHÔNG có
nguồn nào khác trong repo để tái tạo unit này (đã xác nhận `scripts/omni-agent-install.sh` không
chứa định nghĩa unit này) — nếu Bước 0 bị bỏ qua, KHÔNG CÒN đường rollback nào ngoài viết lại unit
file thủ công từ trí nhớ/log. Đây là lý do Bước 0 là BẮT BUỘC, không phải tùy chọn. Đây là đánh
đổi đã được xác nhận với user (chấp nhận chậm hơn để loại bỏ hẳn lớp bug regression).

---

## Phase 5a — Sửa CLAUDE.md dòng 411 + cập nhật ADR-001

**Phụ thuộc:** Phase 4 xong trên cả 3 VM (để mô tả đúng trạng thái ĐÃ xảy ra, không phải dự định).

**Việc cần làm:**
1. `CLAUDE.md` dòng 411 hiện ghi: *"`omni-remote-agent.service` (tên khác `aoip-agent` — đừng
   tìm nhầm unit)"* — câu này sai (ngụ ý 2 tên chỉ là 1 unit). Sửa lại phản ánh đúng: đây là 2
   service riêng biệt, `omni-remote-agent.service` đã bị gỡ hẳn khỏi 3 VM lab từ 2026-08-11,
   runtime production duy nhất trên VM khách hàng bây giờ là `aoip-agent.service`
   (`aoip.agent.employee`).
2. `docs/architecture/ADR-001-canonical-agent-runtime.md`: thêm mục cập nhật mới (theo đúng
   format các mục "Cập nhật" đã có trong file) ghi nhận: migrate hoàn tất 2026-08-11,
   `omni-remote-agent.service` đã gỡ hẳn khỏi cả 3 VM (không còn giữ làm rollback path — khác
   với dự định ban đầu ở bản cập nhật IT-7), lý do đổi quyết định: giữ-disabled-làm-rollback đã
   từng gây chính bug double-agent này regress (xem
   `docs/audit/regression_agent_dual_process_2026-08-11.md` từ Phase 1).

**Verify:**
- `grep -n "tên khác .aoip-agent" CLAUDE.md` → không còn match (câu sai đã bị thay)
- `git diff CLAUDE.md docs/architecture/ADR-001-canonical-agent-runtime.md` — review thủ công
  nội dung đúng theo trên.

**Rollback:** `git checkout -- CLAUDE.md docs/architecture/ADR-001-canonical-agent-runtime.md`
(chưa commit thì đơn giản; nếu đã commit, revert commit tương ứng).

---

## Phase 5b — Dọn comment/script còn hướng dẫn quy trình rollback đã không còn hợp lệ

**Phụ thuộc:** Phase 4 xong. Độc lập với Phase 5a (file khác nhau), có thể chạy song song.

**Việc cần làm:**
1. Danh sách chỗ cần sửa = kết quả grep đầy đủ từ Phase 1 bước 2 (không chỉ cụm `--now`). Tối
   thiểu 2 file PHẢI sửa vì có thao tác `enable`/`start`/`restart` thật nhắm vào unit này (không
   chỉ nhắc tên suông):
   - **`scripts/enroll_remote_agent.py`** dòng 41-42 (`UNIT = "omni-remote-agent"`) + dòng
     114-121 (`systemctl restart` + `is-active` check unit này sau mỗi lần push `run.env`). Sau
     Phase 4 unit không còn tồn tại → mọi lần enroll/re-enroll sẽ CRASH ở bước restart (tốt hơn
     silent — nhưng vẫn phải sửa để script hoạt động đúng). Đổi `UNIT = "omni-remote-agent"`
     thành `UNIT = "aoip-agent"`. Cập nhật comment dòng 10 (docstring nhắc
     "restart omni-remote-agent.service") theo.
   - **`scripts/e2e_onboarding_full_flow.py`** dòng 345-347 (`systemctl daemon-reload; enable
     omni-remote-agent; start omni-remote-agent`) — đây là nghi phạm #1 gây regression ở Phase 1.
     Đổi thẳng sang `enable`/`start` unit `aoip-agent` thay vì `omni-remote-agent`. Nếu script
     này còn dùng ở nơi khác giả định `omni-remote-agent` là unit chính (tìm thêm trong cùng
     file), sửa đồng bộ tất cả.
   - Các file còn lại trong danh sách grep (`scripts/omni-agent-update-fleet.sh`,
     `scripts/e2e_orbstack_fleet.py`, `scripts/aoip-agent.service`,
     `scripts/aoip-agent-guard.sh`, `scripts/deploy_aoip_gate_config.sh`, các file `docs/`) —
     kiểm tra từng cái: nếu chỉ nhắc tên trong comment/doc mô tả lịch sử thì để nguyên hoặc sửa
     nhẹ cho rõ nghĩa lịch sử; nếu có thao tác `systemctl` thật thì xử lý như 2 file trên.
2. `scripts/omni-agent-update-fleet.sh`: script này quản lý lifecycle `omni-remote-agent.service`
   quanh việc update code (dòng 71-72 stop trước update, dòng 102-107 start lại sau nếu trước đó
   active). Sau Phase 4, unit này không còn tồn tại → `systemctl is-active
   omni-remote-agent.service` sẽ lỗi/trả "inactive" an toàn (không crash script), nhưng toàn bộ
   đoạn logic là dead code gây hiểu nhầm. Sửa:
   - Xoá đoạn stop/start quanh `omni-remote-agent.service` (dòng 71-72, 102-107).
   - **LƯU Ý QUAN TRỌNG:** dòng 76-78 của script này có `for p in "${PAYLOAD[@]}"; do orb -m "$m"
     sudo rm -rf "$INSTALL_DIR/$p"; done` với `PAYLOAD=(remote_agent aoip pkg config)` — dòng
     này **CỐ Ý và HỢP LỆ**, khác hẳn với cảnh báo "không rm /opt/omni-remote-agent" ở đầu plan.
     Đây là xoá thư mục con `remote_agent/` để giải nén code MỚI đè lên (routine code sync), xảy
     ra ngay trước khi `tar xzf` giải nén bản mới — không phải xoá vĩnh viễn. KHÔNG sửa/xoá dòng
     này. Chỉ cần thêm: `sudo systemctl stop aoip-agent.service` NGAY TRƯỚC vòng lặp `rm -rf` này
     (dòng 76) — vì `aoip-agent.service` đang chạy có thể đã load module `remote_agent` vào bộ
     nhớ, thay file dưới chân 1 process đang sống là không an toàn — rồi `systemctl start
     aoip-agent.service` sau khi giải nén xong xuôi + kiểm tra import thành công (giữa dòng 95
     và 100 hiện tại), thay cho logic start/stop cũ dựa trên `omni-remote-agent`.

**Verify:**
- `grep -rln "omni-remote-agent" scripts/ docs/architecture/ docs/product/ tests/` sau khi sửa —
  liệt kê lại toàn bộ match còn lại, xác nhận mỗi match chỉ còn ở dạng mô tả LỊCH SỬ (trong chính
  plan này, `docs/audit/regression_agent_dual_process_2026-08-11.md`, ADR-001 mục lịch sử) —
  không còn match nào có `systemctl enable/start/restart` thật đi kèm.
- Chạy thử `scripts/enroll_remote_agent.py --help` (hoặc tương đương dry-run nếu có) xác nhận
  không còn tham chiếu `omni-remote-agent` trong code path thật thực thi khi gọi.
- Đọc lại toàn bộ `scripts/omni-agent-update-fleet.sh` sau sửa: xác nhận dòng `rm -rf
  "$INSTALL_DIR/$p"` (payload `remote_agent`) vẫn còn nguyên (KHÔNG bị xoá nhầm), và logic
  stop/start mới xoay quanh `aoip-agent.service` thay vì `omni-remote-agent.service`.

**Rollback:** `git checkout -- scripts/omni-agent-update-fleet.sh scripts/aoip-agent.service`
(hoặc tương đương tên file thật tìm được ở bước 1).

---

## Phase 5c — Ghi nhận nợ kỹ thuật ADR-001 §5 (KHÔNG fix trong plan này)

**Phụ thuộc:** Không phụ thuộc phase nào khác về mặt kỹ thuật (đọc/ghi doc thuần), nhưng xếp sau
Phase 4 để gộp chung 1 đợt cập nhật tài liệu.

**Việc cần làm:** `docs/architecture/ADR-001-canonical-agent-runtime.md` §5 đã ghi nhận rủi ro
drift giữa `src/gateway/routes/agent_runtime.py` và `aoip.agent.delivery.DurableCommandChannel`
(2 bản triển khai độc lập cùng 1 command lifecycle — state transition, fencing, visibility
timeout, idempotency). ADR ghi rõ lý do kỹ thuật ban đầu (Gateway không import được `aoip`) đã
hết hiệu lực từ commit `409dcb2` nhưng việc hợp nhất **chưa được thực hiện**.

Thêm 1 dòng vào đầu ADR-001 (khu vực "Cập nhật") trỏ rõ: tính tới 2026-08-11, nợ kỹ thuật §5
**vẫn chưa fix**, cần task/ADR riêng — không nằm trong scope migration lần này (đúng theo ràng
buộc "không thêm tính năng mới" đã chốt). Không sửa code `agent_runtime.py` hay
`aoip/agent/delivery.py` trong phase này.

**Verify:** `git diff docs/architecture/ADR-001-canonical-agent-runtime.md` chỉ có thêm dòng
ghi chú, không có thay đổi nào ở code.

**Rollback:** `git checkout -- docs/architecture/ADR-001-canonical-agent-runtime.md` (nếu gộp
chung commit với 5a thì rollback cùng).

---

## Phase 6 — Verify tổng thể (exit criteria toàn bộ migration)

**Phụ thuộc:** Phase 4, 5a, 5b, 5c đều xong.

**Checklist (chạy lệnh thật, dán kết quả vào báo cáo, không tự nhận "done" nếu thiếu bằng chứng):**

1. Trên cả 3 VM (`cust-app`, `cust-db`, `cust-edge`):
   ```bash
   orb -m <vm> sudo systemctl status omni-remote-agent.service
   # Kỳ vọng: "Unit omni-remote-agent.service could not be found."
   orb -m <vm> ps aux | grep -c "remote_agent.agent\b"
   # Kỳ vọng: 0 (không còn process riêng chạy remote_agent.agent như entrypoint độc lập —
   #          chỉ còn import bên trong process aoip.agent.employee)
   orb -m <vm> sudo systemctl is-active aoip-agent
   # Kỳ vọng: active
   ```
2. **⚠️ KHÔNG dùng số lượng key Redis registry làm bằng chứng duy nhất** — đây chính xác là
   phép đo đã tạo false-negative khiến fix 2026-07-22 tưởng thành công (cả 2 process cùng gửi
   `agent_id` giống hệt nhau nên registry luôn chỉ có 1 key/host BẤT KỂ có double-fire hay
   không — nó chỉ chứng minh "record mới nhất tới", không chứng minh "chỉ có 1 process gửi").
   Bằng chứng THẬT phải là **tỉ lệ tần suất**, đo trên **cả 3 VM** (không chỉ cust-app):
   ```bash
   # Phía VM — đếm số dòng "emitted evidence" trong đúng 60 giây:
   orb -m <vm> sudo bash -c 'timeout 60 tail -f /var/log/omni-agent.log | grep -c "emitted evidence"'
   # Kỳ vọng: xấp xỉ 60/OMNI_AGENT_COLLECT_INTERVAL (hiện interval=20s → ~3 dòng/60s).
   #          Nếu double-fire vẫn còn, số này sẽ gấp ~2 lần kỳ vọng.
   ```
   ```bash
   # Phía Omni cùng khung giờ — đếm số lần pipeline "About to process" cho đúng agent_id đó:
   kubectl logs -n multi-agent deploy/omni-fullstack --since=90s 2>/dev/null | \
     grep "loyalty-uat_<vm>" | grep -c "About to process"
   ```
   Dán số liệu THẬT của cả 2 lệnh trên, cho cả 3 VM, vào báo cáo cuối — không chỉ ghi "đã kiểm
   tra, ổn".
   Registry key count (`kubectl exec -n multi-agent redis-0 -- redis-cli KEYS
   "omni:remote_agent:registry:loyalty-uat_*"` → kỳ vọng đúng 3 key) vẫn nên chạy như một
   sanity-check phụ (đúng agent_id, đúng tenant) nhưng KHÔNG được dùng làm bằng chứng chống
   double-fire.
3. `grep -n "tên khác .aoip-agent" CLAUDE.md` → không match.
4. `grep -rn "enable --now omni-remote-agent" scripts/ docs/architecture/ docs/product/` → không
   còn match nào dạng hướng dẫn thao tác hiệu lực (chỉ còn trong tài liệu lịch sử/audit).
5. Risk item hardening (`aoip-agent.service` chạy root, không `ProtectSystem`/`NoNewPrivileges`,
   PoC RCE đã từng xuyên thủng 2026-07-31) — xác nhận **CHƯA fix, ghi thành mục riêng** trong
   báo cáo cuối, không tự ý xử lý (đúng phạm vi đã chốt với user). Trỏ tới
   `docs/audit/SRE_READINESS_2026-08.md` mục B7 và `scripts/omni-agent.service` (template đã
   hardening nhưng chưa dùng) làm điểm khởi đầu cho task riêng sau này.

**Nếu bất kỳ mục 1-4 nào FAIL:** dừng, không coi migration là hoàn tất, quay lại phase tương ứng.

---

## Rủi ro tồn đọng sau khi hoàn tất plan (không nằm trong scope xử lý)

1. **Hardening security** (`aoip-agent.service` chạy root) — ghi nhận ở Phase 6, chưa fix.
2. **ADR-001 §5** (Gateway `agent_runtime.py` trùng logic với `aoip.agent.delivery`) — ghi nhận
   ở Phase 5c, chưa fix.
3. **`aoip.agent.daemon`** (canonical target dài hạn theo ADR-001 §1) vẫn chưa từng deploy thật
   — quyết định đã chốt là KHÔNG build trong phạm vi này; đây là hướng đi tương lai, cần ADR/kế
   hoạch riêng khi có nhu cầu thật (durable inbox/outbox, fencing, lease renewal cấp production).
4. **3 installer script khác nhau tồn tại trong repo** (`scripts/omni-agent-install.sh`,
   `scripts/install_agent.sh` cài vào `/opt/aoip-agent` — path KHÁC layout fleet thật
   `/opt/omni-remote-agent`) — phát hiện phụ trong lúc nghiên cứu plan này, không nằm trong scope
   (không phải nguyên nhân gây bug double-agent), nhưng là nguồn nhầm lẫn tiềm tàng khác nếu ai
   dùng nhầm installer cho VM mới sau này. Ghi lại để cân nhắc dọn ở 1 task riêng.
