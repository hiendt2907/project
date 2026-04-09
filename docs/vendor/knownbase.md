# Known issues (symptom → fix)

**Split topology / deploy / logs:** [OMNI_PROJECT_CANONICAL.md](OMNI_PROJECT_CANONICAL.md) (canonical); [golden_path_split.md](golden_path_split.md) chỉ là redirect.

Short entries only. **Newest first** within each section. If the same symptom already exists, update **Fix** instead of adding a duplicate.

## Logic / application

**Symptom:** Worker pods (prober/analyst/core/executor) **CrashLoopBackOff** với `ImportError: cannot import name '_parse_tool_json' from 'workers.analyst_agentic_loop'`.  
**Fix:** Giữ alias `_parse_tool_json = _parse_agentic_json` trong `analyst_agentic_loop.py` vì `autonomous_feedback_loop.py` vẫn import tên cũ. Verify: `PYTHONPATH=src python -c "from workers.autonomous_feedback_loop import kafka_action_feedback_loop"`; `make docker-worker && make deploy-worker`.

**Symptom:** `kubectl exec -n multi-agent deploy/omni-prober -- …` (hoặc `omni-core`) trả `Internal error occurred: unable to upgrade connection: container not found ("omni-prober")` dù pod `Ready` và chỉ một container — E2E `gateway_alert_loki_verify.sh` / `e2e_incident_matrix.sh` không chạy được bước POST qua exec.  
**Fix:** **infra_blocker** — kiểm tra CRI/Kubernetes provider (OrbStack/Docker Desktop), kube-apiserver ↔ kubelet exec; thử `kubectl exec` từ máy khác hoặc nâng cấp runtime. Workaround tạm: chạy POST alert từ pod có shell trong cluster theo cách khác (Job `curl`, port-forward tới gateway) — không sửa bằng đổi tên container nếu manifest đã đúng. Verify khi exec ổn: `kubectl exec … -- true` = 0 rồi `SCENARIOS=nginx_waiting_fault bash scripts/e2e_incident_matrix.sh`.

**Symptom:** Sigma (`ERR_REA_SIGMA_GATE_BLOCKED`) chặn mutate dù workload HTTP thật sự 5xx liên tục; hoặc Loki không đọc được khi đã đủ điều kiện API/Web + `autonomous_allowed_namespaces`.  
**Fix:** Bật lab `OMNI_SIGMA_LOG_BYPASS_ENABLED=true` + `OMNI_LOKI_BASE_URL`; matrix row `workload_profile: api_web` (vd. `silent_5xx_bypass_sigma`); `workers/log_surge_probe.py` chứng minh sustained 500/503/504. Nếu Loki lỗi: `ERR_REA_LOG_SOURCE_UNAVAILABLE` + Telegram `"Sigma blocked & Log source unavailable"` + tombstone — không mutate. Verify: `pytest tests/test_incident_matrix_profile.py tests/test_log_surge_probe.py`; grep log `event=log_surge_sigma_bypass_ok` khi lab inject 5xx.

**Symptom:** Cần mở rộng incident training matrix theo registry và thêm self-learning shadow nhưng vẫn giữ runtime hiện tại không drift; verify strict thường fail ở `sigma_gate_ok`/`trace_stage_matrix_ok` dù matrix run pass.  
**Fix:** Thêm `config/incident_training_matrix.yaml` + payload generator `scripts/incident_matrix_payload_from_config.py`, mở rộng `scripts/e2e_incident_matrix.sh` chạy theo registry (31 scenario hiện hữu); thêm shadow module `src/workers/selflearning_shadow.py` và cờ `OMNI_*` mặc định off trong `src/workers/settings.py`; thêm gates `scripts/validate_nonimpact_guards_gate.py` + `scripts/validate_learning_loop_gate.py` và nối vào `Makefile` (`autonomy-gate`). Verify: `STRICT_ASSERT=0 SLEEP_SEC=5 bash scripts/e2e_incident_matrix.sh` => pass, strict audit vẫn ghi blocker `insufficient_sigma_evidence`/`trace_not_found_in_required_stages`.

**Symptom:** Ops team thiếu góc nhìn tài nguyên theo lớp hạ tầng: chưa có dashboard tách riêng cho usage pod (ngoại trừ `kube-system`) và usage node K8s nên khó soi saturation theo scope vận hành.  
**Fix:** Bổ sung `k8s/monitor/dashboards/omni_pod_resources.json` + `omni_node_resources.json`; mở rộng sync contract trong `scripts/sync_grafana_dashboard_configmaps.py`; regenerate `k8s/monitor/grafana-dashboards.yaml` để provision thêm 2 dashboard mới. Verify: apply ConfigMap + rollout `deployment/grafana -n monitor`.

**Symptom:** Dashboard Pod/Node bản rút gọn thiếu panel vận hành sâu và không soi được queue pipeline (Redis/Kafka, DLQ, lag, stuck-consumer).  
**Fix:** Rebuild full panel sets cho `omni_pod_resources.json` + `omni_node_resources.json`; thêm cụm panel Redis/Kafka health gồm `omni_worker_lag_size`, heuristic stuck consumer (`max_over_time + changes`), Redis stream backlog (`omni_redis_stream_backlog`), DLQ/Kafka Loki log panels. Đồng thời bật scrape kubelet `/metrics/resource` qua job `kubernetes-nodes-resource` trong `k8s/monitor/prometheus.yaml` để pod CPU/memory usage có dữ liệu `namespace/pod`. Verify: query panel chính trả series >0 và Grafana rollout OK.

**Symptom:** Dashboard provisioning bị rác do tồn tại đồng thời bộ cũ (L0/L1/L2/L3) và nhu cầu mới (Ops/Security/Learning), gây drift giữa dashboard source và runtime Grafana sidecar.  
**Fix:** Xóa toàn bộ JSON dashboard cũ trong `k8s/monitor/dashboards`, dựng lại 3 dashboard chuẩn `omni_ops.json`, `omni_security.json`, `omni_learning.json`, và thay `k8s/monitor/grafana-dashboards.yaml` chỉ còn 3 key tương ứng. Bổ sung script sync mới `scripts/sync_grafana_dashboard_configmaps.py` để render ConfigMap từ đúng 3 JSON canonical. Verify: `./scripts/with_working_kube.sh apply -f k8s/monitor/grafana-dashboards.yaml` + rollout `deployment/grafana -n monitor`.

**Symptom:** Security gate chưa “sạch bóng” khi bật scan strict: DSN credential cũ còn nằm trong history commit; đồng thời strict runtime audit có thể fail (`sigma_gate_ok=false`, `trace_stage_matrix_ok=false`) dù unit/contract pass và deploy thành công.  
**Fix:** Thay toàn bộ DSN hardcoded hiện tại bằng placeholder `${OMNI_DB_PASSWORD}` + fail-fast validator tại `src/rag/pgvector_store.py`; chuẩn hóa placeholder secret manifest Grafana; thêm `.gitleaks.toml` + `.pre-commit-config.yaml`; CI/Make thêm `secret-gate` (working tree, critical fail) và `secret-history-audit` tách riêng để governance xử lý history (rotate key + duyệt rewrite lịch sử). Verify: `make secret-gate` pass, `make e2e-incident-matrix` pass, `make autonomy-gate` fail do `sigma_gate_ok/trace_stage_matrix_ok` (đã ghi blocker).

**Symptom:** `EXECUTE_MUTATE` vẫn có thể nhận read-only tools (`k8s_describe_resource`, `inspect_*`, `list_*`) và classifier dễ map nhầm `ProbeFailureLab` do regex rộng; planner đôi lúc đề xuất tool read-only/hallucinated, gây hành vi mutate không chuẩn kiến trúc.  
**Fix:** Refactor theo mutate-only contract: tách `K8S_SDK_MUTATING_TOOL_NAMES` vs `READONLY_TOOL_ALLOWLIST` trong `workers/autonomous_execute.py`, reject có `reason_code` chuẩn; `analyst_agentic_loop` map reject sang reason codes SIEM và route read-only plan về suggest channel; `diagnostic_mapping` đổi label-first + `priority`, matrix pin row `ProbeFailureLab`; `evidence_consumer` thêm `Proof of Fault + 3-sigma + observation window` trước emit mutate; thêm gates `validate_mutate_only_gate.py`, `validate_classifier_regression_gate.py`, `validate_phase_docs_gate.py` + pytest contracts `test_diagnostic_mapping.py`, `test_evidence_proof_gate.py`.

**Symptom:** Governance flags (`lab_unchained`, `cluster_full_access`, `god_mode`) dễ drift theo từng module, thiếu công tắc môi trường thống nhất nên khó bảo đảm mặc định prod fail-closed hoặc bật high-action có kiểm soát cho dev.  
**Fix:** Thêm contract `OMNI_ENV_MODE=prod|dev` (default `prod`) trong `workers/settings.py` + ConfigMap; `prod` tự hạ bypass flags, `dev` mở high-action theo role. Enforce trong executor/policy path (`workers/autonomous_execute.py`, `workers/kafka_actions_consumer.py`, `execution/policy.py`, `execution/manager.py`, `execution/promotion.py`, `workers/proactive_react_runner.py`, `workers/autonomous_decider.py`). Thêm gate `scripts/validate_env_mode_gate.py`, Make target `env-mode-gate`, CI chạy gate + contract tests.

**Symptom:** Planner output hợp lệ JSON nhưng thiếu chuẩn args/schema theo tool đôi khi bị drop im lặng, khó truy hồi nguyên nhân chất lượng plan.  
**Fix:** `workers/analyst_agentic_loop.py` thêm reject taxonomy (`invalid_json`, `tool_not_allowlisted`, `rollout_missing_namespace_or_deployment`, ...), emit log `event=agentic_mutate_plan_reject` theo model/step/reason; test `tests/test_analyst_agentic_loop.py::test_reject_reason_taxonomy`.

**Symptom:** Executor ném `ValidationError: DescribeResourceArgs.resource_type Field required` khi planner emit `k8s_describe_resource` với args kiểu `{kind:"pod", pod:"...", namespace:"..."}` (không có `resource_type`).  
**Fix:** `workers/autonomous_execute.py` thêm chuẩn hoá args trước khi invoke registry: với `k8s_describe_resource`, map `kind/type -> resource_type` (`pod/deployment/service`) và fallback `name` từ `pod|deployment|service`. Thêm regression test `test_normalize_describe_resource_args_from_kind` trong `tests/test_autonomous_contract.py`. Verify: `pytest tests/test_autonomous_contract.py -q`; rà log `--since=15m` không còn `DescribeResourceArgs`/`resource_type missing`.

**Symptom:** Runtime model selection dễ drift vì chỉ set `OMNI_CHAT_MODEL`; các role khác (`reasoning_engine`, `heavy_lifter`, `helper`, `diag planner`, `autonomous_decider`) dùng mặc định ngầm, khó kiểm soát khi promote môi trường.  
**Fix:** Pin model profile trực tiếp trong `k8s/deployments/omni-worker-configmap.yaml`: `OMNI_CHAT_MODEL=qwen2.5:7b`, `OMNI_MODEL_REASONING_ENGINE=deepseek-r1:8b`, `OMNI_MODEL_HEAVY_LIFTER=gemma3:27b`, `OMNI_MODEL_HELPER=qwen2.5:1.5b`, `OMNI_DIAG_EVIDENCE_LLM_MODEL=deepseek-r1:8b`, `OMNI_AUTONOMOUS_DECIDER_MODEL=deepseek-r1:8b`, `OMNI_EMBED_MODEL=nomic-embed-text:latest`. Verify: `ollama list`, `kubectl exec deploy/omni-analyst -- env | grep OMNI_.*MODEL`, `SCENARIOS=nginx_waiting_fault bash scripts/e2e_incident_matrix.sh`.

**Symptom:** `analyst_agentic_loop` báo `404 Not Found` khi gọi Ollama (log hay hiện `.../api/generate`), dù `ollama-service` vẫn sống. Gốc là planner chọn model mặc định sai (`ollama_model` không tồn tại trong `settings`) nên rơi về `llama3` (model không có) → Ollama trả 404 `model not found`.  
**Fix:** `workers/analyst_agentic_loop.py` đổi chọn model theo chain hợp lệ: `diag_evidence_llm_model` → `model_reasoning_engine` → `model_helper` → `chat_model` (dedup), và thử lần lượt khi một model lỗi. Thêm test `tests/test_analyst_agentic_loop.py` để khóa regression model fallback. Verify: từ pod `omni-analyst`, `/api/chat` và `/api/generate` đều 200 với model có sẵn; planner không còn rơi mặc định `llama3`.

**Symptom:** Với một số model endpoint lab, planner loop có thể fail liên tiếp `404 /api/generate` nên không emit mutate plan dù evidence đã xác nhận fault workload (`CreateContainerConfigError`), khiến đường tự sửa bị ngắt.  
**Fix:** `workers/evidence_consumer.py` giữ planner-first nhưng thêm fallback an toàn khi planner thất bại: nếu `rollout_args_from_evidence_batch` hợp lệ và incident thuộc `workload_fault_incident_rollout_eligible|workload_cpu_incident_rollout_eligible` thì emit `EXECUTE_MUTATE(k8s_rollout_restart)`. Đồng thời `autonomous_feedback_loop` write-back học cho diagnostic path vào `action_experience` sau `VERIFIED_SUCCESS` (feedback mang `mutate_args`). Verify: `SCENARIOS=nginx_waiting_fault bash scripts/e2e_incident_matrix.sh` có `event=agentic_mutate_fallback ...` + `event=action_emitted action=EXECUTE_MUTATE` + `event=pre_apply_revalidate_ok ... deployment=nginx-test` + `VERIFIED_SUCCESS`.

**Symptom:** Flow diagnostic có xu hướng hardcode restart gate sau RAG hit (heuristic CPU/fault), dễ lệch kiến trúc planner-first (SDK -> RAG -> LLM plan loop).  
**Fix:** `workers/evidence_consumer.py` chuyển sang planner-first emit mutate: luôn gọi `run_agentic_mutate_plan` (max `autonomous_agentic_max_steps`) với sanitized evidence; bỏ shortcut heuristic auto-rollout trong runtime path. `workers/analyst_agentic_loop.py` mở planner output theo `MUTATE_TOOL_ALLOWLIST` (không khóa cứng chỉ rollout). Verify: scenario `redis_probe_fault` có `action=SUGGEST_REMEDIATION` + `action=EXECUTE_MUTATE` + `action_feedback_published status=ok`.

**Symptom:** Trace có `event=omni_actions_audit_only action=SUGGEST_REMEDIATION (no execute)` dù đã chẩn đoán xong; path RAG_HIT cho fault workload (probe failure / CreateContainer*) chỉ emit suggest nên không tự fix.  
**Fix:** Mở rộng gate mutate trong `workers/evidence_mutate_emit.py` + `workers/evidence_consumer.py`: fault incident (`createcontainer/crashloop/imagepull/probe`) có namespace+deployment sẽ emit `EXECUTE_MUTATE(k8s_rollout_restart)` (không chỉ CPU incident). Bật runtime `OMNI_AUTO_EXECUTE_ENABLED=true` trong `k8s/deployments/omni-worker-configmap.yaml`; rollout worker. Verify: log có `event=action_emitted action=EXECUTE_MUTATE` + executor `event=omni_actions_in action=EXECUTE_MUTATE` + `event=action_feedback_published ... status=ok`.

**Symptom:** `k8s_resource_quota_probe` trong path `pod_state` báo `403 Forbidden` cho SA `omni-prober` khi list `resourcequotas`, làm evidence batch có `FAILED` không cần thiết dù lỗi chính là `CreateContainerConfigError`.  
**Fix:** Cấp đúng quyền tối thiểu trong `k8s/deployments/prober-rbac.yaml`: thêm read-only `resourcequotas` (`get,list`) cho Role `omni-prober-read`; giữ nguyên deny mutate (`delete pods` vẫn `no`). Bổ sung `scripts/e2e_incident_matrix.sh` xuất report JSON (`REPORT_JSON`, mặc định `reports/incident-matrix/latest.json`) theo từng scenario + trace. Verify: `kubectl auth can-i list resourcequotas --as=system:serviceaccount:multi-agent:omni-prober -n multi-agent` = yes; `... delete pods ...` = no.

**Symptom:** `make e2e-proactive`/`full_system_audit --strict` dễ false-negative trong lab vì check stage chưa tách trace gateway/proactive và không gate theo 3-sigma (`dr`/`z_*`) hay preflight recording rules.  
**Fix:** `scripts/full_system_audit.py` thêm preflight Prom series (`omni:node_cpu:z`, `omni:mem:z`), sigma gate (>=N mẫu `dr=1` hoặc `|z|>=threshold`), tách trace matrix theo `gateway_trace_ids` và `proactive_trace_ids`; có fallback pass khi gateway không in trace nhưng trace đã chứng minh qua >=3 worker pods. Verify: `python3 -m py_compile scripts/full_system_audit.py` + `scripts/full_system_audit.py --strict ...`.

**Symptom:** Autonomy trace correlation chỉ dựa payload; nhánh lỗi ở feedback/proactive có thể thiếu terminal tombstone chuẩn, gây khó chứng minh đóng vòng và dễ tạo ghost-loop state key.  
**Fix:** Thêm `workers/autonomy_contract.py` (ordered transition + terminal tombstone), nối vào `omni_worker` stream ingest, `evidence_consumer`, `kafka_actions_consumer`, `autonomous_feedback_loop`, `proactive_observer`; Kafka dual propagation `trace_id` (header + payload) tại `messaging/kafka_bus.py`; thêm executor rate-limit theo action fingerprint (`executor_action_rate_limit_*`). Verify: `pytest tests/integration/test_autonomy_loop_transitions.py tests/integration/test_feedback_replan_loop.py tests/integration/test_fault_injection_matrix.py`.

**Symptom:** Ollama `**/api/embed` 400** khi query RAG quá dài — CPU spike / lỗi embed.  
**Fix:** `pkg/rag/embed_utils.truncate_for_embedding` + `rag_embed_max_tokens` (gate); `rag/pgvector_store._ollama_embed_query_robust` tier 512→64 token + optional `embed_model_fallback`; `llm/ollama_client` log `event=ollama_embed_400`. Verify: `pytest tests/test_embed_utils.py`.

**Symptom:** `diag_batch_flush` chỉ **2** probe (`pod_status` + `pod_metrics`) trong khi dispatcher còn **log_tail** + **Prom** — RAG chạy thiếu evidence.  
**Fix:** `workers/evidence_batch.py`: workload / `pod_container_state` chỉ flush khi đủ **5** probe (khớp `resource_probe_ids`) hoặc `AGG_TIMEOUT_SEC`. Verify: log `diag_batch_flush` có 5 probe; `pytest tests/`.

**Symptom:** Alert **CreateContainerError** / **reason** trên labels mà dispatcher chạy **redis_ping** + **k8s_list_pods** — không chẩn đoán pod từ SDK.  
**Fix:** `is_kube_pod_container_state_alert` + `kube_pod_state_probe_ids()` trong `workers/diagnostic_resource.py`; `diagnostic_dispatcher` nhánh `pod_container_state` (SDK + Prom pod) trước matrix. `evidence_batch`: gom batch như workload cho `symptom_group=pod_container_state`. Matrix `crash_loop_backoff` đổi probe sang **k8s_clinical_*** + **prom_pod_***. Verify: `pytest tests/test_diagnostic_resource.py`.

**Symptom:** Cần biết **alert đúng hay sai** mà **state machine SDK** (API) là chuẩn — phải **so sánh** tuyên bố alert với PodMetrics/PodStatus; không đánh đồng với “hard logic” tùy ý.  
**Fix:** `workers/alert_sdk_truth_compare.py` — `compare_alert_claim_to_sdk_state` (workload_resource + guard `labels.reason` + phase/waiting); `evidence_consumer` emit `SUGGEST_REMEDIATION` với `source=STATE_MACHINE_CONTRAST` khi **mâu thuẫn rõ** (vd CPU alert vs ~0 trong PodMetrics). Còn lại → RAG/LLM. Verify: `pytest tests/test_alert_sdk_truth_compare.py tests/test_diagnostic_resource.py`.

**Symptom:** Cần alert khi **nginx-test** memory **4Mi** (lab) — Pod **CreateContainerError** (OrbStack tối thiểu 6MB) / **OOM**; không có rule Prometheus cũ.  
**Fix:** `k8s/monitor/prometheus.yaml` nhóm `omni_lab_nginx`: `**NginxTestContainerWaitingFaultLab`** (`kube_pod_container_status_waiting_reason` … CreateContainerError|CrashLoop|…), `**NginxTestContainerLastTerminatedOOMLab**` (`kube_pod_container_status_last_terminated_reason` OOMKilled). `kubectl apply -f k8s/monitor/prometheus.yaml` + rollout **StatefulSet/prometheus**. Payload E2E: `scripts/alert_payloads/alertmanager_nginx_waiting_fault.json`; `gateway_alert_loki_verify.sh` ưu tiên pod lỗi (`E2E_NGINX_POD` override). Verify: `query` `ALERTS{alertname="NginxTestContainerWaitingFaultLab",alertstate="firing"}`.

**Symptom:** Lab — cần **EXECUTE_MUTATE** chỉ thao tác **Kubernetes SDK** (đúng API), không mở echo/shell/Prom.  
**Fix:** `workers/autonomous_execute.py`: `K8S_SDK_EXECUTE_TOOL_NAMES` = toàn bộ tool `kubernetes_asyncio` + `kubectl_cluster`; alias `k8s_patch_deployment` → `k8s_patch_resource`; `k8s_rollout_restart` nhánh `execute_rollout_restart_from_pending`. Verify: `pytest tests/test_autonomous_contract.py`.

**Symptom:** Pod `**nginx-load-*` OOMKilled** / tải không lên — script lab spawn **hàng nghìn** tiến trình `curl` (mặc định cũ `LOAD_CONCURRENCY=10000`).  
**Fix:** Mặc định `**LOAD_CONCURRENCY=256**` trong `scripts/nginx_test_cpu_alert_lab.sh`; cảnh báo khi >2000. CPU thật + alert overlap: `STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15` hoặc `make lab-nginx-cpu-overlap`. Target: `http://nginx-test.<ns>.svc.cluster.local/` (in-cluster), không port-forward laptop.

**Symptom:** Truth Law RAG miss ngắt luồng; trộn lệnh thực thi với văn bản Telegram; phê duyệt Redis cho escalate.  
**Fix:** `rag_truth_law_enforced` + RAG miss → `reason_diagnostic_rag_miss_sdk_only` (zero-knowledge, chỉ SDK); output hai kênh `MACHINE_JSON` / `HUMAN_SUMMARY` (`pkg/reasoning/two_channel_sdk.py`); mọi ESCALATE / vượt `autonomous_verify_max_rounds` → `emit_telegram_escalation` (`workers/telegram_escalation.py`); `tool_approval.request_approval` chỉ escalate Telegram, không Redis. Sơ đồ: `docs/vendor/autonomous_state_machine.md`. Verify: `pytest tests/test_two_channel_sdk.py tests/test_v3_tools.py`.

**Symptom:** Log `rag_gate search failed` / `… [Errno -2] Name or service not known` — RagGate = **Ollama embed** rồi **Postgres pgvector**; trước đây lỗi gộp chung khó biết DNS/host nào. (Cùng triển khai **Ollama trên Mac** qua cluster DNS.)  
**Fix:** `src/rag/pgvector_store.py`: `event=rag_ollama_embed_failed` vs `event=rag_pgvector_query_failed`; `src/pkg/rag/gate.py`: `rag_gate search failed … phase=ollama_embed|pgvector_query|unknown` + `detail.phase`. **Postgres:** `POSTGRES_RAG_DSN` từ secret `omni-postgres-app` key `uri` — lab `omni-postgres-rw:5432/ragdb` (xem thêm mục CrashLoop `pgpool-gateway` nếu worker không lên). **Ollama hướng B (lab):** Service `ExternalName` `ollama-service` → `host.docker.internal` — `make deploy-ollama`, file `k8s/deployments/ollama-service.yaml`; Mac: `OLLAMA_HOST=0.0.0.0:11434`. Verify: `kubectl exec deploy/omni-analyst -n multi-agent -- curl -sS -o /dev/null -w "%{http_code}" http://ollama-service:11434/api/tags` → **200**. **Hướng A:** Deployment Ollama in-cluster + ClusterIP `ollama-service:11434` khi không phụ thuộc host.

**Symptom:** Cần tách **audit** (`SUGGEST_REMEDIATION`) vs **thực thi** (`EXECUTE_MUTATE`), feedback executor → analyst, lab bỏ chờ Telegram.  
**Fix:** `pkg/autonomous_actions.py` + `workers/autonomous_execute.py` + `workers/autonomous_feedback_loop.py`. Topic `omni-action-feedback` (`scripts/kafka_ensure_omni_topics.sh`). `OMNI_AUTO_EXECUTE_ENABLED` (mặc định false). Analyst: `kafka_action_feedback_loop` trong `omni_worker` (role analyst). Verify: `pytest tests/test_autonomous_contract.py`.

**Symptom:** `full_system_audit` mặc định inject Kafka → trace `full-audit-*` + Telegram `[PROACTIVE][ESCALATED]` dày; proactive ReAct gọi `k8s_list_pods` không `namespace` hoặc `k8s_rollout_restart` với tên giống Pod/ReplicaSet.  
**Fix:** `scripts/full_system_audit.py`: `--inject-proactive` (mặc định off); `scripts/proactive_e2e.sh`: `E2E_INJECT_PROACTIVE=1` để bật. `proactive_react_runner` + `proactive_rollout_restart_allowed` / `proactive_react_require_namespace_for_list`. Phản hồi ngắn: `omni_concise_reply_max_words` + `effective_reply_max_words`. Verify: `pytest tests/test_proactive_guardrails.py tests/test_llm_context_budget.py`.

**Symptom:** Lab nginx-test stress **busy loop** trong container `nginx` → pod không ổn / CPU cAdvisor không phản ánh đúng (kìm master/worker).  
**Fix:** **kubectl port-forward** từ laptop là nút thắt (thường chỉ ~30–40% so với CPU limit pod). `**rakyll/hey:latest**` trên Docker Hub hay **ImagePullBackOff** (repo/deny). Lab: **Service** `nginx-test` + Pod `**curlimages/curl**` — vòng `curl` song song tới `http://nginx-test.<ns>.svc.cluster.local/` (`STRESS_MODE=curl`, `LOAD_CONCURRENCY` **128–512**, tránh >2000). Legacy: `STRESS_MODE=portforward`.

**Symptom:** `k8s_clinical_pod_metrics` log **404** `podmetrics.metrics.k8s.io "ns/pod" not found` — probe bị coi FAILED.  
**Fix:** 404 là **APIServer/Metrics API** khi **chưa có** CR `PodMetrics`. Nếu metrics-server đã chạy mà **mọi** namespace trống: xem **Infrastructure** — kubelet `/stats/summary` có thể trả `pods: []` (OrbStack/k3s). Không phải bug Python. `diagnostic_k8s_clinical.probe_k8s_clinical_pod_metrics`: **404 → INCONCLUSIVE** + `omit_reason=podmetrics_not_found_404`. Chẩn đoán: `bash scripts/diagnose_kubelet_pod_metrics.sh`. Verify: `pytest tests/test_diagnostic_k8s_clinical.py`.

**Symptom:** Log prober chỉ thấy **2** probe `prom_pod_*`, không có `k8s_clinical_*` / `diagnostic_dispatcher_plan kind=resource` — hoặc alert thiếu **label `pod**` (chỉ mô tả trong annotations), hoặc image/`resource_probe_ids` cũ chỉ còn Prom.  
**Fix:** `pod_identity_from_event`: fallback tên pod từ text `… in pod <name>` / `pod/<name>` (annotations + `error_hint`). `resource_probe_ids` luôn SDK trước rồi Prom. Verify: `pytest tests/test_diagnostic_resource.py`; `bash scripts/nginx_test_cpu_alert_lab.sh` (port-forward + curl host + POST gateway) — expect `probes=['k8s_clinical_…', 'prom_…']` trong log prober. Lab CPU thật: `scripts/nginx-test-deployment.yaml` limit **50m**; rule **NginxTestHighCpuLab** `> 0.04` **for 60s** trong `k8s/monitor/prometheus.yaml` (group `omni_lab_nginx`); `kubectl apply` ConfigMap monitor + rollout Prometheus; firing → gateway cần **Alertmanager** (repo không ship) — script vẫn POST synthetic để chứng pipeline.

**Symptom:** Lab nginx-test CPU ~90% nhưng Prometheus không firing / expr không match.  
**Fix:** Kiểm tra label cAdvisor (`namespace` vs `kubernetes_namespace` tùy cluster); đồng bộ limit pod với ngưỡng rule (0.04 cores ≈ ~80% của **50m**). `STRESS_SEC` ≥ 75s; `curl` instant query trong script để xem rate.

**Symptom:** Proactive ReAct / Telegram báo **ESCALATED** / `k8s_rollout_restart` lung tung khi payload Kafka chỉ có `canonical_query` stub, **không** namespace **không** `trigger_promql` — GIGO (garbage in → garbage out). MPV3 có **coerce GIGO-safe** cho **evidence** (`pkg/reasoning/schema.py`) nhưng **không** tự áp cho incident proactive.  
**Fix:** `proactive_gigo_cluster_identity_ok`: bỏ qua sớm với audit `**SKIPPED_GIGO**` nếu thiếu **cả** `namespace` **và** `trigger_promql` (bật `OMNI_PROACTIVE_GIGO_REQUIRE_CLUSTER_IDENTITY`, mặc định true). `evaluate_proactive_triggers` vẫn pass vì luôn set `trigger_promql`. `full_system_audit` inject thêm `namespace` + `trigger_promql`. Verify: `pytest tests/test_proactive_guardrails.py`.

**Symptom:** Trace `gw-prom-*` có trong `request_trace` nhưng **không** xuyên suốt: access log Uvicorn không có trace; log **asyncio** `Unclosed client session` (aiohttp) không gắn trace — khó debug.  
**Fix:** Gateway: `**trace_id` sinh ngay đầu** `POST /webhook/prometheus`, `request.state.trace_id`, `push_gateway_trace_id`/`pop` bọc handler, `**install_gateway_trace_logging()**` (filter inject `[trace_id=…]` cho root/uvicorn/asyncio/aiohttp; bỏ qua nếu chuỗi đã chứa id), header `**X-Omni-Trace-Id**`, middleware `http_done`. Worker stream/proactive: `**push_trace_id`/`pop**` trong `_process_stream_entry` + `_process_proactive_message`; JSON log thêm field `**trace_id**` từ `current_trace_id()`. Probe: `**probe_k8s_list_pods_namespace**` `await v1.api_client.close()` trong `finally`. Verify: `bash scripts/gateway_alert_loki_verify.sh` (grep cùng trace gateway + split workers); `make e2e-proactive` (gateway POST + proactive inject). Build: `make docker-gateway` + `make docker-worker` trước rollout.

### Verify scripts vs MPV3 split topology (tránh tham chiếu Pod sai / nhiễu RAG)

**Chuẩn lab hiện tại:** `omni-prober` / `omni-analyst` / `omni-core` / `omni-executor` + `omni-gateway`; `omni-worker` thường **replicas=0** (không dùng làm mặc định cho `kubectl exec`).


| Script / artifact                                    | Trạng thái                                                                                                                                                        |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/gateway_alert_loki_verify.sh`               | **Đã cập nhật:** `exec` mặc định `**deploy/omni-prober**` (`E2E_EXEC_DEPLOY`); log + Loki gom split + gateway.                                                    |
| `scripts/follow-trace.sh`                            | **Đã cập nhật:** quét deploy split (+ `omni-worker` nếu scale>0); `FOLLOW_TRACE_DEPLOYS` override.                                                                |
| `scripts/proactive_e2e.sh`                           | **OK** — rollout cả split + gateway; `omni-worker` optional (`                                                                                                    |
| `scripts/full_system_audit.py`                       | **OK** — chọn metrics deploy prober khi worker=0.                                                                                                                 |
| `scripts/deploy_v6.sh`, `scripts/v63_deploy_test.sh` | **Legacy monolith** — chỉ đúng khi dùng một Deployment `omni-worker`; với split: **không** dùng làm pipeline chính (cập nhật doc hoặc khai tử khi bỏ hẳn legacy). |
| `scripts/chaos_autonomous_smoke.sh`                  | **Cần chỉnh hoặc gắn nhãn legacy** — vẫn `rollout`/`logs` `omni-worker`; chạy split phải trỏ prober/core hoặc tách job.                                           |


**Symptom:** `scripts/gateway_alert_loki_verify.sh` treo / fail vì `kubectl exec deploy/omni-worker` trong khi lab MPV3 **scale omni-worker = 0**.  
**Fix:** Script dùng `**E2E_EXEC_DEPLOY=omni-prober`** (Python + worker image); gom log trace từ **prober/analyst/core/executor** (+ worker nếu replicas>0); Loki LogQL `pod_name=~` cả split + gateway. `follow-trace.sh` cùng logic.

**Symptom:** Log `deep_scout` / `deep_scout_autonomous`: **404** `http://ollama-service:11434/api/embed` hoặc `/api/chat` — Ollama host cũ hoặc build không có route mới.  
**Fix:** `llm/ollama_client.py`: `embed` fallback `POST /api/embeddings` + `prompt` khi `/api/embed` → 404 (input str); `chat` fallback `POST /api/generate` + `system`/`prompt` khi `/api/chat` → 404 (non-stream). Verify: `pytest tests/test_ollama_client.py`.

**Symptom:** `omni-analyst` log `kafka_evidence_loop message error: fetch_baseline_system_prompt() missing 1 required positional argument: 'max_chars'` — evidence path hỏng sau đổi signature baseline.  
**Fix:** `reasoning_evidence_inbound.reason_diagnostic_evidence_only`: gọi `fetch_baseline_system_prompt(ctx.redis, ctx.settings.baseline_system_prompt_max_chars)` khi `baseline_snapshot_enabled` (cùng pattern `handlers.py`). Verify: log analyst không còn TypeError; `pytest tests/test_proactive_react_evidence.py`.

**Symptom:** Gateway dùng chung image worker `multi-agent-system:latest` + `hostPath` mount source — stack nặng, không tách bạch ingress.  
**Fix:** Image riêng `**omni-gateway:latest`** — `Dockerfile.gateway`, `requirements-gateway.txt`, `src/gateway/` baked in. Manifest `k8s/deployments/omni-gateway.yaml` (không hostPath). `make docker-gateway` rồi `make deploy-gateway`. `scripts/proactive_e2e.sh` build cả worker + gateway và apply/rollout `omni-gateway`.

**Symptom:** Cần Redis Sentinel HA thay vì một Pod `redis` standalone.  
**Fix:** Set `OMNI_REDIS_SENTINEL_HOSTS` (CSV `host:26379`) + `OMNI_REDIS_SENTINEL_MASTER_NAME` trong ConfigMap; worker dùng `redis.asyncio.sentinel.Sentinel`. Sentinel trên cluster: tự dựng StatefulSet/Helm theo [Redis Sentinel](https://redis.io/docs/management/sentinel/) — xem `docs/vendor/redis_sentinel_lab.md`. Rỗng → vẫn `OMNI_REDIS_URL`.

**Symptom:** Tách mutation khỏi analyst — chỉ executor chạy `pkg.executor`.  
**Fix:** `OMNI_WORKER_ROLE=executor` + Deployment `omni-executor` consume `omni-actions`; payload JSON `{"action":"execute_write_pending","trace_id":"…","data":{...}}` (cùng schema `execute_write_pending_from_redis`). Analyst chỉ `reason_diagnostic_evidence_only` (không `handle_inbound`). `make deploy-worker` áp `omni-executor.yaml`; `make ensure-kafka-topics` tạo `omni-actions`.

**Symptom:** Worker Pod CrashLoop — `socket.gaierror` / `Name or service not known` khi `init_pg_pool`; DSN mặc định code trỏ `pgpool-gateway` (không có Service trong namespace lab).  
**Fix:** Set `POSTGRES_RAG_DSN` từ Secret CNPG `omni-postgres-app` key `uri` (env trong Deployment, không hardcode password vào ConfigMap). `kubectl rollout restart deployment/omni-prober …` sau khi áp manifest.

**Symptom:** Chạy song song `deployment/omni-worker` (legacy full) với `omni-prober` — cả hai consume `omni-alerts` / xử lý trùng.  
**Fix:** Chọn một topology: `make deploy-worker` (ba Pod: prober + analyst + core, `OMNI_WORKER_ROLE` tương ứng) **hoặc** `make deploy-worker-legacy` (một Pod). Scale deployment không dùng về 0.

**Symptom:** Lab `k8s/kafka/kafka-single.yaml` dùng `bitnami/kafka:…` — sau `docker system prune` / registry Bitnami tag không pull được (`manifest unknown`).  
**Fix:** Chuyển image sang `apache/kafka:3.8.0` + env KRaft (`CLUSTER_ID`, `KAFKA_*` theo image `/etc/kafka/docker/run`). `kubectl apply -f k8s/kafka/kafka-single.yaml`; `make deploy-kafka`.

**Symptom:** `OMNI_GOD_MODE` / `OMNI_LAB_UNCHAINED` bật — unattended Prometheus vẫn nhận system `SLOW_SYSTEM_GOD_UNATTENDED_EN` (few-shot `kubectl top` → `execute_shell_command`), mâu thuẫn `[PRIORITY]` / SDK-first và dễ chọn shell thay vì `namespace_pods_top` / `kubectl_cluster`.  
**Fix:** `handlers.build_agentic_system_messages`: unattended luôn dùng `SLOW_SYSTEM_UNATTENDED_EN`; nếu lab/god thì nối `AGENTIC_LAB_SHELL_SUPPLEMENT_UNATTENDED_EN` (shell last resort). `ollama_prompts_en`: `SRE_JSON_GENERATOR_UNATTENDED_EN` làm rõ khi đã có pod+ns thì không áp rule “identifiers missing”. Verify: `pytest tests/test_handlers_inbound_preview.py`.

**Symptom:** Agentic Prometheus (multi-turn) kết thúc `HTTPStatusError: 500` từ Ollama `/api/chat` (~100s+); log có `llm_request` với user message chứa `[CONTEXT: topology_cache]` + nhiều chunk `infra_topology` (hàng chục nghìn ký tự) cộng dồn system messages — vượt ngưỡng context / làm Ollama lỗi.  
**Fix:** `handlers.py` — RagGate (`pkg.rag.gate`) trước preflight; **sau MISS** mọi source (kể cả prometheus) dùng `preflight_infra_kb` + `enrich_working_text_with_infra` với trần `OMNI_INFRA_ENRICH_MAX_TOTAL_CHARS` (`infra_context._apply_infra_enrich_cap`, log `event=infra_enrich_capped`). Verify: `pytest tests/test_prometheus_rag_gate.py`.

**Symptom:** E2E Prometheus chỉ thấy `handler_done autonomous_sdk`, không có agentic/Ollama — không kiểm chứng suy luận/resolved. Nguyên nhân: `_vietnamese_logs_intent` dùng `if "log" in text` → chuỗi **topology** trong `[OLLAMA_ANCHOR_EN]` chứa substring `log` → khớp nhầm → `inspect_pod_deep` trước LLM.  
**Fix:** `workers/autonomous_route.py` — nhận diện logs bằng `\blogs?\b` / `\btail\b` (word boundary), không substring. Test: `pytest tests/test_autonomous_route.py`.

**Symptom:** Cần anchor tiếng Anh cho Ollama (tránh hallucination pod), trigger CPU/RAM/…, và cap độ dài; prompt Ollama chuyển EN.  
**Fix:** `workers/prometheus_alert_enrichment.py` (infer trigger + `[OLLAMA_ANCHOR_EN]`); `workers/ollama_prompts_en.py` (English prompts, `OLLAMA_MAX_OUTPUT_WORDS=25` + `truncate_plain_text_to_max_words`); `handlers.py` ghép anchor; `tools.py` cắt `reply` / `omni_mark_resolved` theo **25 từ** không phải ký tự. Verify: `pytest tests/test_ollama_text_truncate.py tests/test_handlers_inbound_preview.py tests/test_agentic_slow_path.py`.

**Symptom:** Alert đã có `pod=` + `namespace=` nhưng agentic vẫn mở bằng `k8s_list_pods` / `namespace_pods_top` (log `tool_before` iter 0–1); không inspect pod/probe. Nguyên nhân: `_k8s_smart_target_hint` luôn gắn “list/discovery” khi text có từ khoá pod/namespace, lấn prompt “Inspect khi đã có định danh”.  
**Fix:** `workers/handlers.py`: `_parse_alert_pod_namespace_from_preview` + khi đủ pod+ns thì `_k8s_smart_target_hint` chuyển sang inspect-first; `build_agentic_system_messages` (unattended) thêm system `[PRIORITY — …]`. Verify: `pytest tests/test_handlers_inbound_preview.py`.

**Symptom:** Prometheus alert thiếu `instance` → inbound text kiểu `Alert: X on unknown - …`; LLM gọi `resolve_pod_identity(pod_name="unknown")` và fail.  
**Fix:** Chuẩn hoá `_effective_inbound_text_preview` trong `workers/handlers.py`: ưu tiên `pod`/`pod_name` + `namespace`/`deployment` trên dòng chính; không default `instance=unknown`; bỏ segment `on …` khi instance rỗng hoặc là sentinel `unknown|none|n/a`. Verify: `pytest tests/test_handlers_inbound_preview.py`.

## Product / ops behavior (e.g. proactive, gateway, workers)

**Symptom:** Prometheus unattended alert `identifiers=unspecified` — agentic turn 0 gọi `escalate_to_human` (insufficient_data); HTTP/stream vẫn 200 nhưng **business fail** (không điều tra).  
**Fix:** `prometheus_alert_enrichment.build_ollama_anchor_en` — hint khi thiếu FACTS bắt discovery (`list_all_pods_sdk` / promql) trước escalate; `agentic_slow_path` chặn escalate lượt đầu khi `unattended_alert` + chưa có tool; metric `omni_agent_premature_escalate_blocked_total`. Prompt unattended bỏ mâu thuẫn “insufficient → escalate”. Verify: `pytest tests/test_agentic_slow_path.py tests/test_prometheus_alert_enrichment.py`.

**Symptom:** Proactive ReAct chỉ diagnose (CSV default không có mutate tools), scale deployment giới hạn 0–10, gated promotion chỉ thực thi `k8s_rollout_restart` — incident đơn giản không tự rollout/scale/patch/kubectl.  
**Fix:** `OMNI_CLUSTER_FULL_ACCESS` (default true) bật full toolbelt + bỏ gate confidence trong proactive; tool `kubectl_cluster` (argv list, audit); `execution/promotion.py` dispatch mọi tool trong `PROMOTION_CLUSTER_TOOLS` qua registry; scale không cap trên (chỉ `ge=0`). Tắt: `OMNI_CLUSTER_FULL_ACCESS=false`. Verify: `pytest tests/test_policy_denylist.py tests/test_v3_tools.py`.

**Symptom:** Sau lỗi Ollama / restart worker, `omni:delayed_queue` / `omni:lock:*` / `omni:retry:*` kẹt — alert không xử lý sạch (bus là Kafka, không còn Redis Streams PEL).  
**Fix:** Trong pod worker: `PYTHONPATH=/app/src python -m devtools.redis_cleanup_stuck` (xóa delayed ZSET, SCAN lock/retry, DEL circuit breaker). Kafka lag/stuck consumer: reset group hoặc `k8s/kafka` tooling. Sau đó `kubectl rollout restart deployment/omni-worker -n multi-agent`.

**Symptom:** Lab chưa deploy Kafka — worker/gateway lỗi kết nối `kafka:9092`, không consume/produce.  
**Fix:** `kubectl apply -f k8s/kafka/kafka-single.yaml`, Service `kafka:9092`, ConfigMap `OMNI_KAFKA_BOOTSTRAP_SERVERS`. Topics mặc định `omni-*` (auto-create). Verify: rollout worker + gateway sau khi broker Ready.

*(none else yet)*

## Infrastructure (K8s, Redis, deploy, observability)

**Symptom:** `kubectl top node` có số liệu, `kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods` → `items: []`, `kubectl top pod` “No resources”, probe `k8s_clinical_pod_metrics` 404/INCONCLUSIVE — **metrics-server Deployment vẫn Running**.  
**Fix:** Không phải thiếu metrics-server. **metrics-server** lấy PodMetrics từ kubelet `**/stats/summary`**; nếu `**pods` trong summary = rỗng** thì không có PodMetrics. Triệu chứng: `kubectl top node` OK nhưng `kubectl top pod` trống / `PodMetricsList` rỗng. Chẩn đoán: `bash scripts/diagnose_kubelet_pod_metrics.sh` (đếm `pods` trong summary).  
**OrbStack (đã verify lab):** trong **Settings → Kubernetes → Kubelet Configuration** merge nội dung `**k8s/lab/kubelet-configuration-orbstack.yaml`** (feature gate `PodAndContainerStatsFromCRI: true`) — kubelet điền pod trong `/stats/summary` → PodMetrics / `kubectl top pod` hoạt động. Không đổi args `metrics-server` một mình nếu summary vẫn `pods: []`.  
**Ghi chú:** Checklist “cài metrics-server + `--kubelet-insecure-tls`” đúng khi chưa cài hoặc APIService lỗi; nếu APIService Available mà vẫn trống → kiểm `**stats/summary`**, không chỉ TLS.

**Symptom:** `make e2e-proactive` / `full_system_audit` fail ngay sau rollout worker — `Connection refused` khi exec `python -c` gọi `http://127.0.0.1:9090/metrics` (metrics chưa bind).  
**Fix:** `scripts/proactive_e2e.sh` chờ vòng lặp `curl` :9090 trả 200 (tối đa ~60s) sau `rollout status` rồi mới chạy audit. Verify: `bash scripts/proactive_e2e.sh --skip-build`.

**Symptom:** Redis Cluster 6 node + `OMNI_REDIS_CLUSTER=true` / `OMNI_REDIS_CLUSTER_NODES` — vận hành nặng; app dùng `RedisCluster`.  
**Fix:** `k8s/deployments/redis-standalone.yaml` (Service `redis`, StatefulSet + PVC, AOF `appendfsync everysec`, rewrite, preamble). ConfigMap: chỉ `OMNI_REDIS_URL=redis://redis:6379/0`, không cluster. Code: `redis_client.py` / gateway `Redis.from_url`. Monitor: `OmniRedisStandaloneDown`; redis-exporter standalone. `scripts/deploy_v6.sh` apply standalone. Verify: rollout `omni-worker` + `omni-gateway`, pytest.

**Symptom:** Cần dọn backlog `events:inbound` / `incidents:proactive` trước khi test E2E alert; queue dài làm trễ xử lý.  
**Fix:** `kubectl exec -n multi-agent deploy/omni-worker -- env PYTHONPATH=/app/src python -m devtools.redis_cleanup_stuck` — XACK pending + XTRIM flush cả hai stream + DEL delayed/lock như script. Verify: POST webhook rồi grep `trace_id` trong log worker.

*(none else yet)*