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

## Iteration 9 — Phase 6 slice: real Agent+VM for tenant-replay-01, cross-tenant isolation proof (2026-07-02)

**Bottleneck đã fix (Phase 6 của slice "Repeatable Tenant Onboarding Baseline"):**

Trước iteration này `tenant-replay-01` chỉ có 1 row `omni_admin.tenant` (iteration 7) — chưa có
Agent/VM thật nào gắn vào, nên golden journey Agent→Discovery→Fact→Twin→Competency chưa từng chạy
cho tenant này và chưa có bằng chứng cách ly (isolation) giữa 2 tenant trong cùng cluster.

Không tạo VM mới (chỉ có 3 VM lab: cust-edge/cust-app/cust-db, đều đã gắn `staging-sim`) — dùng lại
`cust-edge`, cài **agent thứ hai song song** (identity riêng, install dir riêng, systemd unit
riêng) bind vào `tenant-replay-01`:

1. Thêm API key `tenant-replay-01` vào `omni-gateway-secret` (`OMNI_TENANT_APIKEYS`), rolling
   restart `omni-gateway` — verify `kubectl rollout status` xanh.
2. Cài `/opt/omni-remote-agent-replay01` trên `cust-edge` (copy venv từ agent staging-sim có sẵn),
   `run.env` mới (`OMNI_AGENT_ID=tenant-replay-01_cust-edge`, `OMNI_AGENT_TENANT_ID=tenant-replay-01`),
   systemd unit `omni-remote-agent-replay01.service` riêng — `systemctl enable --now`.
3. Runtime proof (log agent, không chỉ code/deploy):
   `/var/log/omni-agent-replay01.log` → `POST .../webhook/agent/register "HTTP/1.1 200 OK"`,
   `POST .../webhook/agent/profile "HTTP/1.1 200 OK"`, `POST .../webhook/agent/evidence` →
   `emitted evidence enqueued=6`.
4. Twin persisted thật: `redis-cli HGETALL omni:aoip:system_model:tenant-replay-01` → 41 facts,
   `revision=6`, subject duy nhất `host:cust-edge`, mọi provenance `agent:tenant-replay-01_cust-edge`
   (không có `agent:unknown`, không có host của tenant khác).
5. **Cross-tenant isolation — VERIFIED_RUNTIME**: `redis-cli HGET omni:aoip:system_model:staging-sim
   facts` vẫn nguyên 78 facts / 3 host (`cust-edge`, `cust-app`, `cust-db`) — không bị agent mới ghi
   đè hay trộn lẫn. Ở tầng API: `GET /onboarding/unknowns?tenant_id=staging-sim` gọi bằng API key
   của `tenant-replay-01` → HTTP 200 nhưng **body trả về `tenant_id: tenant-replay-01`** (281
   unknowns của chính tenant gọi, không phải của staging-sim) — `resolve_scope()`
   (`src/gateway/tenant_context.py:29`) cố tình bỏ qua `override_tid` cho caller non-admin, chỉ dùng
   `ctx.tenant_id` từ key. Ban đầu nghi ngờ đây là lỗ hổng cách ly (chỉ check HTTP status code) —
   sau khi đọc body mới xác nhận KHÔNG phải bug, chỉ là hành vi cố ý (silent-scope-override, không
   phải 403) — **lưu ý UX**: caller không biết mình đang bị scope lại âm thầm, đáng cân nhắc trả
   403 hoặc warning thay vì âm thầm đổi scope ở iteration UX sau, nhưng không phải data-loss/security
   defect.
6. Operator-visible qua API thật: `GET /onboarding/unknowns?tenant_id=tenant-replay-01` (Bearer
   đúng key) → Unknown thật (`svc:systemd-networkd` facet `owner`, `svc:omni-remote-agent` facet
   `upstream`, …); `GET /onboarding/competency?tenant_id=tenant-replay-01&entity_type=host&entity_id=host:cust-edge`
   → `identity: VERIFIED`, evidence_refs trỏ `agent:tenant-replay-01_cust-edge` +
   `discovery:port_scan/process_list` thật.

**Chưa DONE** (để lại cho iteration sau, không mở rộng trong iteration này):
- Chỉ 1/1 host cho tenant-replay-01 (dùng chung VM `cust-edge` với staging-sim qua 2 agent song
  song) — chưa test multi-host thật cho tenant thứ hai.
- Chưa viết automated test (unit/integration) cho kịch bản "2 agent, 2 tenant, cùng 1 VM" — mới chỉ
  runtime proof thủ công qua `orb -m` + `redis-cli` + `curl` trong pod.
- Chưa cập nhật `scripts/provision_fresh_tenant.py` / `remote_agent_provisioning.py` để tự động hoá
  bước "thêm API key vào `omni-gateway-secret`" — hiện làm tay qua `kubectl` patch, chưa canonical.
- UX gap `resolve_scope()` silent override (mục 5 ở trên) chưa fix, chỉ ghi nhận.

Verify: `orb -m cust-edge sudo systemctl status omni-remote-agent-replay01.service` (active running);
`kubectl exec pod/redis-0 -- redis-cli HGET omni:aoip:system_model:tenant-replay-01 facts` (41
facts, đúng agent_id); `kubectl exec deploy/omni-gateway -- curl .../onboarding/unknowns?tenant_id=tenant-replay-01`
(200, Unknown thật).

## Iteration 10 — Canonicalize tenant API-key provisioning step (2026-07-02)

**Bottleneck đã fix**: iteration 9 mục "Chưa DONE" #3 — bước "thêm API key vào
`omni-gateway-secret`" trước đây làm tay qua `kubectl patch`, không có script canonical, không
idempotent, dễ lặp lỗi khi provision tenant tiếp theo.

**Fix**: `scripts/add_tenant_api_key.sh <tenant_id> [api_key]` — đọc giá trị hiện tại của
`OMNI_TENANT_APIKEYS` trong secret `omni-gateway-secret`, no-op nếu `tenant_id` đã tồn tại (in ra
key hiện có), sinh key mới bằng `openssl rand -hex 32` nếu không truyền, patch secret, rolling
restart + `rollout status` gateway.

**Runtime proof (VERIFIED_RUNTIME, chạy trực tiếp trên cluster lab thật, không chỉ code/test)**:
1. No-op path: chạy với `tenant-replay-01` (đã tồn tại từ iteration 9) → in đúng key hiện có, không
   patch, không restart.
2. Mutation path: chạy với tenant tạm `tenant-scripttest-01` → sinh key mới, patch secret thành
   công, `omni-gateway` rollout restart xanh (`successfully rolled out`).
3. Idempotency re-run: chạy lại lệnh giống hệt cho `tenant-scripttest-01` → no-op, trả đúng key vừa
   sinh ở bước 2 (không tạo entry trùng, không sinh key thứ hai).
4. Dọn dẹp: revert `OMNI_TENANT_APIKEYS` về đúng giá trị gốc (3 tenant: default/staging-sim/
   tenant-replay-01), rolling restart lại, verify secret content khớp nguyên trạng.
5. Gateway health sau toàn bộ chu trình: `kubectl get --raw
   /api/v1/namespaces/multi-agent/services/omni-gateway:80/proxy/healthz` → `{"status":"ok",
   "rate_limit_tps":1000}`; `printenv OMNI_TENANT_APIKEYS` trong pod mới xác nhận chứa
   `tenant-replay-01`.
6. Test suite liên quan: `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or
   tenant"` → 146 passed.

**Chưa DONE**: script chưa được gọi tự động từ `scripts/provision_fresh_tenant.py` (vẫn là 2 bước
tách biệt: tạo tenant Postgres + thêm API key) — canonical hoá thành 1 lệnh duy nhất là candidate
cho iteration sau. UX gap `resolve_scope()` silent override vẫn chưa fix (không phải bottleneck của
iteration này).

Verify: `bash scripts/add_tenant_api_key.sh tenant-replay-01` (no-op, in key hiện có);
`git show HEAD -- scripts/add_tenant_api_key.sh`.

## Iteration 11 — Wire API-key provisioning into single-command tenant provisioning (2026-07-02)

**Bottleneck đã fix**: iteration 10 mục "Chưa DONE" — `scripts/provision_fresh_tenant.py` (Postgres
tenant row) và `scripts/add_tenant_api_key.sh` (gateway secret) vẫn là 2 lệnh tách biệt, dễ quên bước
thứ hai khi provision tenant mới.

**Fix**: `provision_fresh_tenant.py` gọi `provision_api_key()` (subprocess `bash
scripts/add_tenant_api_key.sh <tenant_id>`) ngay sau khi Postgres row provision thành công. Thêm cờ
`--skip-api-key` cho trường hợp caller muốn tự quản lý gateway secret riêng.

**Runtime proof (VERIFIED_RUNTIME, chạy trực tiếp trên cluster lab thật)**:
1. Port-forward `omni-postgres:5432` → chạy `OMNI_ADMIN_PG_DSN=... PYTHONPATH=src python
   scripts/provision_fresh_tenant.py --tenant-id tenant-wiretest-01 --display-name "Wire Test 01"`
   → cả 2 bước chạy trong 1 lệnh: log `provisioned tenant=tenant-wiretest-01` rồi
   `[add_tenant_api_key] generated new key ... secret updated ... successfully rolled out`.
2. Idempotency re-run: chạy lại lệnh giống hệt → Postgres `create_tenant(idempotent=True)` no-op,
   `add_tenant_api_key.sh` in đúng key cũ, không patch/không restart lần 2.
3. Gateway health sau mutation: port-forward `svc/omni-gateway:80` → `curl .../healthz` →
   `{"status":"ok","rate_limit_tps":1000}`.
4. Dọn dẹp: revert `OMNI_TENANT_APIKEYS` về đúng 3-tenant gốc (default/staging-sim/
   tenant-replay-01) + rolling restart + verify secret content khớp nguyên trạng + healthz lại `ok`;
   `DELETE FROM omni_admin.tenant WHERE tenant_id='tenant-wiretest-01'` → xác nhận
   `SELECT tenant_id FROM omni_admin.tenant` chỉ còn 3 tenant gốc.
5. Test suite liên quan: `.venv/bin/python -m pytest tests/ -q -k "onboarding or gateway_api or
   tenant" --ignore=tests/integration` → 146 passed (không đổi so với iteration 10 — không thêm test
   mới, chỉ verify không regress).

**Chưa DONE**: chưa có automated test (pytest, không chỉ live-cluster manual run) cho
`provision_fresh_tenant.py` gọi `provision_api_key()` — vẫn là runtime proof thủ công, chưa CI-safe.
Multi-host cho `tenant-replay-01`, `resolve_scope()` UX gap, "2 agents/2 tenants" test coverage vẫn
mở từ iteration 9.

Verify: `git show HEAD -- scripts/provision_fresh_tenant.py`; `grep -n provision_api_key
scripts/provision_fresh_tenant.py`.

## Iteration 12 — pytest coverage for provision_api_key() (2026-07-02)

**Bottleneck đã fix**: iteration 11's "Chưa DONE" — `provision_api_key()` (subprocess wrapper
calling `add_tenant_api_key.sh`) only had live-cluster manual proof, no CI-safe automated test.

**Fix**: `tests/test_provision_fresh_tenant.py` (mới, 3 test) — mock `subprocess.run` to assert
`provision_api_key()` invokes `["bash", str(ADD_API_KEY_SCRIPT), tenant_id]` với `check=True` và
đúng `cwd` (project root); asserts `subprocess.CalledProcessError` propagates (no swallowed
failure); asserts `ADD_API_KEY_SCRIPT` resolves to a real file on disk. Scope: subprocess wiring
only — the real Postgres/gateway mutation path stays covered by the iteration 11 live-cluster
runtime proof, not re-tested here (would require live Postgres + gateway, out of scope for a unit
test).

**VERIFIED_TEST**: `.venv/bin/python -m pytest tests/test_provision_fresh_tenant.py -q` → 3 passed.
Regression check `.venv/bin/python -m pytest -k "onboarding or gateway_api or tenant or provision"
--ignore=tests/integration -q` → 155 passed (was 146 in iteration 11 baseline + these 3 new + other
already-existing tests newly matched by the broadened `provision` keyword).

**Chưa DONE**: multi-host cho `tenant-replay-01`, `resolve_scope()` UX gap, "2 agents/2 tenants" test
coverage vẫn mở từ iteration 9 — decide whether Phase 6/7 of the slice needs these before declaring
"Repeatable Tenant Onboarding Baseline" fully DONE.

Verify: `.venv/bin/python -m pytest tests/test_provision_fresh_tenant.py -q`.

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
