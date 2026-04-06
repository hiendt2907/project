# Known issues (symptom → fix)

Short entries only. **Newest first** within each section. If the same symptom already exists, update **Fix** instead of adding a duplicate.

## Logic / application

**Symptom:** Lab nginx-test stress **busy loop** trong container `nginx` → pod không ổn / CPU cAdvisor không phản ánh đúng (kìm master/worker).  
**Fix:** **kubectl port-forward** từ laptop là nút thắt (thường chỉ ~30–40% so với CPU limit pod). **`rakyll/hey:latest`** trên Docker Hub hay **ImagePullBackOff** (repo/deny). Lab: **Service** `nginx-test` + Pod **`curlimages/curl`** — N vòng `curl` song song tới `http://nginx-test.<ns>.svc.cluster.local/` (`STRESS_MODE=curl` mặc định, `LOAD_CONCURRENCY=1000`). Legacy: `STRESS_MODE=portforward`.

**Symptom:** `k8s_clinical_pod_metrics` log **404** `podmetrics.metrics.k8s.io "ns/pod" not found` — probe bị coi FAILED.  
**Fix:** 404 là **APIServer/Metrics API** khi **chưa có** CR `PodMetrics`. Nếu metrics-server đã chạy mà **mọi** namespace trống: xem **Infrastructure** — kubelet `/stats/summary` có thể trả `pods: []` (OrbStack/k3s). Không phải bug Python. `diagnostic_k8s_clinical.probe_k8s_clinical_pod_metrics`: **404 → INCONCLUSIVE** + `omit_reason=podmetrics_not_found_404`. Chẩn đoán: `bash scripts/diagnose_kubelet_pod_metrics.sh`. Verify: `pytest tests/test_diagnostic_k8s_clinical.py`.

**Symptom:** Log prober chỉ thấy **2** probe `prom_pod_*`, không có `k8s_clinical_*` / `diagnostic_dispatcher_plan kind=resource` — hoặc alert thiếu **label `pod`** (chỉ mô tả trong annotations), hoặc image/`resource_probe_ids` cũ chỉ còn Prom.  
**Fix:** `pod_identity_from_event`: fallback tên pod từ text `… in pod <name>` / `pod/<name>` (annotations + `error_hint`). `resource_probe_ids` luôn SDK trước rồi Prom. Verify: `pytest tests/test_diagnostic_resource.py`; `bash scripts/nginx_test_cpu_alert_lab.sh` (port-forward + curl host + POST gateway) — expect `probes=['k8s_clinical_…', 'prom_…']` trong log prober. Lab CPU thật: `scripts/nginx-test-deployment.yaml` limit **50m**; rule **NginxTestHighCpuLab** `> 0.04` **for 60s** trong `k8s/monitor/prometheus.yaml` (group `omni_lab_nginx`); `kubectl apply` ConfigMap monitor + rollout Prometheus; firing → gateway cần **Alertmanager** (repo không ship) — script vẫn POST synthetic để chứng pipeline.

**Symptom:** Lab nginx-test CPU ~90% nhưng Prometheus không firing / expr không match.  
**Fix:** Kiểm tra label cAdvisor (`namespace` vs `kubernetes_namespace` tùy cluster); đồng bộ limit pod với ngưỡng rule (0.04 cores ≈ ~80% của **50m**). `STRESS_SEC` ≥ 75s; `curl` instant query trong script để xem rate.

**Symptom:** Proactive ReAct / Telegram báo **ESCALATED** / `k8s_rollout_restart` lung tung khi payload Kafka chỉ có `canonical_query` stub, **không** namespace **không** `trigger_promql` — GIGO (garbage in → garbage out). MPV3 có **coerce GIGO-safe** cho **evidence** (`pkg/reasoning/schema.py`) nhưng **không** tự áp cho incident proactive.  
**Fix:** `proactive_gigo_cluster_identity_ok`: bỏ qua sớm với audit **`SKIPPED_GIGO`** nếu thiếu **cả** `namespace` **và** `trigger_promql` (bật `OMNI_PROACTIVE_GIGO_REQUIRE_CLUSTER_IDENTITY`, mặc định true). `evaluate_proactive_triggers` vẫn pass vì luôn set `trigger_promql`. `full_system_audit` inject thêm `namespace` + `trigger_promql`. Verify: `pytest tests/test_proactive_guardrails.py`.

**Symptom:** Trace `gw-prom-*` có trong `request_trace` nhưng **không** xuyên suốt: access log Uvicorn không có trace; log **asyncio** `Unclosed client session` (aiohttp) không gắn trace — khó debug.  
**Fix:** Gateway: **`trace_id` sinh ngay đầu** `POST /webhook/prometheus`, `request.state.trace_id`, `push_gateway_trace_id`/`pop` bọc handler, **`install_gateway_trace_logging()`** (filter inject `[trace_id=…]` cho root/uvicorn/asyncio/aiohttp; bỏ qua nếu chuỗi đã chứa id), header **`X-Omni-Trace-Id`**, middleware `http_done`. Worker stream/proactive: **`push_trace_id`/`pop`** trong `_process_stream_entry` + `_process_proactive_message`; JSON log thêm field **`trace_id`** từ `current_trace_id()`. Probe: **`probe_k8s_list_pods_namespace`** `await v1.api_client.close()` trong `finally`. Verify: `bash scripts/gateway_alert_loki_verify.sh` (grep cùng trace gateway + split workers); `make e2e-proactive` (gateway POST + proactive inject). Build: `make docker-gateway` + `make docker-worker` trước rollout.

### Verify scripts vs MPV3 split topology (tránh tham chiếu Pod sai / nhiễu RAG)

**Chuẩn lab hiện tại:** `omni-prober` / `omni-analyst` / `omni-core` / `omni-executor` + `omni-gateway`; `omni-worker` thường **replicas=0** (không dùng làm mặc định cho `kubectl exec`).

| Script / artifact | Trạng thái |
|---------------------|------------|
| `scripts/gateway_alert_loki_verify.sh` | **Đã cập nhật:** `exec` mặc định **`deploy/omni-prober`** (`E2E_EXEC_DEPLOY`); log + Loki gom split + gateway. |
| `scripts/follow-trace.sh` | **Đã cập nhật:** quét deploy split (+ `omni-worker` nếu scale>0); `FOLLOW_TRACE_DEPLOYS` override. |
| `scripts/proactive_e2e.sh` | **OK** — rollout cả split + gateway; `omni-worker` optional (`|| true` rollout status). |
| `scripts/full_system_audit.py` | **OK** — chọn metrics deploy prober khi worker=0. |
| `scripts/deploy_v6.sh`, `scripts/v63_deploy_test.sh` | **Legacy monolith** — chỉ đúng khi dùng một Deployment `omni-worker`; với split: **không** dùng làm pipeline chính (cập nhật doc hoặc khai tử khi bỏ hẳn legacy). |
| `scripts/chaos_autonomous_smoke.sh` | **Cần chỉnh hoặc gắn nhãn legacy** — vẫn `rollout`/`logs` `omni-worker`; chạy split phải trỏ prober/core hoặc tách job. |

**Symptom:** `scripts/gateway_alert_loki_verify.sh` treo / fail vì `kubectl exec deploy/omni-worker` trong khi lab MPV3 **scale omni-worker = 0**.  
**Fix:** Script dùng **`E2E_EXEC_DEPLOY=omni-prober`** (Python + worker image); gom log trace từ **prober/analyst/core/executor** (+ worker nếu replicas>0); Loki LogQL `pod_name=~` cả split + gateway. `follow-trace.sh` cùng logic.

**Symptom:** Log `deep_scout` / `deep_scout_autonomous`: **404** `http://ollama-service:11434/api/embed` hoặc `/api/chat` — Ollama host cũ hoặc build không có route mới.  
**Fix:** `llm/ollama_client.py`: `embed` fallback `POST /api/embeddings` + `prompt` khi `/api/embed` → 404 (input str); `chat` fallback `POST /api/generate` + `system`/`prompt` khi `/api/chat` → 404 (non-stream). Verify: `pytest tests/test_ollama_client.py`.

**Symptom:** `omni-analyst` log `kafka_evidence_loop message error: fetch_baseline_system_prompt() missing 1 required positional argument: 'max_chars'` — evidence path hỏng sau đổi signature baseline.  
**Fix:** `reasoning_evidence_inbound.reason_diagnostic_evidence_only`: gọi `fetch_baseline_system_prompt(ctx.redis, ctx.settings.baseline_system_prompt_max_chars)` khi `baseline_snapshot_enabled` (cùng pattern `handlers.py`). Verify: log analyst không còn TypeError; `pytest tests/test_proactive_react_evidence.py`.

**Symptom:** Gateway dùng chung image worker `multi-agent-system:latest` + `hostPath` mount source — stack nặng, không tách bạch ingress.  
**Fix:** Image riêng **`omni-gateway:latest`** — `Dockerfile.gateway`, `requirements-gateway.txt`, `src/gateway/` baked in. Manifest `k8s/deployments/omni-gateway.yaml` (không hostPath). `make docker-gateway` rồi `make deploy-gateway`. `scripts/proactive_e2e.sh` build cả worker + gateway và apply/rollout `omni-gateway`.

**Symptom:** Muốn dùng Ollama chạy sẵn trên Mac (M4), không deploy `ollama/ollama` trong K8s.  
**Fix:** Service **`ExternalName`** `ollama-service` → `host.docker.internal` — `k8s/deployments/ollama-service.yaml`; `make deploy-ollama`. Pod gọi `http://ollama-service:11434` → host Ollama. Nếu **connection refused**: trên Mac bật bind `0.0.0.0:11434` (ví dụ `OLLAMA_HOST=0.0.0.0:11434`). Verify: `kubectl exec deploy/omni-prober -n multi-agent -- curl -s -o /dev/null -w "%{http_code}" http://ollama-service:11434/api/tags` → **200**.

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

_(none else yet)_

## Infrastructure (K8s, Redis, deploy, observability)

**Symptom:** `kubectl top node` có số liệu, `kubectl get --raw /apis/metrics.k8s.io/v1beta1/pods` → `items: []`, `kubectl top pod` “No resources”, probe `k8s_clinical_pod_metrics` 404/INCONCLUSIVE — **metrics-server Deployment vẫn Running**.  
**Fix:** Không phải thiếu metrics-server. **metrics-server** lấy PodMetrics từ kubelet **`/stats/summary`**; nếu **`pods` trong summary = rỗng** thì không có PodMetrics. Triệu chứng: `kubectl top node` OK nhưng `kubectl top pod` trống / `PodMetricsList` rỗng. Chẩn đoán: `bash scripts/diagnose_kubelet_pod_metrics.sh` (đếm `pods` trong summary).  
**OrbStack (đã verify lab):** trong **Settings → Kubernetes → Kubelet Configuration** merge nội dung **`k8s/lab/kubelet-configuration-orbstack.yaml`** (feature gate `PodAndContainerStatsFromCRI: true`) — kubelet điền pod trong `/stats/summary` → PodMetrics / `kubectl top pod` hoạt động. Không đổi args `metrics-server` một mình nếu summary vẫn `pods: []`.  
**Ghi chú:** Checklist “cài metrics-server + `--kubelet-insecure-tls`” đúng khi chưa cài hoặc APIService lỗi; nếu APIService Available mà vẫn trống → kiểm **`stats/summary`**, không chỉ TLS.

**Symptom:** `make e2e-proactive` / `full_system_audit` fail ngay sau rollout worker — `Connection refused` khi exec `python -c` gọi `http://127.0.0.1:9090/metrics` (metrics chưa bind).  
**Fix:** `scripts/proactive_e2e.sh` chờ vòng lặp `curl` :9090 trả 200 (tối đa ~60s) sau `rollout status` rồi mới chạy audit. Verify: `bash scripts/proactive_e2e.sh --skip-build`.

**Symptom:** Redis Cluster 6 node + `OMNI_REDIS_CLUSTER=true` / `OMNI_REDIS_CLUSTER_NODES` — vận hành nặng; app dùng `RedisCluster`.  
**Fix:** `k8s/deployments/redis-standalone.yaml` (Service `redis`, StatefulSet + PVC, AOF `appendfsync everysec`, rewrite, preamble). ConfigMap: chỉ `OMNI_REDIS_URL=redis://redis:6379/0`, không cluster. Code: `redis_client.py` / gateway `Redis.from_url`. Monitor: `OmniRedisStandaloneDown`; redis-exporter standalone. `scripts/deploy_v6.sh` apply standalone. Verify: rollout `omni-worker` + `omni-gateway`, pytest.

**Symptom:** Cần dọn backlog `events:inbound` / `incidents:proactive` trước khi test E2E alert; queue dài làm trễ xử lý.  
**Fix:** `kubectl exec -n multi-agent deploy/omni-worker -- env PYTHONPATH=/app/src python -m devtools.redis_cleanup_stuck` — XACK pending + XTRIM flush cả hai stream + DEL delayed/lock như script. Verify: POST webhook rồi grep `trace_id` trong log worker.

_(none else yet)_
