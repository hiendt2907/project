# Root Makefile — minimal targets for CI and local evidence.
.PHONY: traefik-install traefik-uninstall nginx-uninstall hosts-update test-evidence omni-death-loop docker-worker docker-gateway docker-hitl-api deploy-worker deploy-worker-legacy legacy-deploy-worker deploy-ollama deploy-gateway deploy-services deploy-kafka deploy-prober-rbac ensure-kafka-topics e2e-proactive e2e-incident-matrix e2e-nginx-missing-configmap chaos-rag-lab lab-nginx-cpu lab-nginx-cpu-overlap autonomy-gate env-mode-gate mutate-only-gate classifier-regression-gate phase-docs-gate nonimpact-guards-gate learning-loop-gate secret-gate secret-history-audit deploy-siem-stack deploy-hitl-api verify-hitl-production hitl-gate prove-siem-capabilities siem-proof-3x siem-lab-gate print-image-digests siem-lab-inject siem-deploy-workers teardown-omni-postgres

# ── Traefik ingress management ────────────────────────────────────────────────
traefik-install:
	helm repo add traefik https://traefik.github.io/charts && helm repo update traefik
	helm upgrade --install traefik traefik/traefik \
	  -n traefik --create-namespace \
	  -f k8s/ingress/traefik-values.yaml
	kubectl apply -f k8s/ingress/traefik-middlewares.yaml
	kubectl apply -f k8s/ingress/ai-agent-local.yaml

traefik-uninstall:
	helm uninstall traefik -n traefik

nginx-uninstall:
	kubectl delete validatingwebhookconfiguration ingress-nginx-admission --ignore-not-found
	kubectl delete ingressclass nginx --ignore-not-found
	kubectl delete ns ingress-nginx --ignore-not-found

hosts-update:
	@TRAEFIK_IP=$$(kubectl get svc -n traefik traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'); \
	echo "Traefik IP: $$TRAEFIK_IP"; \
	sudo sed -i '' "s|.*ai-agent\.local.*|$$TRAEFIK_IP omni.ai-agent.local finguard.ai-agent.local gateway.ai-agent.local siem.ai-agent.local|" /etc/hosts && \
	grep ai-agent /etc/hosts

test-evidence:
	bash scripts/run_test_evidence.sh

# nginx-test CPU lab: deploy + optional stress + POST gateway (see scripts/nginx_test_cpu_alert_lab.sh header).
lab-nginx-cpu:
	NS=multi-agent bash scripts/nginx_test_cpu_alert_lab.sh

# Tải in-cluster + giữ load khi POST (true alarm path; LOAD_CONCURRENCY an toàn, không 10k process).
lab-nginx-cpu-overlap:
	NS=multi-agent STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15 OVERLAP_STRESS_SEC=120 LOAD_CONCURRENCY=256 WAIT_PROM_CPU=1 SLEEP_SEC=45 bash scripts/nginx_test_cpu_alert_lab.sh

# nginx-test: patch envFrom → ConfigMap rồi xóa CM (CreateContainerConfigError) + gateway trace (fault thật).
e2e-nginx-missing-configmap:
	NS=multi-agent bash scripts/e2e_nginx_missing_configmap.sh

docker-worker:
	docker build -t multi-agent-system:latest -f Dockerfile .

docker-gateway:
	docker build -t omni-gateway:latest -f Dockerfile.gateway .

# Builds finguard-hitl-api binary from smart-siem monorepo.
# Build context is smart-siem/ so go.work + vendor are on PATH.
# Tag: finguard-hitl-api:lab  (override with IMAGE_TAG=prod make docker-hitl-api)
docker-hitl-api:
	docker build -t finguard-hitl-api:$${IMAGE_TAG:-lab} \
	  -f smart-siem/Dockerfile.hitl-api \
	  smart-siem/

# Print image digests for all SIEM/HITL images — copy sha256 values into manifests for digest pinning.
print-image-digests:
	@echo "=== Image digests for prod pinning (copy into kustomization newDigest or manifest image field) ==="
	@docker inspect omni-hitl-dispatcher:latest --format='omni-hitl-dispatcher:latest  {{.Id}}' 2>/dev/null || echo "omni-hitl-dispatcher:latest  NOT BUILT (run: make docker-hitl-dispatcher)"
	@docker inspect multi-agent-system:latest   --format='multi-agent-system:latest    {{.Id}}' 2>/dev/null || echo "multi-agent-system:latest    NOT BUILT (run: make docker-worker)"
	@docker inspect finguard-hitl-api:lab       --format='finguard-hitl-api:lab        {{.Id}}' 2>/dev/null || echo "finguard-hitl-api:lab        NOT BUILT (run: make docker-hitl-api)"
	@docker inspect finguard-hitl-api:prod      --format='finguard-hitl-api:prod       {{.Id}}' 2>/dev/null || echo "finguard-hitl-api:prod       NOT BUILT (run: IMAGE_TAG=prod make docker-hitl-api)"

# Master Plan V3: omni-prober (alerts+diagnostic) + omni-analyst (evidence) + omni-core (periodic/proactive).
deploy-worker:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/prober-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/analyst-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-prober.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-analyst.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-core.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-executor.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-prober deployment/omni-analyst deployment/omni-core deployment/omni-executor -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-prober -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-analyst -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-core -n multi-agent --timeout=180s
	./scripts/with_working_kube.sh rollout status deployment/omni-executor -n multi-agent --timeout=180s

# Ollama trên Mac — chỉ Service ExternalName → host.docker.internal:11434 (không Deployment trong cluster).
deploy-ollama:
	./scripts/with_working_kube.sh apply -f k8s/deployments/ollama-service.yaml

# Gateway FastAPI — image riêng `omni-gateway:latest` (chạy `make docker-gateway` trước khi rollout).
deploy-gateway:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-gateway.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-gateway -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-gateway -n multi-agent --timeout=180s

# Gom Ollama + Gateway (build gateway: `make docker-gateway`; worker không bắt buộc cho gateway).
deploy-services: deploy-ollama deploy-gateway

# Single-process legacy: OMNI_WORKER_ROLE=full (monolith). Scale omni-prober/analyst/core to 0 if using this.
deploy-worker-legacy:
	@echo "[legacy] deploy-worker-legacy is deprecated; use legacy-deploy-worker."
	@$(MAKE) legacy-deploy-worker

legacy-deploy-worker:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-worker -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-worker -n multi-agent --timeout=180s

deploy-kafka:
	./scripts/with_working_kube.sh apply -f k8s/kafka/kafka-single.yaml

deploy-prober-rbac:
	./scripts/with_working_kube.sh apply -f k8s/deployments/prober-rbac.yaml

ensure-kafka-topics:
	bash scripts/kafka_ensure_omni_topics.sh

rag-hot-sync:
	PYTHONPATH=src .venv/bin/python scripts/rag_hot_sync_worker.py

e2e-proactive:
	NS=multi-agent bash scripts/proactive_e2e.sh

e2e-incident-matrix:
	NS=multi-agent bash scripts/e2e_incident_matrix.sh

# Death loop: build -> deploy -> optional pytest_unit -> product_e2e (gateway Loki strict, proactive e2e, incident matrix). Pytest is secondary; override NS via env.
NS ?= multi-agent
omni-death-loop:
	NS=$(NS) bash scripts/omni_dev_death_loop.sh

chaos-rag-lab:
	NS=multi-agent bash scripts/chaos_rag_lab_run.sh

env-mode-gate:
	.venv/bin/python scripts/validate_env_mode_gate.py

mutate-only-gate:
	.venv/bin/python scripts/validate_mutate_only_gate.py

classifier-regression-gate:
	.venv/bin/python scripts/validate_classifier_regression_gate.py

phase-docs-gate:
	.venv/bin/python scripts/validate_phase_docs_gate.py

nonimpact-guards-gate:
	.venv/bin/python scripts/validate_nonimpact_guards_gate.py

learning-loop-gate:
	.venv/bin/python scripts/validate_learning_loop_gate.py

secret-gate:
	docker run --rm -v "$$(pwd):/repo" zricethezav/gitleaks:v8.18.2 detect --no-git --source=/repo --config=/repo/.gitleaks.toml --report-path=/repo/leak_report.json --verbose

secret-history-audit:
	docker run --rm -v "$$(pwd):/repo" zricethezav/gitleaks:v8.18.2 detect --source=/repo --config=/repo/.gitleaks.toml --report-path=/repo/leak_report_history.json --verbose

# Phase 5 gate: fail when autonomy verification regresses.
autonomy-gate:
	$(MAKE) secret-gate
	.venv/bin/python scripts/validate_env_mode_gate.py
	.venv/bin/python scripts/validate_mutate_only_gate.py
	.venv/bin/python scripts/validate_classifier_regression_gate.py
	.venv/bin/python scripts/validate_phase_docs_gate.py
	.venv/bin/python scripts/validate_nonimpact_guards_gate.py
	.venv/bin/python scripts/validate_learning_loop_gate.py
	.venv/bin/python -m pytest tests/test_autonomous_experience_gate.py tests/test_agentic_planner_early_exit.py tests/test_feedback_full_agentic_planner.py tests/test_deterministic_mutate_from_evidence.py tests/test_omni_stateful_loop.py tests/test_shadow_os_contract.py tests/integration/test_e2e_autonomous_loop.py -q
	$(MAKE) hitl-gate
	.venv/bin/python scripts/full_system_audit.py --duration-sec 90 --interval-sec 10 --strict --min-action-experience 0 --sigma-min-hits 0

# SIEM stack: deploy SIEM bridge + HITL dispatcher + EvidenceAdapter (multi-agent namespace).
# Run `make docker-worker` first to rebuild the image with the new services.
deploy-siem-stack:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-siem-bridge.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-hitl-dispatcher.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-evidence-adapter.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-siem-bridge deployment/omni-hitl-dispatcher deployment/omni-evidence-adapter -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-siem-bridge -n multi-agent --timeout=120s
	./scripts/with_working_kube.sh rollout status deployment/omni-hitl-dispatcher -n multi-agent --timeout=120s
	./scripts/with_working_kube.sh rollout status deployment/omni-evidence-adapter -n multi-agent --timeout=120s

# Deploy finguard-hitl-api into finguard-customer namespace via kustomize.
# Prerequisite: `make docker-hitl-api` and image imported into cluster (k3s ctr import or registry push).
# Secrets must be pre-created out-of-band; see scripts/rotate_hitl_token.sh.
deploy-hitl-api:
	./scripts/with_working_kube.sh apply -k smart-siem/customer/k3s/overlays/lab
	./scripts/with_working_kube.sh rollout status deployment/finguard-hitl-api -n finguard-customer --timeout=120s

# Run post-deploy production readiness gate for the full HITL path.
verify-hitl-production:
	bash scripts/verify_hitl_production.sh

# CI gate: unit tests + token rotation script linting.
# Safe to run without cluster access; tests use fakeredis/mock Kafka.
hitl-gate:
	.venv/bin/python -m pytest tests/test_hitl_dispatcher.py -q --tb=short
	bash -n scripts/rotate_hitl_token.sh
	bash -n scripts/verify_hitl_production.sh

# Run full SIEM capability proof (CAP-1 through CAP-6) against live cluster.
# Requires: cluster port-forwards active (Kafka:29092, Redis:16379/19379, HITL:18081).
# HITL_TOKEN is read from in-cluster secret; no env var needed when running locally.
# Output: artifacts/siem_capability_proof_<YYYYMMDD_HHMM>.json
prove-siem-capabilities:
	mkdir -p artifacts
	HITL_TOKEN=$$(./scripts/with_working_kube.sh get secret hitl-dispatcher-secret -n multi-agent \
	  -o jsonpath='{.data.hitl_api_token}' | base64 -d) \
	  .venv/bin/python scripts/prove_siem_capabilities.py \
	    --out artifacts/siem_capability_proof_$$(date +%Y%m%d_%H%M).json

# Run capability proof 3 consecutive times to catch flakes.
# All 3 must pass; fails fast on first failure.
siem-proof-3x:
	@echo "=== siem-proof-3x: run 1/3 ===" && $(MAKE) prove-siem-capabilities
	@echo "=== siem-proof-3x: run 2/3 ===" && $(MAKE) prove-siem-capabilities
	@echo "=== siem-proof-3x: run 3/3 ===" && $(MAKE) prove-siem-capabilities
	@echo "=== siem-proof-3x: all 3 runs passed ==="

# Lab-only gate: full SIEM capability proof + production readiness check.
# Requires live cluster with port-forwards. NOT run in GitHub CI.
# To run: make siem-lab-gate
siem-lab-gate:
	$(MAKE) verify-hitl-production
	$(MAKE) prove-siem-capabilities

# Smart SIEM: inject fake log events vào stream:siem_normalized để test pipeline.
# SCENARIO=brute_force|port_scan|normal|all  COUNT=15  FG_NAMESPACE=finguard-customer
siem-lab-inject:
	bash smart-siem/scripts/siem_log_injector.sh

# Smart SIEM: scale brain-go + math-gateway + agent lên replicas=1 (sau khi đã build image).
siem-deploy-workers:
	./scripts/with_working_kube.sh apply -f smart-siem/customer/k3s/components/redis-stream-workers/worker-deployments.yaml
	./scripts/with_working_kube.sh rollout restart deployment/finguard-brain-go deployment/finguard-math-gateway deployment/finguard-agent -n finguard-customer
	./scripts/with_working_kube.sh rollout status deployment/finguard-brain-go -n finguard-customer --timeout=120s
	./scripts/with_working_kube.sh rollout status deployment/finguard-math-gateway -n finguard-customer --timeout=120s
	./scripts/with_working_kube.sh rollout status deployment/finguard-agent -n finguard-customer --timeout=120s

# Playbooks now stored in Redis Stack (HNSW index). Legacy Postgres schema removed.

# Teardown Omni Postgres cluster (RAG already migrated to Redis Stack).
# Dry-run by default; pass APPLY=1 to actually delete.
teardown-omni-postgres:
	@if [ "$(APPLY)" = "1" ]; then \
		./scripts/teardown_omni_postgres.sh --apply; \
	else \
		./scripts/teardown_omni_postgres.sh; \
	fi
