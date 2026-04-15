# Verification & gates

Khi đổi code **worker / gateway / Docker / requirements**, cần vòng xác minh đầy đủ — không chỉ pytest (theo `.cursor/rules/omni-cicd-k8s.mdc`).

## CI (GitHub Actions)

- `gitleaks` — secret scan, fail nghiêm.
- `pytest` contract (tập cố định trong `ci.yml` — autonomous, analyst, mapping, evidence proof, proactive, integration autonomy).
- Gates Python: `validate_env_mode_gate.py`, `validate_mutate_only_gate.py`, `validate_classifier_regression_gate.py`, `validate_phase_docs_gate.py`.
- Build image worker + gateway (không push).

## Local / Makefile

- `make autonomy-gate` — bộ gate đầy đủ hơn (theo `CLAUDE.md`).
- `make docker-worker` / `make docker-gateway` — build image.
- `make deploy-worker` / `make deploy-gateway` — lab (khi có cluster).

## E2E (chọn theo mục tiêu)

| Mục tiêu | Lệnh |
|----------|------|
| Proactive + audit | `make e2e-proactive` → `scripts/proactive_e2e.sh` |
| Gateway + alert + Loki + trace | `bash scripts/gateway_alert_loki_verify.sh` |
| Incident matrix | `make e2e-incident-matrix` → `scripts/e2e_incident_matrix.sh` |

## Wiki site

- `bash scripts/wiki_build.sh` — build MkDocs (mặc định **không** `--strict` vì tài liệu repo có link tới `src/` ngoài site).
- Strict tùy chọn: `WIKI_STRICT=1 bash scripts/wiki_build.sh`.

## Kết luận vận hành

- **Matrix pass ≠ audit strict đầy đủ** — có thể fail sigma/trace trong lab — xem `project-memory` (LabVsRealAlertTesting / FailurePatterns).
- `reports/incident-matrix/latest.json` — artifact matrix; đối chiếu `git_sha` / `config_sha256_primary_matrix` khi báo cáo.
