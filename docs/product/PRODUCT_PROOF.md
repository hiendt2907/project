# Product Proof

Tài liệu này KHÔNG phải bằng chứng tự thân — mỗi dòng phải trỏ tới lệnh/API/log/datastore query
thật đã chạy. Cập nhật sau mỗi iteration của Continuous Productization Loop.

## Environment

- Commit: `67423b9` (tại thời điểm build) → image rebuild sau đó không đổi source, chỉ đổi digest
- Build: `multi-agent-system:latest`, digest `sha256:c2d433daac77e0ec4c4c474bc011b2000bd22fbf962117a18610126fbb44e9f6`
- Namespace: `multi-agent` (OrbStack k8s, single node `orbstack`)
- Tenant: `staging-sim` (provisioned qua `AdminConfigRepo.create_tenant()`, xem drift-correction post-mortem)
- VMs: `cust-edge` (192.168.139.87), `cust-app` (192.168.139.237), `cust-db` (192.168.139.225) — OrbStack, Ubuntu 24.04.4 arm64
- Agents: `staging-sim_cust-edge`, `staging-sim_cust-app`, `staging-sim_cust-db` — systemd unit `omni-remote-agent.service` trên cả 3 VM
- Last verified: 2026-07-02, iteration 4 của Continuous Productization Loop

## Capability Matrix

| Capability | Code | Deployed | Runtime verified | Operator-visible | Evidence |
|---|---:|---:|---:|---:|---|
| Tenant creation | ✅ | ✅ | ✅ | ⚠️ (API only, không UI) | `omni_admin.tenant` row `staging-sim` — `psql -c "SELECT * FROM omni_admin.tenant"` |
| Agent enrollment | ✅ | ✅ | ✅ | ❌ | `/var/log/omni-agent.log` trên cả 3 VM: `POST .../webhook/agent/register "HTTP/1.1 200 OK"` |
| Agent heartbeat | ✅ | ✅ | ✅ | ❌ | Log trên `cust-app`: `register` lặp lại mỗi ~2 phút (ttl=120), `GET .../commands/...` mỗi ~5s |
| Continuous discovery (cust-edge, cust-db, cust-app) | ✅ | ✅ | ✅ **3/3 host** | ❌ | `omni:evrl:p:staging-sim_{cust-edge,cust-db,cust-app}:{process_list,port_scan,service_topology}:PASSED` tồn tại cho cả 3 host trong Redis |
| Discovery evidence transport (Kafka) | ✅ | ✅ | ✅ | ❌ | `kafka-consumer-groups.sh --describe --group omni-onboarding-discovery` → lag=0, offset tăng liên tục |
| Observation → Fact projection | ✅ (code từ `1bc6292`) | ✅ (sau redeploy iteration 1) | ✅ | ❌ | log `onboarding_pipeline: system_model contradiction tenant=staging-sim` + Redis `omni:aoip:system_model:staging-sim` |
| System Twin persisted | ✅ | ✅ | ✅ **3/3 host** | ❌ (chỉ đọc được qua `redis-cli`) | `HGETALL omni:aoip:system_model:staging-sim` → revision=54, 76 facts (cust-edge 38, cust-db 19, cust-app 19); `host:cust-app` → `exposes_port 8080` khớp `ss -lntp` trên VM |
| Competency Matrix | ✅ | ✅ | ⚠️ chưa test riêng trong iteration này | ❌ | chưa kiểm trong iteration 1 |
| Unknown/Question lifecycle (O2B) | ✅ | ✅ | ⚠️ chưa test riêng | ❌ | chưa kiểm trong iteration 1 |
| Onboarding readiness | ✅ | ✅ | ✅ | ⚠️ (đọc DB trực tiếp) | `omni_admin.tenant_readiness_state` có row `staging-sim`, `readiness_flag=false` |
| Competency Matrix API (`GET /onboarding/competency`) | ✅ | ✅ (sau fix iteration 3) | ✅ | ✅ | `curl -H "Authorization: Bearer $KEY" ".../onboarding/competency?tenant_id=staging-sim&entity_type=host&entity_id=host:cust-app"` → `identity: VERIFIED`, evidence_refs trỏ `discovery:port_scan/process_list` thật |
| Unknowns API (`GET /onboarding/unknowns`) | ✅ | ✅ (sau fix iteration 3) | ✅ | ✅ | `curl .../onboarding/unknowns?tenant_id=staging-sim` → trả Unknown thật (vd `svc:fsidd` facet `business_capability`) |
| Mission/Command/Execution (closed-loop mutation) | ✅ code tồn tại | ✅ | ❌ chưa test | ❌ | Ngoài phạm vi golden journey hiện tại — `OMNI_AUTO_EXECUTE_ENABLED=false` cố ý |
| Fact provenance có agent_id thật | ✅ (fix iteration 4) | ✅ | ✅ | ✅ (qua `/onboarding/competency`) | Root cause: `_project_into_system_model` đọc `ev_doc.get("agent_id")` sai vị trí (thật ra nằm trong `extracted_fact`). Fix 2 lớp: `onboarding_pipeline.py` đọc đúng vị trí + `schema.py` promote `agent_id`/`hostname` lên top-level trước khi truncate. Verify: `redis-cli HGET omni:aoip:system_model:staging-sim facts` → 0/76 fact còn `agent:unknown` |

## Golden Journey

### Tenant onboarding (staging-sim, 3 VM lab)

**Status: PARTIAL** (nâng từ 2/3 → 3/3 host sau iteration 2; operator visibility vẫn là gap chính còn lại)

1. ✅ Tenant `staging-sim` tồn tại trong `omni_admin.tenant` (tạo qua `AdminConfigRepo.create_tenant()`).
2. ✅ 3 Agent registered — log `agent/register 200 OK` trên cả 3 VM, `omni:remote_agent:registry:staging-sim_{cust-edge,cust-app,cust-db}` tồn tại trong Redis.
3. ⚠️ Agent online: chưa kiểm tra "stale/offline" threshold trong iteration này (out of scope).
4. ✅ Inventory hiển thị ĐỦ 3/3 host (`cust-edge`, `cust-db`, `cust-app`) qua Twin.
5. ✅ Services/ports phát hiện đúng cho cả 3 host: `host:cust-db` → `exposes_port 3306` (mariadbd), `exposes_port 6379` (redis-server); `host:cust-edge` → `runs_process nginx` (port 80); `host:cust-app` → `exposes_port 8080` — cả 3 khớp `ss -lntp` chạy trực tiếp trên VM qua `orb -m`.
6. ✅ Twin revision hiển thị và tăng theo thời gian thực (6 → 18 → 54 qua 2 iteration), provenance có `discovery:{probe}:{trace_id}` + `agent:unknown` (⚠️ gap nhỏ — `to_observation()` không điền đúng `agent_id`, chỉ ghi placeholder).
7. ❌ Unknowns/contradictions: có contradiction thật (`runs_service` bị ghi đè giữa các probe khác nhau trên `cust-edge`) nhưng CHƯA verify hiển thị qua API/operator surface nào.
8. ✅ Operator nay CÓ cách xem — `GET /onboarding/competency?tenant_id=...&entity_type=host&entity_id=host:cust-app` (cần `entity_id` đúng format `{type}:{id}` khớp subject trong Twin, ví dụ nhỏ nhưng dễ nhầm) trả về facet/state/evidence/confidence thật; `GET /onboarding/unknowns` trả Unknown thật. **API tồn tại và hoạt động — CHƯA có UI**, ghi rõ PARTIAL cho phần UI.

## Known Broken Links

1. **Chưa có UI đọc Twin/Competency** — chỉ có API (đã fix iteration 3), operator vẫn cần biết endpoint + cách gọi thủ công (không phải dashboard).
2. Kafka `PartitionCount=1` toàn hệ thống — chưa sửa (P1 riêng, xem drift-correction post-mortem).
3. **Chỉ `cust-app` bị thiếu discovery flag lúc provision** (`OMNI_REMOTE_DISCOVERY_ENABLED` không có trong `run.env`, trong khi cust-edge/cust-db có) — đã fix trực tiếp trên VM (`echo >> run.env` + `systemctl restart`), nhưng đây là fix runtime, CHƯA có cơ chế provisioning tự động đảm bảo VM mới không rơi vào tình trạng tương tự (gap ở `scripts/e2e_orbstack_fleet.py`/agent bundle provisioning).
4. `entity_id` param của `/onboarding/competency` yêu cầu format nội bộ `{entity_type}:{entity_id}` (vd `host:cust-app`) thay vì chỉ `cust-app` — API dễ gây nhầm lẫn cho operator, đáng cân nhắc UX fix ở iteration sau.
5. ~~`coerce_evidence_dict()` cắt cứng `extracted_fact` ở 2000 ký tự~~ — **FIXED iteration 5**, xem bên dưới.
6. ~~Tenant provisioning KHÔNG idempotent~~ — **FIXED iteration 6**: `create_tenant(..., idempotent=True)` opt-in param, xem bên dưới. API HTTP `POST /autonomy/tenants` (gateway) vẫn giữ nguyên semantics 409 cũ (không set `idempotent=True`) — không phá contract hiện có, chỉ mở đường cho caller nội bộ (provisioning tooling) dùng repeat-safe path.
7. ~~Chưa có fresh-tenant runtime proof~~ — **PARTIAL iteration 7**: Phase 4 (repeat-provisioning
   proof trên Postgres thật) DONE, xem bên dưới. Phase 6-7 (golden journey Tenant→Twin→Competency
   không sửa tay cho tenant mới, cross-tenant isolation proof, operator read-only flow) CHƯA chạy —
   `tenant-replay-01` mới có row `omni_admin.tenant`, CHƯA có Agent/VM thật gắn vào tenant này.

## Iteration 7 — Fresh-tenant repeat-provisioning runtime proof (2026-07-02)

**Bottleneck đã fix (Phase 4 của slice "Repeatable Tenant Onboarding Baseline"):**

`scripts/provision_fresh_tenant.py` (MỚI) — canonical caller gọi thẳng
`AdminConfigRepo.create_tenant(idempotent=True)` qua `asyncpg` pool thật, tách biệt khỏi HTTP
`POST /autonomy/tenants` (gateway route cố ý giữ nguyên contract 409, không set `idempotent=True` —
xem mục 6 ở trên). Chạy THẬT 2 lần liên tiếp trên Postgres thật trong cluster (port-forward
`svc/omni-postgres`, tenant `tenant-replay-01`):
- Lần 1: tạo tenant, 1 row `omni_admin.tenant`, 1 row `omni_admin.config_change_log`
  (`actor=provisioning-tooling, action=create`).
- Lần 2 (idempotent=True): trả về result giống hệt, **không** raise, **không** row thêm — verify
  bằng `SELECT` trực tiếp: đúng 1 row tenant, đúng 1 audit event (`GROUP BY` count=1).

`VERIFIED_RUNTIME` cho riêng phần "create_tenant(idempotent=True) an toàn khi re-run trên Postgres
thật" — đây là bằng chứng runtime đầu tiên cho iteration 6 (trước đó chỉ `VERIFIED_TEST` với
FakePgPool). Chưa DONE toàn bộ Phase 4-7 của slice: `tenant-replay-01` mới tồn tại ở tầng
`omni_admin.tenant`, CHƯA có Agent provisioning + discovery + Twin + Competency thật cho tenant này
(đó là Phase 6, cần VM lab riêng — ngoài phạm vi iteration này).

Verify: `.venv/bin/python -m pytest tests/test_admin_config_store.py
tests/test_remote_agent_provisioning.py -q` → 29 passed. Runtime: `psql` query trực tiếp trên
`omni-postgres-0` qua port-forward, output đính kèm ledger.

## Iteration 5 — Safe evidence compaction + canonical provisioning module (2026-07-02)

**Bottleneck đã fix (Phase 1-3 của slice "Repeatable Tenant Onboarding Baseline"):**

1. `coerce_evidence_dict()` (`src/pkg/reasoning/schema.py`) — thay slicing thô `json.dumps(ef)[:2000]`
   (có thể cắt giữa JSON token → `json.loads()` lỗi → mất toàn bộ evidence, kể cả AOIP projection) bằng
   `_compact_extracted_fact()`: parse trước, shrink field lồng (string/list) theo budget giảm dần
   (500→200→80→20 ký tự/field), fallback drop nested collection nếu vẫn vượt — luôn trả JSON hợp lệ.
   Thêm field top-level `schema_version`, `truncated`, `original_size`, `content_hash` (SHA-256 của
   bản gốc chưa cắt) để downstream/audit biết evidence có bị compact hay không. `agent_id`/`hostname`
   vẫn được promote lên top-level TRƯỚC compaction (giữ nguyên logic iteration 4) nên luôn sống sót
   dù `extracted_fact` bị cắt. 9 test mới `tests/test_evidence_compaction.py` — dưới ngưỡng, đúng
   ngưỡng, vượt xa ngưỡng, unicode, nested sâu, `None` field, identity luôn còn, JSON list top-level.
2. **Canonical provisioning module** `scripts/lib/remote_agent_provisioning.py` (mới) — trích xuất
   f-string `run.env` viết tay trong `scripts/e2e_onboarding_full_flow.py` (TC-OB02, nguồn gốc gap
   cust-app thiếu `OMNI_REMOTE_DISCOVERY_ENABLED`) thành `AgentProvisioningSpec` + `render_run_env()`
   thuần Python, `discovery_enabled: bool = True` mặc định (không còn cách nào quên set flag này khi
   dùng module). Thêm `is_idempotent_rewrite()` (so sánh nội dung render, không rewrite/restart nếu
   giống hệt) và `effective_config_summary()` (non-secret, dùng để log lúc agent startup — chưa wire
   vào `remote_agent/agent.py` thật, chỉ có hàm sẵn sàng). `e2e_onboarding_full_flow.py` đã chuyển
   sang gọi module này thay vì f-string tay. 7 test mới `tests/test_remote_agent_provisioning.py`.

**Chưa làm (Phase 4-7, cần VM/cluster thật + thời gian dài hơn):** tạo tenant lab mới
(`tenant-replay-01`), provision qua canonical, chứng minh golden journey Tenant→Twin→Competency chạy
không sửa tay, chứng minh tenant isolation với `staging-sim`, chứng minh repeat-provisioning không phá
state, operator read-only proof flow. `AdminConfigRepo.create_tenant()` không idempotent (mục 6 ở
trên) sẽ chặn Phase 5 nếu không sửa trước.

Verify: `.venv/bin/python -m pytest tests/ -q --ignore=tests/integration` — xem log chạy trong
`docs/handoffs/CURRENT_SESSION.md`.

## Iteration 6 — omni-autonomous-productizer skill + tenant idempotency (2026-07-02)

Bootstrap skill `.claude/skills/omni-autonomous-productizer/` (Continuous Productization Loop
operator — Reality Map, evidence taxonomy, safety policy, quota/resume protocol, supervisor
fallback). Smoke-tested read-only (`reality_check.sh` chạy thật, xác nhận 3 VM lab Running +
`OMNI_AUTO_EXECUTE_ENABLED=false` trên `omni-fullstack`). Commit `5c76425`.

Iteration đầu tiên do skill này chọn (bottleneck #2 trong `references/current-priority.md`):
`AdminConfigRepo.create_tenant()` (`src/services/admin_config/repo.py:569-596`) thêm tham số opt-in
`idempotent: bool = False` — mặc định vẫn raise `ValueError` (giữ nguyên HTTP 409 contract của
`POST /autonomy/tenants`), nhưng khi `idempotent=True` (dành cho provisioning tooling nội bộ, ví dụ
fresh-tenant replay ở Phase 4-5 của slice "Repeatable Tenant Onboarding Baseline"), tenant đã tồn
tại được trả về nguyên trạng, không tạo dòng trùng, không tạo audit event trùng. Test mới
`test_create_tenant_idempotent_true_is_repeatable`
(`tests/test_admin_config_store.py`) verify: gọi 2 lần idempotent=True → cùng kết quả, đúng 1 row,
đúng 1 audit event. `VERIFIED_TEST` — CHƯA runtime-verify trên Postgres thật (chỉ FakePgPool), CHƯA
wire vào bất kỳ caller thật nào (đang chờ Phase 4 dùng).
