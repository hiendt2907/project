# SPRINT — "Nhân viên SRE": Remote Agent Production Lifecycle

**Ngày lập:** 2026-07-07 · **Trạng thái:** PROPOSED (chờ user duyệt)
**Chủ đề:** Remote Agent là **nhân viên SRE của công ty Omni** — tiếp nhận hệ thống khách hàng,
tìm hiểu, quan sát, vận hành. Sprint này productize vòng đời nhân viên đó ở tầng backend.
**Bám:** `docs/product/PRODUCTION_MISSON.md` (ưu tiên #2 Productize Remote Agent, #4 safety/
durability) · ADR-001 (AOIP = canonical runtime) · ADR-002 (protocol vocabulary — ĐÃ XONG,
không lặp lại) · FRAMEWORK_LAWS (`INV_DATA_RESIDENCY` [C], `INV_FAIL_CLOSED`,
`INV_RECOVERABLE_ACTION`).

## Sprint Goal

Đóng trọn segment **"Enroll Remote Agents → Discover → Build System Twin"** của Golden Journey
ở mức production: agent có identity, enrollment lặp lại được, phát hiện drift, update/rollback
an toàn, command outcome không mất/không trùng, và residency được chứng minh tại nguồn.

**Thước đo cuối sprint (tất cả phải đạt):**
1. Payload rời VM khách hàng: **0 field raw content** (chứng minh bằng Kafka message thật).
2. VM chạy bundle cũ bị phát hiện tự động trong ≤1 chu kỳ heartbeat (không còn class bug
   "bundle-drift" từng cắn 2 lần).
3. Enroll 1 VM mới → agent chạy + Twin có fact, **không sửa tay** file nào trên VM/Redis/PG.
4. Update agent qua command channel, health-gate fail → tự rollback, chứng minh trên VM thật.
5. Kill agent giữa mutation + restart gateway → 0 mất outcome, 0 duplicate mutation.
6. Reboot VM / cắt mạng 10 phút → agent tự resume, không mất evidence.

## Baseline TRƯỚC sprint (đo thật 2026-07-07 — để so sánh sau sprint)

Đo trực tiếp trên 3 VM lab (`orb -m`) + cluster (`kubectl exec redis-0`) + repo, KHÔNG suy diễn
từ tài liệu. Mỗi thước đo sprint có trạng thái "trước" tương ứng:

| # | Thước đo sprint | Trạng thái TRƯỚC (đo thật) | Bằng chứng |
|---|---|---|---|
| 1 | 0 raw content rời VM | ❌ **Vi phạm sống trên cả 3 VM**: dòng `found.append({"path": ..., "content": content})` tồn tại tại `/opt/omni-remote-agent/remote_agent/collectors/discovery_evidence.py:209-210` trên cust-edge/app/db. At-rest phía Omni đã sạch (snapshot Redis chỉ có `services`, không field `content`) — vi phạm là **in-transit** | `orb -m <vm> grep`; `GET omni:knowledge:discovery_snapshot:staging-sim:*` parse JSON → `content` absent |
| 2 | Drift phát hiện ≤1 heartbeat | ❌ **Không có cơ chế so sánh.** Hiện tại VERSION `1.1.3` khớp repo trên cả 3 VM (tình cờ đồng bộ, không phải nhờ cơ chế); `/webhook/agent/versions` chỉ list, không có expected manifest, không alert | `cat /opt/omni-remote-agent/.../VERSION` ×3 = 1.1.3 = repo |
| 3 | Enroll VM mới không sửa tay | ❌ **Credential tĩnh trong `run.env`** (`OMNI_AGENT_API_KEY` + `OMNI_AGENT_ID` render sẵn lúc provision); không có enroll token, không revoke, không tenant-binding handshake | `grep ^[A-Z_]* run.env` trên cust-edge: không có biến `OMNI_ENROLL*` |
| 4 | Update hỏng tự rollback | ❌ `updater.py` có download+sha256+extract+restart nhưng **không health-gate, không N-1 bundle, không auto-rollback**. Runtime trên cả 3 VM = `remote_agent.agent` (unit `omni-remote-agent.service` active); `aoip-agent.service` inactive/absent cả 3 — AOIP daemon **chưa từng chạy thật** | `systemctl is-active` ×3; `systemctl cat` → `ExecStart=... -m remote_agent.agent` |
| 5 | 0 mất/0 trùng outcome (chaos) | ⚠️ **Redis-only, chưa từng chaos-proof**: command state chỉ ở `omni:cmd:rec:*`/`omni:cmd:ready:*` (Redis, hiện 0 key); PG `omni_admin` **không có bảng command/outcome nào** (migrations 0001-0004: tenant/tier/playbook/readiness/portal-identity) | `redis-cli --scan 'omni:cmd:*'` = 0; grep migrations |
| 6 | Agent tự resume sau reboot/mất mạng | ❓ **Chưa từng đo** — chưa có bài test reboot/network-partition nào được chạy có chủ đích. Not Run | — |

Trạng thái nền liên quan (snapshot cùng thời điểm):
- Twin: 2 tenant có System Model — `omni:aoip:system_model:staging-sim` (hash, HLEN=3) và
  `omni:aoip:system_model:tenant-replay-01` (HLEN=3). `tenant-replay-01` đã có agent profile cho
  cả `cust-edge` lẫn `cust-app` (`omni:agent:profile:*`) — item "multi-host cho tenant-replay-01"
  từ iteration 9 thực tế đã tiến triển hơn ghi chú cũ.
- Bundle 3 VM hiện **đồng bộ với repo** (có `collect_connection_scan`, VERSION khớp) — thời điểm
  tốt để bắt đầu sprint vì không phải dọn drift trước.
- HEAD lúc đo: `359d7c1` (main, đã push). Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false`.

**Cách so sánh sau sprint:** chạy lại đúng các lệnh ở cột "Bằng chứng" (IT-7 sprint review) và
điền cột "SAU" — mỗi ❌/⚠️/❓ phải chuyển thành ✅ có runtime proof, hoặc ghi trung thực
Failed/Not Run.

## Nguyên tắc xuyên suốt (không nhắc lại trong từng iteration)

- Mỗi iteration = 1 vertical slice, đóng bằng **runtime proof trên VM/cluster thật**
  (`orb -m <machine>`), không chỉ unit test. `test pass + push ≠ deployed` — luôn verify
  module trong pod/VM đang chạy.
- Feature agent mới viết trên **`src/aoip/agent/`** (ADR-001); `src/remote_agent/` chỉ nhận
  thay đổi compatibility tối thiểu (residency fix, version reporting).
- Sau mỗi iteration: pytest unit + cập nhật `PRODUCT_PROOF.md` + checkpoint
  `docs/handoffs/CURRENT_SESSION.md`. Commit theo iteration (như 26 iteration trước).
- Kill-switch `OMNI_AUTO_EXECUTE_ENABLED=false` giữ nguyên toàn sprint; mọi mutation test qua
  đường HITL/command-channel có fencing sẵn có.

---

## IT-1 — Data residency tại nguồn (nhỏ, làm trước)

**Vấn đề:** `src/remote_agent/collectors/discovery_evidence.py::collect_doc_snapshot`
(dòng ~209-210) đọc nguyên văn file (≤8000 byte × ≤20 file) và gửi raw `content` qua Kafka.
Omni hash sau khi nhận (`discovery_doc.py::_sanitize_documents`) — sai chỗ: raw đã rời VM.
Vi phạm `INV_DATA_RESIDENCY` [CONSTITUTIONAL] duy nhất còn sót.

**Việc:**
- Hash/sanitize NGAY TRÊN VM: payload chỉ còn `path`, `sha256`, `length`, `mtime` (+ summary
  metadata nếu cần, ≤2000 chars, không raw).
- Omni-side `_sanitize_documents` giữ tolerant dual-format (agent cũ còn gửi `content` trong
  transition window) — nhưng log warning khi gặp raw.
- Test: payload schema không chứa field `content`; test tolerant-path phía Omni.
- Redeploy bundle lên cả 3 VM (`cust-edge/app/db`) — nhớ gotcha: VM bundle từng cũ hơn repo.

**DoD:** consume thật topic `omni-knowledge-evidence`, xác nhận doc snapshot mới không có raw
content. Metric sprint #1 đạt.

## IT-2 — Drift detection: phát hiện nhân viên "chạy kiến thức cũ"

**Nền có sẵn:** agent đã có `VERSION` (1.1.3), endpoint `/webhook/agent/versions` (iteration 25)
đã list version theo tenant. Thiếu: **so sánh với expected**.

**Việc:**
- Release manifest (version + bundle sha256 expected) — nguồn: repo/PG, gateway đọc được.
- Agent gửi kèm bundle hash trong heartbeat/envelope (remote_agent: thay đổi compat tối thiểu).
- Gateway so sánh → trạng thái `current | drifted | unknown` per agent; expose qua
  `/webhook/agent/versions` + readiness card; drift → Telegram advisory (label VI chuẩn).
- Test: agent giả lập version cũ → API trả `drifted`.

**DoD:** hạ version 1 VM thật → hệ thống tự báo drift trong ≤1 chu kỳ heartbeat. Metric #2 đạt.

## IT-3 — Enrollment + identity: "tuyển dụng" chính thức (nền AOIP)

**Nền có sẵn:** `src/aoip/agent/identity.py`, canonical provisioning module
(`scripts/lib/remote_agent_provisioning.py`), `create_tenant(idempotent=True)` + runtime proof.

**Việc:**
- Enroll flow: one-time enroll token (provision qua Admin API) → agent đổi lấy credential
  per-agent (thay vì API key tenant dùng chung) → tenant binding ghi PG
  (`omni_admin`, nhớ gotcha FK: tenant phải tồn tại trước).
- Gateway endpoint enroll + revoke; credential rotation là non-goal (ghi risk register).
- Installer dùng canonical provisioning module, không f-string tay.
- Test: enroll 2 lần cùng token → lần 2 bị từ chối; agent revoked → 401.

**DoD:** VM mới (hoặc re-provision 1 VM lab) enroll → discovery chạy → Twin có fact, không
sửa tay. Metric #3 đạt.

## IT-4 — Pilot migration: `cust-app` chuyển sang AOIP daemon

**Theo ADR-001:** `aoip.agent.daemon` là canonical (durable inbox/outbox, lease, fencing,
idempotency, crash recovery) nhưng **chưa từng deploy thật**. Migrate 1 VM ít rủi ro nhất
(`cust-app` — chỉ có app :8080) làm pilot.

**Việc:**
- **Parity checklist trước khi chạm VM**: liệt kê collectors `remote_agent` đang có
  (system/services/logs/database/storage/k8s/discovery_evidence) vs năng lực aoip daemon;
  gap nào chưa port → port hoặc ghi nhận chấp nhận thiếu tạm.
- Systemd unit mới song song, cutover có rollback = switch unit cũ lại (giữ unit
  `omni-remote-agent.service` disabled chứ không xoá).
- Chạy song song 1 cửa sổ so sánh envelope (shadow) nếu khả thi, hoặc so Twin fact
  trước/sau 24h.

**DoD:** `cust-app` chạy AOIP daemon ≥24h, Twin không mất fact so với baseline, parity report
ghi vào PRODUCT_PROOF. Rollback path đã diễn tập thật 1 lần.

## IT-5 — Safe update/rollback qua command channel: "đào tạo lại" an toàn

**Nền có sẵn:** `remote_agent/updater.py` (download+sha256+extract+restart — nhưng không có
health-gate/rollback), command channel fencing/heartbeat (`agent_runtime.py`, ADR-002).

**Việc (trên AOIP daemon, theo ADR-001):**
- Update = durable command (idempotency ledger sẵn có): download → verify sha256 vs release
  manifest (IT-2) → swap → health-check window → fail thì tự rollback về bundle trước.
- Giữ N-1 bundle trên VM để rollback offline.
- Outcome (updated/rolled_back + version) báo về qua channel, ghi CRAT event.

**DoD:** trên pilot VM: (a) update thành công lên version mới; (b) cố ý ship bundle hỏng →
health-gate fail → tự rollback, Omni nhận outcome `rolled_back`. Metric #4 đạt. Sau đó
migrate nốt `cust-edge`, `cust-db` sang AOIP daemon bằng chính cơ chế update này.

## IT-6 — Command outcome durability: "giao việc có biên bản"

**Vấn đề:** command state hiện sống ở Redis (gateway routes). Mission yêu cầu "command
delivery không mất outcome, không duplicate mutation" ở mức production → cần source of truth
bền (PG) — đây là "Phase 3 Slice 3" đã được ADR-002 trỏ tới.

**Việc:**
- Persist command + outcome vào PG (`omni_admin` migration mới), Redis giữ vai trò hot-path;
  reconcile loop Redis↔PG. Vocabulary/transitions dùng `aoip.protocol` — cấm định nghĩa lại.
- Chaos proof: kill agent giữa command đang RUNNING; restart gateway; agent resume bằng lease
  + fencing → command hoàn tất đúng 1 lần (idempotency ledger), outcome về PG.

**DoD:** chaos scenario trên chạy thật trên VM lab, `SELECT` PG cho thấy đúng 1 outcome,
CRAT chain hợp lệ. Metric #5 đạt.

## IT-7 — Soak + offline recovery: "đánh giá thử việc" + đóng sprint

**Việc:**
- Reboot từng VM; cắt mạng 10 phút (orb network hoặc iptables tạm) → agent tự resume,
  evidence buffer không mất (outbox), không duplicate sau khi mạng về.
- Chạy lại `scripts/e2e_onboarding_full_flow.py` (10 TC) trên runtime mới cả 3 VM.
- Cập nhật capability matrix trong `PRODUCT_PROOF.md`; sprint review: metric 1-6 Passed/
  Failed/Not Run trung thực; cập nhật ADR-001 (trạng thái migration), risk register.

**DoD:** metric #6 đạt; 10/10 TC pass trên AOIP runtime; PRODUCT_PROOF phản ánh đúng.

## IT-8 (STRETCH — chỉ làm nếu IT-1..7 xong sớm) — Mission contract skeleton

Bước đầu của "nhân viên nhận việc" đúng nghĩa: Omni giao **Mission object** (mission_id, goal,
DoD, authority bounds) cho agent qua command channel thay vì config ngầm; agent báo cáo theo
mission_id. Chỉ skeleton (1 mission type: `onboarding_discovery`), tái dùng `aoip.mission`,
0 noun mới (INV_NO_NEW_NOUNS). Không mở rộng sang scheduler/planner trong sprint này.

---

## Ngoài phạm vi sprint (chốt để không trôi)

- Portal/UI mới (chỉ đụng readiness card ở mức hiển thị drift — backend-first).
- Rewrite `workers/`; multi-partition Kafka; HA/backup/restore (mặt trận sau).
- Credential rotation tự động; multi-tenant billing; discipline mới ngoài SRE.
- Xoá root `ui/` (chờ quyết định riêng của user).

## Rủi ro chính & giảm nhẹ

| Rủi ro | Giảm nhẹ |
|---|---|
| Parity gap collectors khi migrate sang AOIP daemon (rủi ro lớn nhất) | IT-4 bắt buộc parity checklist TRƯỚC khi chạm VM; pilot 1 VM; rollback unit cũ diễn tập thật |
| VM access chỉ qua `orb -m` (không SSH IP) | Đã ghi trong CLAUDE.md; mọi script provisioning dùng orb |
| Bundle drift NGAY TRONG sprint (sửa code agent nhiều lần) | IT-2 làm sớm chính vì vậy — drift detection bảo vệ các iteration sau |
| PG FK violation khi enroll tenant mới | Provision tenant qua `create_tenant()` trước (gotcha đã có post-mortem) |
| Update tự động hỏng cả 3 VM cùng lúc | IT-5 rollout tuần tự: pilot → từng VM; giữ N-1 bundle; health-gate |

## Trình tự & phụ thuộc

```
IT-1 (residency) ──┐
IT-2 (drift)  ─────┼─→ IT-3 (enroll) → IT-4 (pilot AOIP) → IT-5 (update/rollback)
                   │                                          ↓
                   └──────────────────────────→ IT-6 (durability) → IT-7 (soak, đóng sprint)
                                                                       ↓
                                                              IT-8 (stretch: mission)
```

IT-1 và IT-2 độc lập, có thể đảo thứ tự. IT-5 phụ thuộc IT-2 (manifest) + IT-4 (daemon trên
pilot). IT-6 độc lập với IT-4/5 về mặt code nhưng chaos proof cần daemon → xếp sau.

## Lệnh verification chuẩn (mỗi iteration chọn phần liên quan)

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/integration
.venv/bin/python scripts/e2e_onboarding_full_flow.py --skip-reinstall   # IT-3/4/7
NS=multi-agent make e2e-portal                                          # chỉ khi đụng readiness card
orb -m cust-app systemctl status omni-remote-agent aoip-agent           # runtime thật
```
