# Repo Cleanup Plan — go-prod hygiene (2026-06-03)

> Mục tiêu: xoá file/folder **rác / cũ / lệch dự án**. KHÔNG đụng config lab-only hay refactor architectural (để sprint riêng). Plan này chờ DUYỆT trước khi thực thi.
> Nguyên tắc: untracked ≠ rác. Nhiều file untracked là **work thật chưa commit** — sẽ `git add`, KHÔNG xoá.

---

## NHÓM 1 — XOÁ (rác/scratch/artifact rõ ràng)

| Mục | Đường dẫn | Lý do |
|---|---|---|
| Binary build artifact | `omni-remote-agent/agent` (13MB) | Binary Go compiled — không bao giờ nên trong repo. Source ở `src/remote_agent/` + `omni-remote-agent/cmd/` |
| smart-siem junk | `smart-siem/scripts/patch-dockerfiles-orbstack.sh.bak`, `smart-siem/customer/k3s/overlays/lab-workers/kustomization.yaml.bak` | File `.bak` |
| Dockerfile .orig | `smart-siem/omni/siem/{brain-go,math-gateway,agent,bff}/Dockerfile.orig` | Backup thừa từ sed patch |
| Scratch plan | `refactor_plan.md` (root) | Ghi chú nháp; đã thực hiện xong phần lớn (docs overhaul + remote agent) |

> `.orig` trong `smart-siem/customer/wasm-proxy/filter/vendor/*` (Rust vendored deps) — **GIỮ**, thuộc vendored crates, không phải rác của ta.

## NHÓM 2 — ARCHIVE rồi XOÁ khỏi root (báo cáo cũ, đã có bản mới)

| Mục | Lý do | Hành động đề xuất |
|---|---|---|
| `docs/omni_v3_executive_report.md` (Apr 8) | Báo cáo v3 cũ, trước nhiều sprint | Xoá (lịch sử trong git) |
| `docs/SPRINT_DELIVERY_REPORT.md` (May 11) | Sprint cũ một lần | Xoá |
| `docs/PROJECT_COMPLETE_REPORT.md` (May 21) | "complete report" đã bị audit 2026-06-03 thay thế | Xoá (giữ `docs/audit/omni-audit-2026-06-03.md` làm bản hiện hành) |
| `reports/phase{1,2,3,4}-*.md` | Báo cáo phase 1-4 cũ (Master Plan V3 split — kiến trúc đã bỏ) | Xoá |
| `reports/repo_scan_latest.txt` | Snapshot scan cũ | Xoá |
| `reports/e2e-telegram-flow.md` | Báo cáo flow cũ | Xem lại; xoá nếu trùng runbook |

> KHÔNG xoá: `docs/post-mortems/*` (chứng cứ), `docs/ADVISORY_MODE_REDTEAM_FINDINGS.md` (security evidence), `reports/incident-matrix/`, `docs/audit/`.

## NHÓM 3 — GITIGNORE (transient, không track)

| Mục | Lý do |
|---|---|
| `.claude/worktrees/` | Agent worktree tạm — không thuộc repo |
| `.reports/` | Output tool tạm (codemap-diff) |
| `omni-remote-agent/agent` | Binary (đã ở nhóm 1; thêm pattern chặn tái diễn) |

## NHÓM 4 — KEEP + GIT ADD (work thật chưa track — KHÔNG xoá)

> Đây KHÔNG phải cleanup mà là rủi ro mất việc. Cần `git add` riêng:
- `k8s/deployments/omni-fullstack.yaml` + `omni-fullstack-rbac.yaml` — **deployment ĐANG CHẠY**
- `docs/CODEMAPS/`, `docs/audit/`, `docs/lanes/`, `docs/CONTRIBUTING.md`
- `scripts/chaos/`, `scripts/kpi_key_migrate.py`, `scripts/gen_sys_hard_fail_rag.py`, `scripts/wait_omni_consumer_ready.py`, v.v.
- `k8s/network-policies/`, `k8s/services/`, `k8s/jobs/omni-backend-verify.yaml`
- `data/rag_training/`

## NHÓM 5 — CẦN XÁC NHẬN (không rõ còn dùng)

| Mục | Câu hỏi |
|---|---|
| ~~`omni-remote-agent/cmd/`~~ | **RESOLVED 2026-06-05: ĐÃ XOÁ.** Go skeleton v0.1.0 (dead, không build/CI/deploy). Bản chính = `src/remote_agent/` Python v1.1.3. |
| `k8s/deployments/omni-worker-configmap-production-like.yaml.example` | Còn dùng làm template prod không? |
| `k8s/deployments/omni-agent.yaml` | Deployment agent — còn active? |
| `docker-compose.agent.yml`, `requirements-agent.txt` | Remote-agent local dev — còn dùng? |
| `scripts/omni-agent-*.sh`, `omni-provisioner.py`, `omni-ssh-tunnel.sh` | Bộ provisioning remote-agent — còn dùng? |

---

## Thứ tự thực thi (sau duyệt)
1. Nhóm 3 (gitignore) — chặn rác tái diễn trước
2. Nhóm 1 (xoá artifact/scratch) — an toàn nhất
3. Nhóm 2 (xoá báo cáo cũ) — sau khi xác nhận không có index ngoài
4. Nhóm 4 (git add work thật) — tách commit riêng
5. Nhóm 5 — chỉ làm sau khi bạn trả lời

**Verify sau mỗi nhóm:** `git status`, không xoá nhầm; `pytest` nếu đụng file liên quan code.
