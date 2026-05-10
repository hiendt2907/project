# Kế hoạch xử lý technical debt (trừ fleet layer)

**Phạm vi OUT OF SCOPE:** đa cluster / đa nền tảng trong một Omni (fleet registry, multi-kubeconfig) — xem [../vendor/OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md) §9, [project-memory.md](project-memory.md) `TechnicalDebt`.

**Nguồn gộp:** [OMNI_PROJECT_CANONICAL.md](../vendor/OMNI_PROJECT_CANONICAL.md) §9, [master_plan_v3_review_report.md](../vendor/master_plan_v3_review_report.md) §13–§15, [adr-rbac-executor.md](../vendor/adr-rbac-executor.md), [project-memory.md](project-memory.md).

---

## Nguyên tắc

1. **Mỗi mục** có tiêu chí thoát (done) có kiểm chứng (test / `kubectl auth can-i` / e2e).
2. **Không** mở scope Rook/Ceph trong repo (đã khóa tại MPV3).
3. **Self-learning nâng cao** giữ zero-impact default; chỉ nới sau khi gate + bằng chứng tier (đã có invariant trong project-memory).

---

## Wave A — Bảo mật vận hành (ưu tiên cao)

| ID | Nợ | Hành động | Tiêu chí thoát |
|----|-----|-----------|-----------------|
| A1 | `omni-executor` / `omni-core` gắn SA rộng (cluster-admin lab) | Thực hiện ADR: inventory tool mutate từ `autonomous_execute.py` → Role/ClusterRole tối thiểu; tách SA executor nếu cần | `kubectl auth can-i` matrix pass cho từng verb cần thiết; `e2e_incident_matrix.sh` có scenario mutate pass trên SA mới |
| A2 | Gateway/analyst SA (đã tách một phần) | Rà soát lại manifest: prober read-only, gateway `automountServiceAccountToken`, analyst least privilege — đối chiếu bảng MPV3 §1 todo `rbac-remaining-sas` | Audit manifest + `auth can-i` snapshot trong PR |

**Phụ thuộc:** Cho phép mutate trong namespace đối tác = CSV `autonomous_allowed_namespaces` + RBAC khớp namespace đó.

---

## Wave B — Độ tin cậy hạ tầng phụ thuộc

| ID | Nợ | Hành động | Tiêu chí thoát |
|----|-----|-----------|-----------------|
| B1 | Ollama DNS/embed (thiếu Service → lỗi embed) | Chuẩn hoá một pattern triển khai: `ExternalName` / `host.docker.internal` / in-cluster Service — một mục trong runbook deploy đối tác; health check trong checklist | Knownbase + deploy checklist; analyst `curl` tags 200 (đã có snippet canonical) là gate thủ công hoặc script |
| B2 | Redis Sentinel (client có, cụm K8s không cố định trong repo) | **Không bắt buộc** ship manifest Sentinel trong repo — cung cấp **mẫu Helm/operator** hoặc link tới §16 MPV3 + biến `OMNI_REDIS_SENTINEL_*` | Tài liệu “cài Sentinel rồi set env” + test kết nối trong lab |
| B3 | Prometheus/Grafana “chưa mở rộng manifest monitor” (MPV3) | Tách product: **(i)** giữ JSON dashboard canonical + sync script; **(ii)** tùy chọn Helm kube-prometheus-stack cho đối tác | Dashboard SoT không drift; hoặc bản ghi nhận “monitor do đối tác cung cấp” + override URL trong settings |

---

## Wave C — Chất lượng reasoning / proactive (trung bình)

| ID | Nợ | Hành động | Tiêu chí thoát |
|----|-----|-----------|-----------------|
| C1 | Classifier misroute / planner hallucination tool (project-memory FailurePatterns) | Mở rộng `classifier-regression-gate` + test cố định cho các regex nhạy; planner contract tests cho JSON tool taxonomy | Gate CI xanh; không regression trên các case đã ghi |
| C2 | Strict audit / sigma gate nhạy lab (`sigma_gate_ok=false`) | Tách **profile lab vs prod**: ngưỡng PromQL / duration trong ConfigMap; tài liệu “lab acceptance ≠ strict audit” đã có — bổ sung env preset | Hai profile rõ ràng; proactive e2e pass trên lab profile |
| C3 | `omni-results` tên lịch sử | Grep repo — mọi doc/code comment còn sót → trỏ `omni-action-feedback` | Không còn reference gây hiểu nhầm (trừ changelog có mention deprecated) |

---

## Wave D — Tài liệu & nợ “đã đóng”

| ID | Nợ | Hành động | Tiêu chí thoát |
|----|-----|-----------|-----------------|
| D1 | Bảng MPV3 §15 mục 1 (feedback) — đã có code | Đánh dấu **closed** trong bảng hoặc chuyển sang “chỉ còn hygiene doc” | Một dòng trong MPV3 §15: trạng thái **đã đóng** |
| D2 | Strict audit nhạy lab (canonical §9) | Không phải bug — gộp vào C2; hoặc metric dashboard “audit pass rate” | Owner hiểu trade-off; không duplicate canonical |

---

## Thứ tự đề xuất

1. **A1 → A2** (RBAC) trước khi bán triển khai đối tác có mutate.  
2. **B1** song song hoặc ngay sau A (embed là đường RAG).  
3. **C1 → C2** khi ổn định topology.  
4. **B3** theo nhu cầu monitor của đối tác.  
5. **C3, D*** là hygiene cuối mỗi wave.

---

## Không nằm trong kế hoạch này

- Fleet / multi-cluster single Omni (đã tách).  
- Rook/Ceph.  
- Tự động nới self-learning tiers (chỉ sau governance + evidence).

---

*Cập nhật khi một wave đóng: sửa bảng trạng thái + một dòng trong [project-memory.md](project-memory.md) nếu invariant thay đổi.*
