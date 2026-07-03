# PRODUCT CONTRACT — Omni Autonomous SRE (v1, frozen 2026-07-03)

> Tài liệu canonical chốt phạm vi sản phẩm cho giai đoạn productization. Mọi feature mới phải map
> được vào một bước Golden Journey bên dưới; nếu không map được, nó nằm ngoài scope v1.
> Thay đổi contract này cần một ADR mới, không sửa ngầm.

Liên quan: `docs/architecture/ADR-001-canonical-agent-runtime.md`,
`docs/architecture/ADR-002-command-protocol.md`, `docs/product/PRODUCT_PROOF.md`.

## 1. Target customer (v1)

- Doanh nghiệp có đội IT/SRE nhỏ (1–5 người vận hành).
- Có Linux (systemd) và/hoặc Kubernetes.
- 20–100 server hoặc workload.
- Cần: inventory + system understanding (System Twin), incident investigation có evidence,
  và bounded remediation có phê duyệt.
- Chấp nhận triển khai theo tier tăng dần: **Observe → Shadow → Advisory → HITL Execute →
  Scoped Autonomy**. Không tier nào tự động thăng cấp; mỗi lần chuyển tier là một quyết định
  người vận hành với gate riêng (xem §6).

## 2. Supported platforms (v1)

| Được hỗ trợ | Không thuộc v1 (non-goal) |
|---|---|
| Linux + systemd | Database automatic failover |
| Kubernetes (workload restart/rollback) | Network/firewall autonomous mutation |
| HTTP/API health probe | Schema migration tự động |
| PostgreSQL **read-only** discovery | Storage destructive action |
| Redis **read-only** discovery | Generic shell sinh bởi LLM |
| Prometheus/Grafana/log sources | Code riêng theo tenant |
| | Full multi-cloud |
| | Self-organizing agent personas không giới hạn |

## 3. Golden Journey (product gate chính thức)

```text
Create Tenant
→ Create Environment
→ Enroll Remote Agents
→ Discover
→ Build System Twin
→ Resolve Critical Unknowns
→ Understanding Ready
→ Shadow Incident
→ Produce Advisory (evidence-backed)
→ Human Approves Typed Remediation
→ Execute
→ Verify Resolution
→ Update System Twin
→ Audit and Export
```

Toàn bộ phải chạy qua **official API/portal**. Cấm trên product path: thao tác Redis thủ công,
sửa database thủ công, script nội bộ không chính thức, hardcode tenant, fake UI state.

Trạng thái hiện tại của từng bước được theo dõi trong `docs/product/PRODUCT_PROOF.md`
(capability matrix) — contract này định nghĩa đích, PROOF ghi bằng chứng.

## 4. First remediation catalog (đúng 3, không mở thêm trước khi cả 3 đạt gate)

1. `RestartSystemdService`
2. `RestartKubernetesWorkload`
3. `RollbackKubernetesDeployment`

Mỗi action bắt buộc có đầy đủ: typed schema · allowlist · tenant/resource scope · proof-of-fault ·
understanding gate · authority · CRAT (fail-closed) · human approval · precheck · before-state ·
idempotency · lease · fencing · execute timeout · frequency limit · post-verification ·
rollback/escalation · kill-switch enforcement · audit timeline.

Action N+1 chỉ được mở sau khi action N đạt exit gate (không mở song song).

Cấm tuyệt đối: `execute_shell(command_from_llm)` hoặc bất kỳ đường nào để LLM tự sinh shell
command chạy trên production host.

## 5. Initial service objectives (SLO v1)

Hard invariants (mục tiêu = 0, đo được, vi phạm = release blocker):

```text
Cross-tenant command delivery      = 0
Lost terminal outcome              = 0
Duplicate mutation                 = 0
Unaudited mutation                 = 0
Mutation without post-verification = 0
```

Metric vận hành phải đo (target chốt sau 30-day shadow, baseline trước):
Agent availability · command delivery latency · recovery-after-offline · System Twin freshness ·
diagnosis precision · recommendation acceptance · unsafe recommendation rate · restore success ·
release rollback success.

## 6. Autonomy tiers

| Tier | Quyền | Gate để vào tier |
|---|---|---|
| Observe | chỉ thu thập | enrollment hợp lệ |
| Shadow | chẩn đoán nội bộ, không hiển thị khuyến nghị cho tenant | discovery + Twin có dữ liệu |
| Advisory | hiển thị recommendation có evidence | understanding readiness đạt ngưỡng |
| HITL Execute | thực thi action typed sau human approval | action đạt exit gate Phase 5 (safety) |
| Scoped Autonomy | tự thực thi trong allowlist hẹp | 30-day shadow đạt quality threshold |

Mặc định mãi mãi: `OMNI_AUTO_EXECUTE_ENABLED=false` (fail-closed kill-switch); tier hiệu lực
resolve qua Redis cache > PG > env (invariant `resolve_tier`).

## 7. Data boundary & residency

- **PostgreSQL** = durable source of truth: tenant, environment, agent identity/ownership,
  credential metadata, mission, command/attempt/outcome (đích Phase 3 Slice 3 — hiện tại còn ở
  Redis, ghi nhận là debt có chủ đích), decision, approval, policy, competency, audit metadata.
- **Redis** = heartbeat, cache, lease, fencing, ready-queue, dispatch acceleration, ephemeral
  session, distributed lock. Redis không được là nguồn duy nhất của audit history.
- **Object storage** (chưa có, đích Phase 7): raw evidence, log bundle, diagnostic archive,
  report, handover artifact.
- **`INV_DATA_RESIDENCY`**: tài liệu khách hàng chỉ lưu metadata phía Omni (file_id + summary
  ≤2000 chars); nội dung thô ở phía khách hàng.

## 8. Pilot acceptance criteria

Pilot được coi là thành công khi, trên một tenant thật:
- onboarding lặp lại được không cần developer;
- Agent lifecycle ổn định (install/update/rollback đã diễn tập);
- System Twin đủ coverage cho các host được enroll;
- incident investigation tạo giá trị được operator xác nhận;
- ≥1 action HITL chạy an toàn end-to-end với audit đầy đủ;
- 0 cross-tenant violation, 0 lost terminal outcome, 0 duplicate mutation;
- backup/restore Control Plane đã diễn tập thành công.

## 9. Non-goals bổ sung (v1)

Không mở đồng thời với productization core: billing, multi-region, marketplace, full UI redesign,
agent persona mới. Portal không được hiển thị plan/quota nếu backend chưa enforce chúng.
