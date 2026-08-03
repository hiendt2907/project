# Root Makefile — minimal targets for CI and local evidence.
.PHONY: lint-imports list-omni-postgres-backups restore-omni-postgres-verify verify-case-ledger deploy-landing sync-public sync-public-ui sync-public-backend sync-public-all agent-bundle agent-bundle-offline agent-keygen publish-agent-release tunnel-setup tunnel-teardown ssh-tunnel traefik-install traefik-uninstall nginx-uninstall hosts-update test-evidence omni-death-loop docker-worker docker-gateway docker-hitl-api deploy-worker deploy-fullstack deploy-ollama deploy-gateway deploy-services deploy-kafka deploy-prober-rbac deploy-netpol ensure-kafka-topics e2e-proactive e2e-incident-matrix e2e-nginx-missing-configmap e2e-portal product-release-gate chaos-rag-lab lab-nginx-cpu lab-nginx-cpu-overlap autonomy-gate env-mode-gate mutate-only-gate auto-execute-gate classifier-regression-gate phase-docs-gate nonimpact-guards-gate learning-loop-gate secret-gate secret-history-audit deploy-siem-stack deploy-hitl-api verify-hitl-production hitl-gate prove-siem-capabilities siem-proof-3x siem-lab-gate print-image-digests siem-lab-inject siem-deploy-workers teardown-omni-postgres rollback rollback-verify pre-deploy-validate benchmark-advisory coverage coverage-html coverage-gate coverage-gate-strict coverage-waves coverage-project-real sbom chaos-drill chaos-drill-dry chaos-drill-rollback chaos-drill-rollback-dry chaos-drill-redis chaos-drill-kafka chaos-drill-llm chaos-drill-evidence-flood chaos-drill-pod-kill chaos-drill-all wait-omni-consumer-ready backend-verify-local backend-verify-job-infra backend-verify-job-apply backend-verify-job-run

NS ?= multi-agent

# ── Remote Agent packaging & setup ───────────────────────────────────────────
agent-bundle:
	bash scripts/omni-agent-bundle.sh

agent-bundle-offline:
	bash scripts/omni-agent-bundle.sh --offline

agent-keygen:
	bash scripts/omni-agent-keygen.sh

# Publish expected agent release (version + bundle sha256) → Redis manifest.
# Gateway compares every registered agent against it (drift detection, IT-2).
publish-agent-release:
	.venv/bin/python scripts/publish_agent_release.py --bundle-b64 /tmp/omni-agent-release.b64 | kubectl -n $(NS) exec -i redis-0 -- redis-cli -x SET omni:agent:release_manifest
	kubectl -n $(NS) exec -i redis-0 -- redis-cli -x SET omni:agent:release_bundle < /tmp/omni-agent-release.b64
	rm -f /tmp/omni-agent-release.b64
	kubectl -n $(NS) exec redis-0 -- redis-cli GET omni:agent:release_manifest

tunnel-setup:
	@test -n "$(DOMAIN)" || (echo "Usage: make tunnel-setup DOMAIN=omni-gateway.yourdomain.com"; exit 1)
	bash scripts/omni-tunnel-setup.sh --domain $(DOMAIN)

tunnel-teardown:
	bash scripts/omni-tunnel-teardown.sh

ssh-tunnel:
	@test -n "$(HOST)" || (echo "Usage: make ssh-tunnel HOST=user@192.168.1.100"; exit 1)
	bash scripts/omni-ssh-tunnel.sh --host $(HOST) --persist

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
	sudo sed -i '' "s|.*ai-agent\.local.*|$$TRAEFIK_IP dex.ai-agent.local provider.ai-agent.local tenant.ai-agent.local finguard.ai-agent.local gateway.ai-agent.local soc.ai-agent.local|" /etc/hosts && \
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

# Poll analyst/worker /readyz (OMNI_WORKER_READY_URL; default localhost:8090).
wait-omni-consumer-ready:
	PYTHONPATH=src .venv/bin/python scripts/wait_omni_consumer_ready.py

# Gateway HTTP webhook + trace-scoped DLQ poll + optional gateway /metrics CB scrape (lab env vars).
backend-verify-local:
	PYTHONPATH=src .venv/bin/python scripts/omni_backend_verify.py

# Gateway NetPol (lab) + analyst ClusterIP Service — prerequisites for in-cluster verify Job.
backend-verify-job-infra:
	./scripts/with_working_kube.sh apply -f k8s/network-policies/omni-gateway-netpol-ingress-multi-agent.yaml
	./scripts/with_working_kube.sh apply -f k8s/services/omni-analyst-service.yaml

# Apply verify Job manifest (idempotent; use backend-verify-job-run for a fresh run).
backend-verify-job-apply: backend-verify-job-infra
	./scripts/with_working_kube.sh apply -f k8s/jobs/omni-backend-verify.yaml

# Fresh Job run: infra → delete old Job → apply → wait/logs.
backend-verify-job-run: backend-verify-job-infra
	./scripts/with_working_kube.sh delete job omni-backend-verify -n multi-agent --ignore-not-found
	./scripts/with_working_kube.sh apply -f k8s/jobs/omni-backend-verify.yaml
	./scripts/with_working_kube.sh wait --for=condition=complete job/omni-backend-verify -n multi-agent --timeout=180s || true
	./scripts/with_working_kube.sh logs job/omni-backend-verify -n multi-agent --tail=200

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

# Split-role pods (prober/analyst/core/executor) consolidated into omni-fullstack
# (2026-06-03). deploy-worker now aliases deploy-fullstack — single pod role=full.
deploy-worker: deploy-fullstack

deploy-netpol:
	./scripts/with_working_kube.sh apply -f k8s/network-policies/default-deny-ingress.yaml
	./scripts/with_working_kube.sh apply -f k8s/network-policies/omni-gateway-netpol-ingress-multi-agent.yaml
	./scripts/with_working_kube.sh apply -f k8s/network-policies/omni-workers-netpol.yaml
	@echo "NetworkPolicy applied. Verify with: kubectl get netpol -n multi-agent"

# Ollama trên Mac — chỉ Service ExternalName → host.docker.internal:11434 (không Deployment trong cluster).
deploy-ollama:
	./scripts/with_working_kube.sh apply -f k8s/deployments/ollama-service.yaml

# Gateway FastAPI — image riêng `omni-gateway:latest` (chạy `make docker-gateway` trước khi rollout).
## Build image + deploy omni-gateway. PHẢI phụ thuộc docker-gateway: trước đây target
## này chỉ apply+restart nên luôn chạy lại image cũ — "rollout successful" mà code mới
## không có trong pod (đã gây misdeploy thật, xem commit fix /reports/playbooks).
deploy-gateway: docker-gateway ## Build image + deploy omni-gateway
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-gateway.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-gateway -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-gateway -n multi-agent --timeout=180s

rollback: ## Rollback Omni worker+gateway to previous revision
	./scripts/with_working_kube.sh rollout undo deployment/omni-fullstack -n $(NS)
	./scripts/with_working_kube.sh rollout undo deployment/omni-gateway -n $(NS)
	./scripts/with_working_kube.sh rollout status deployment/omni-fullstack -n $(NS) --timeout=120s

rollback-verify: ## Smoke test after rollback — CRAT pipeline only
	python3 scripts/verify_e2e_crat_pipeline.py --smoke-only

pre-deploy-validate: ## Validate all prerequisites before deploy
	NS=$(NS) bash scripts/pre-deploy-validate.sh

# Gom Ollama + Gateway (build gateway: `make docker-gateway`; worker không bắt buộc cho gateway).
deploy-services: deploy-ollama deploy-gateway


## Fullstack: single pod OMNI_WORKER_ROLE=full (analyst+prober+core+executor).
## Run `make docker-worker` first to bake source into image.
deploy-fullstack: docker-worker ## Build image + deploy omni-fullstack (single pod)
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-fullstack-rbac.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-fullstack.yaml
	./scripts/with_working_kube.sh rollout restart deployment/omni-fullstack -n multi-agent
	./scripts/with_working_kube.sh rollout status deployment/omni-fullstack -n multi-agent --timeout=180s

## ── Public plane (app.omnisre.xyz) ───────────────────────────────────────────
## Đồng bộ code local lên mặt public. Script tự build image RỒI so imageID của pod
## với image local — `rollout restart` một mình KHÔNG build gì (imagePullPolicy:
## IfNotPresent + tag :latest), nên "rollout successful" là tín hiệu giả.
## Mặc định KHÔNG đụng lab .local; dùng sync-public-all nếu muốn cả hai.
sync-public: ## Build + deploy UI và backend lên public plane
	bash scripts/sync_public_plane.sh

sync-public-ui: ## Chỉ Next.js shell → aoip-provider-web-public
	bash scripts/sync_public_plane.sh --ui

sync-public-backend: ## Chỉ FastAPI console → aoip-provider-portal-public
	bash scripts/sync_public_plane.sh --backend

sync-public-all: ## Đồng bộ CẢ public lẫn lab .local (blast radius rộng hơn)
	bash scripts/sync_public_plane.sh --with-lab

## Landing page → www.omnisre.xyz bằng Direct Upload. CỐ Ý không nối repo vào
## Cloudflare: repo private chứa manifest RBAC, tên topic Kafka, mẫu DSN và lịch sử
## pentest — nối vào chỉ để phục vụ 5 file tĩnh là mở đường truy cập thừa.
## Chỉ thư mục cloudflare/pages/ được đẩy lên. Đăng nhập lần đầu:
##   npx --yes wrangler@latest login
deploy-landing: ## Upload landing page lên Cloudflare Pages (không qua GitHub)
	bash scripts/deploy_landing.sh

## Bất biến sổ ca nằm ở TRIGGER Postgres, không nằm trong Python — pytest với fake
## pool vẫn xanh dù trigger bị drop. Target này chạy trên DB thật để bịt âm tính giả đó.
verify-case-ledger: ## Kiểm chứng sổ ca trên Postgres thật trong cluster
	bash scripts/verify_case_ledger.sh

deploy-kafka:
	./scripts/with_working_kube.sh apply -f k8s/kafka/kafka-single.yaml

# RBAC for the worker SA now lives in omni-fullstack-rbac.yaml (consolidated).
deploy-prober-rbac:
	./scripts/with_working_kube.sh apply -f k8s/deployments/omni-fullstack-rbac.yaml

ensure-kafka-topics:
	bash scripts/kafka_ensure_omni_topics.sh

rag-hot-sync:
	PYTHONPATH=src .venv/bin/python scripts/rag_hot_sync_worker.py

e2e-proactive:
	NS=multi-agent bash scripts/proactive_e2e.sh

e2e-incident-matrix:
	NS=multi-agent RBAC_NEGATIVE_NAMESPACE=kube-system bash scripts/e2e_incident_matrix.sh

# Portal E2E release gate — Playwright tests/e2e_portals lên provider/tenant portal
# thật (cần cluster, không chạy trong CI thuần).
e2e-portal:
	NS=$(NS) bash scripts/e2e_portal_release_gate.sh

product-release-gate:
	bash scripts/product_release_gate.sh

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

auto-execute-gate:
	PYTHONPATH=src .venv/bin/python -m pytest tests/test_auto_execute_gate.py tests/test_emit_execute_mutate_crat.py -q --tb=short

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

# WS0: dependency-direction contracts (.importlinter) — pkg/anomaly/rag must not
# import workers/, gateway must not import workers/. Run from src/ so root_packages
# resolve the same way pytest's pythonpath=src does.
lint-imports:
	cd src && ../.venv/bin/lint-imports --config ../.importlinter

secret-history-audit:
	docker run --rm -v "$$(pwd):/repo" zricethezav/gitleaks:v8.18.2 detect --source=/repo --config=/repo/.gitleaks.toml --report-path=/repo/leak_report_history.json --verbose

# Phase 5 gate: fail when autonomy verification regresses.
benchmark-advisory:
	@echo "==> Advisory schema gate (always blocking — no LLM needed)"
	.venv/bin/python -m pytest tests/benchmarks/test_advisory_schema.py -q --tb=short
	@echo "==> Advisory live LLM benchmark (informational — requires OMNI_OLLAMA_BASE_URL)"
	.venv/bin/python -m pytest tests/benchmarks/test_advisory_quality.py::test_benchmark_pass_rate -q --tb=short 2>&1 || true
	@echo "==> Tip: OMNI_OLLAMA_BASE_URL=http://localhost:11434 make benchmark-advisory"

coverage:  ## Run test coverage report
	.venv/bin/python -m pytest tests/ --ignore=tests/integration --ignore=tests/real_services --cov=src --cov-report=term-missing --cov-report=html:htmlcov -q

coverage-html:  ## Open HTML coverage report
	open htmlcov/index.html

coverage-gate:  ## Business scope coverage (see .coveragerc.gate); prints TOTAL — no fail until ≥90%
	.venv/bin/python -m pytest tests/ --ignore=tests/integration --ignore=tests/real_services --cov=src --cov-config=.coveragerc.gate --cov-report=term -q

coverage-gate-strict:  ## Same as coverage-gate but fails CI if TOTAL < 90% (non-product dirs only omitted; see .coveragerc.gate)
	.venv/bin/python -m pytest tests/ --ignore=tests/integration --ignore=tests/real_services --cov=src --cov-config=.coveragerc.gate --cov-report=term -q --cov-fail-under=90

coverage-waves:  ## Full src/ pytest+cov then W1/W2/W3 gap table + top files (see scripts/coverage_gap_report.py)
	.venv/bin/python scripts/coverage_gap_report.py --top 40

coverage-project-real:  ## This machine: Python gate cov + optional live Redis (OMNI_REDIS_URL) + smart-siem go test -cover
	bash scripts/coverage_project_real.sh

sbom:  ## Generate Software Bill of Materials (requires syft)
	@which syft > /dev/null 2>&1 && syft packages dir:. -o cyclonedx-json > omni-sbom.json && echo "SBOM written to omni-sbom.json" || echo "syft not installed — brew install anchore/syft/syft"

chaos-drill:  ## Run chaos drill on all lanes
	.venv/bin/python scripts/chaos_lane_drill.py --lane all

chaos-drill-dry:  ## Dry run chaos drill (no actual injection)
	.venv/bin/python scripts/chaos_lane_drill.py --lane all --dry-run

chaos-drill-rollback:  ## S1.2: inject bad ConfigMap → verify auto-rollback + CRAT event
	NS=$(NS) .venv/bin/python scripts/chaos_drill_rollback.py --namespace $(NS)

chaos-drill-rollback-dry:  ## S1.2: dry-run rollback drill (no actual injection)
	NS=$(NS) .venv/bin/python scripts/chaos_drill_rollback.py --namespace $(NS) --dry-run

chaos-drill-redis:  ## Inject Redis kill, verify CRAT fail-closed + recovery (lab only)
	NS=$(NS) OMNI_ENV_MODE=lab bash scripts/chaos/chaos_redis.sh

chaos-drill-kafka:  ## Block Kafka, verify graceful degradation + consumer lag recovery (lab only)
	NS=$(NS) OMNI_ENV_MODE=lab bash scripts/chaos/chaos_kafka.sh

chaos-drill-llm:  ## Kill LLM URL, verify degraded advisory mode + recovery (lab only)
	NS=$(NS) OMNI_ENV_MODE=lab bash scripts/chaos/chaos_llm.sh

chaos-drill-evidence-flood:  ## Flood 1000 fake evidences, verify sigma gate + lag recovery (lab only)
	OMNI_ENV_MODE=lab OMNI_AUTO_EXECUTE_ENABLED=false .venv/bin/python scripts/chaos/chaos_evidence_flood.py --count 1000

chaos-drill-pod-kill:  ## Kill omni-analyst pod, verify K8s restart + Kafka replay (lab only)
	NS=$(NS) OMNI_ENV_MODE=lab bash scripts/chaos/chaos_pod_kill.sh

chaos-drill-all:  ## Run all chaos drills sequentially — lab only, auto-restore after each
	@echo "==> Running all chaos drills (lab only)"
	$(MAKE) chaos-drill-redis
	$(MAKE) chaos-drill-kafka
	$(MAKE) chaos-drill-llm
	$(MAKE) chaos-drill-evidence-flood
	$(MAKE) chaos-drill-pod-kill
	@echo "==> All chaos drills complete"

asyncio-lint:
	@echo "==> Checking for time.sleep() in async functions"
	.venv/bin/python scripts/check_asyncio_sleep.py src/

autonomy-gate:
	$(MAKE) asyncio-lint
	$(MAKE) secret-gate
	.venv/bin/python scripts/validate_env_mode_gate.py
	.venv/bin/python scripts/validate_mutate_only_gate.py
	$(MAKE) auto-execute-gate
	.venv/bin/python scripts/validate_classifier_regression_gate.py
	.venv/bin/python scripts/validate_phase_docs_gate.py
	.venv/bin/python scripts/validate_nonimpact_guards_gate.py
	.venv/bin/python scripts/validate_learning_loop_gate.py
	.venv/bin/python -m pytest tests/test_autonomous_experience_gate.py tests/test_agentic_planner_early_exit.py tests/test_feedback_full_agentic_planner.py tests/test_deterministic_mutate_from_evidence.py tests/test_shadow_os_contract.py -q
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

# Teardown Omni Postgres cluster. Guarded: aborts if omni_admin schema (Admin
# config source-of-truth) is live unless FORCE_DATA_LOSS=1 is also passed.
# Dry-run by default; pass APPLY=1 to actually delete.
teardown-omni-postgres:
	@if [ "$(APPLY)" = "1" ]; then \
		if [ "$(FORCE_DATA_LOSS)" = "1" ]; then \
			./scripts/teardown_omni_postgres.sh --apply --force-data-loss; \
		else \
			./scripts/teardown_omni_postgres.sh --apply; \
		fi; \
	else \
		./scripts/teardown_omni_postgres.sh; \
	fi

# Backup/restore for omni-postgres (task #13). Daily pg_dump CronJob
# (k8s/deployments/omni-postgres-backup-cronjob.yaml) writes to a dedicated
# PVC. List available dumps, then restore via scripts/restore_omni_postgres.sh
# (dry-run by default; verify-mode restores into a throwaway DB, never omnidb,
# unless explicitly told to).
list-omni-postgres-backups:
	kubectl -n multi-agent run omni-pg-backup-list --rm -i --restart=Never --image=busybox:1.36 \
		--overrides='{"spec":{"containers":[{"name":"omni-pg-backup-list","image":"busybox:1.36","command":["ls","-la","/backup"],"volumeMounts":[{"name":"backup","mountPath":"/backup"}]}],"volumes":[{"name":"backup","persistentVolumeClaim":{"claimName":"omni-postgres-backup-data"}}]}}' \
		-- true

restore-omni-postgres-verify:
	@if [ -z "$(DUMP)" ]; then \
		echo "Usage: make restore-omni-postgres-verify DUMP=<dump-file> [APPLY=1]"; \
		echo "       (see: make list-omni-postgres-backups)"; \
		exit 1; \
	fi
	@if [ "$(APPLY)" = "1" ]; then \
		./scripts/restore_omni_postgres.sh --apply "$(DUMP)"; \
	else \
		./scripts/restore_omni_postgres.sh "$(DUMP)"; \
	fi
