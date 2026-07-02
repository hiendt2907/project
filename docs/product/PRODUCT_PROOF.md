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
5. **[MỚI, phát hiện khi viết test cho iteration 4]** `coerce_evidence_dict()` (`pkg/reasoning/schema.py`) serialize `extracted_fact` cắt cứng ở 2000 ký tự — nếu `discovery_data` đủ lớn (vd process_list rất dài), JSON bị cắt giữa chừng → `json.loads()` lỗi → TOÀN BỘ evidence bị coi là rỗng (mất cả legacy write lẫn AOIP projection, không chỉ mất agent_id). Chưa fire trong lab hiện tại (process list các VM nhỏ, dưới ngưỡng), nhưng là rủi ro thật cho VM có nhiều process/service hơn. Chưa sửa (ngoài phạm vi bottleneck iteration 4) — ưu tiên cho iteration sau nếu quan trọng.
