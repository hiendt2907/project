# E2E cluster checklist (sau deploy worker / gateway)

Dùng sau `make deploy-worker` hoặc rollout tương đương — xác nhận không escalate oan khi deployment healthy.

## Chuẩn bị

1. Context kube đúng (OrbStack): `./scripts/with_working_kube.sh kubectl get ns`
2. Image đã build theo [`.cursor/rules/omni-cicd-k8s.mdc`](../../.cursor/rules/omni-cicd-k8s.mdc).

## Bước verify

1. **Pods:** `kubectl get pods -n multi-agent -l 'app in (omni-analyst,omni-prober,omni-core,omni-executor)'`
2. **Gateway (nếu đụng ingest):** `kubectl get pods -n multi-agent -l app=omni-gateway`
3. **Một E2E:** `bash scripts/gateway_alert_loki_verify.sh` (hoặc inject chaos tương đương trace đã thấy).
4. **Loki theo trace_id:** Grafana Explore Loki hoặc log query `{namespace="multi-agent"} |= "<trace_id>"`.
5. **Telegram suppress:** Khi `OMNI_TELEGRAM_SUPPRESS_WHEN_DEPLOYMENT_HEALTHY=true` và rollout deployment healthy, log không nên có `telegram_escalation_sent` cho cùng điều kiện “đã healthy” — grep analyst/core logs.

## Pass / fail

- **Pass:** Có ít nhất một trace end-to-end với transition hợp lý; không false Telegram khi suppress bật và deployment ready.
- **Fail:** Ghi `trace_id`, transition lệch, hoặc blocker cluster — không merge tới khi có post-mortem ngắn.
