# Root Makefile — minimal targets for CI and local evidence.
.PHONY: test-evidence docker-worker docker-gateway deploy-worker deploy-worker-legacy legacy-deploy-worker deploy-ollama deploy-gateway deploy-services deploy-kafka deploy-prober-rbac ensure-kafka-topics e2e-proactive e2e-incident-matrix lab-nginx-cpu lab-nginx-cpu-overlap autonomy-gate env-mode-gate mutate-only-gate classifier-regression-gate phase-docs-gate secret-gate secret-history-audit

test-evidence:
	bash scripts/run_test_evidence.sh

# nginx-test CPU lab: deploy + optional stress + POST gateway (see scripts/nginx_test_cpu_alert_lab.sh header).
lab-nginx-cpu:
	bash scripts/nginx_test_cpu_alert_lab.sh

# Tải in-cluster + giữ load khi POST (true alarm path; LOAD_CONCURRENCY an toàn, không 10k process).
lab-nginx-cpu-overlap:
	STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15 OVERLAP_STRESS_SEC=120 LOAD_CONCURRENCY=256 WAIT_PROM_CPU=1 SLEEP_SEC=45 bash scripts/nginx_test_cpu_alert_lab.sh

docker-worker:
	docker build -t multi-agent-system:latest -f Dockerfile .

docker-gateway:
	docker build -t omni-gateway:latest -f Dockerfile.gateway .

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
	bash scripts/proactive_e2e.sh

e2e-incident-matrix:
	bash scripts/e2e_incident_matrix.sh

env-mode-gate:
	.venv/bin/python scripts/validate_env_mode_gate.py

mutate-only-gate:
	.venv/bin/python scripts/validate_mutate_only_gate.py

classifier-regression-gate:
	.venv/bin/python scripts/validate_classifier_regression_gate.py

phase-docs-gate:
	.venv/bin/python scripts/validate_phase_docs_gate.py

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
	.venv/bin/python -m pytest tests/test_autonomous_contract.py tests/test_analyst_agentic_loop.py tests/test_diagnostic_mapping.py tests/test_evidence_proof_gate.py tests/test_proactive_fail_safe.py tests/test_proactive_guardrails.py tests/integration/test_autonomy_loop_transitions.py tests/integration/test_autonomy_transition_contract_strict.py -q
	.venv/bin/python scripts/full_system_audit.py --duration-sec 90 --interval-sec 10 --strict --min-action-experience 0
