# Known issues (symptom → fix)

Short entries only. **Newest first** within each section. If the same symptom already exists, update **Fix** instead of adding a duplicate.

## Logic / application

**Symptom:** Lab `k8s/kafka/kafka-single.yaml` dùng `bitnami/kafka:…` — sau `docker system prune` / registry Bitnami tag không pull được (`manifest unknown`).  
**Fix:** Chuyển image sang `apache/kafka:3.8.0` + env KRaft (`CLUSTER_ID`, `KAFKA_*` theo image `/etc/kafka/docker/run`). `kubectl apply -f k8s/kafka/kafka-single.yaml`; `make deploy-kafka`.

**Symptom:** `OMNI_GOD_MODE` / `OMNI_LAB_UNCHAINED` bật — unattended Prometheus vẫn nhận system `SLOW_SYSTEM_GOD_UNATTENDED_EN` (few-shot `kubectl top` → `execute_shell_command`), mâu thuẫn `[PRIORITY]` / SDK-first và dễ chọn shell thay vì `namespace_pods_top` / `kubectl_cluster`.  
**Fix:** `handlers.build_agentic_system_messages`: unattended luôn dùng `SLOW_SYSTEM_UNATTENDED_EN`; nếu lab/god thì nối `AGENTIC_LAB_SHELL_SUPPLEMENT_UNATTENDED_EN` (shell last resort). `ollama_prompts_en`: `SRE_JSON_GENERATOR_UNATTENDED_EN` làm rõ khi đã có pod+ns thì không áp rule “identifiers missing”. Verify: `pytest tests/test_handlers_inbound_preview.py`.

**Symptom:** Agentic Prometheus (multi-turn) kết thúc `HTTPStatusError: 500` từ Ollama `/api/chat` (~100s+); log có `llm_request` với user message chứa `[CONTEXT: topology_cache]` + nhiều chunk `infra_topology` (hàng chục nghìn ký tự) cộng dồn system messages — vượt ngưỡng context / làm Ollama lỗi.  
**Fix:** `handlers.py` — với `source=prometheus` bỏ `preflight_infra_kb` + `enrich_working_text_with_infra` (anchor `[OLLAMA_ANCHOR_EN]` đã có FACTS). Verify: `pytest tests/test_prometheus_infra_skip.py`.

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

**Symptom:** `make e2e-proactive` / `full_system_audit` fail ngay sau rollout worker — `Connection refused` khi exec `python -c` gọi `http://127.0.0.1:9090/metrics` (metrics chưa bind).  
**Fix:** `scripts/proactive_e2e.sh` chờ vòng lặp `curl` :9090 trả 200 (tối đa ~60s) sau `rollout status` rồi mới chạy audit. Verify: `bash scripts/proactive_e2e.sh --skip-build`.

**Symptom:** Redis Cluster 6 node + `OMNI_REDIS_CLUSTER=true` / `OMNI_REDIS_CLUSTER_NODES` — vận hành nặng; app dùng `RedisCluster`.  
**Fix:** `k8s/deployments/redis-standalone.yaml` (Service `redis`, StatefulSet + PVC, AOF `appendfsync everysec`, rewrite, preamble). ConfigMap: chỉ `OMNI_REDIS_URL=redis://redis:6379/0`, không cluster. Code: `redis_client.py` / gateway `Redis.from_url`. Monitor: `OmniRedisStandaloneDown`; redis-exporter standalone. `scripts/deploy_v6.sh` apply standalone. Verify: rollout `omni-worker` + `omni-gateway`, pytest.

**Symptom:** Cần dọn backlog `events:inbound` / `incidents:proactive` trước khi test E2E alert; queue dài làm trễ xử lý.  
**Fix:** `kubectl exec -n multi-agent deploy/omni-worker -- env PYTHONPATH=/app/src python -m devtools.redis_cleanup_stuck` — XACK pending + XTRIM flush cả hai stream + DEL delayed/lock như script. Verify: POST webhook rồi grep `trace_id` trong log worker.

_(none else yet)_
