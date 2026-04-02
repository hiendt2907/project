# Grafana Unified Alerting — Telegram

## Secret (required)

Grafana runs in namespace `monitor`; omni-worker’s Telegram credentials live in `multi-agent` as Secret `telegram-bot`. Kubernetes cannot mount a Secret across namespaces, so **`grafana-telegram-alerting` in `monitor` must contain the same `bot-token` and `chat-id` bytes**.

**Recommended:** copy from the working `telegram-bot` Secret:

```bash
./scripts/sync_grafana_telegram_secret.sh
./scripts/with_working_kube.sh rollout restart deployment/grafana -n monitor
```

The repo also ships a **placeholder** Secret in `k8s/monitor/grafana-telegram-alerting-secret.yaml` so Grafana can start before you sync; **replace** via sync or:

```bash
./scripts/with_working_kube.sh apply -f k8s/monitor/grafana-telegram-alerting-secret.yaml
```

Manual create (same keys as `telegram-bot`):

```bash
kubectl create secret generic grafana-telegram-alerting \
  -n monitor \
  --from-literal=bot-token='YOUR_BOT_TOKEN' \
  --from-literal=chat-id='YOUR_CHAT_ID' \
  --dry-run=client -o yaml | ./scripts/with_working_kube.sh apply -f -
```

Do not commit real tokens. The **initContainer** mounts the Secret as files under `/var/secrets/telegram/` and writes `contact_points.yaml` with YAML **double-quoted** `bottoken` / `chatid` (numeric chat IDs stay valid strings; avoids Grafana env interpolation quirks).

## Apply and restart Grafana

```bash
./scripts/with_working_kube.sh apply -f k8s/monitor/grafana-alerting-provisioning.yaml
./scripts/with_working_kube.sh apply -f k8s/monitor/grafana.yaml
./scripts/with_working_kube.sh rollout restart deployment/grafana -n monitor
./scripts/with_working_kube.sh rollout status deployment/grafana -n monitor --timeout=120s
```

## Verify

- **Alerting → Contact points**: `omni-telegram` is present.
- Use **Test** on the contact point to confirm Telegram delivery.

## Troubleshooting (Telegram)

- **401 Unauthorized** on **Test** or real notifications: Telegram rejects the **bot token** (wrong, revoked, or still the repo **placeholder**). Run `./scripts/sync_grafana_telegram_secret.sh` if omni-worker Telegram works, then **restart Grafana** so the initContainer re-renders `contact_points.yaml`. Confirm `kubectl get secret grafana-telegram-alerting -n monitor -o jsonpath='{.data.bot-token}' | base64 -d | wc -c` is non-trivial (token length). If you still use placeholder YAML, replace it.
- **400 Bad Request**: often **chat id** wrong (bot not added to group, or missing minus sign for group ids), or `parse_mode` / message format issues from Grafana’s Telegram integration.
- **Whitespace**: the initContainer strips CR/LF padding around `bot-token` and `chat-id` before writing `contact_points.yaml` (avoids stray characters from copy-paste).

## Prometheus `OmniBaseline*` vs Grafana rules

Prometheus rules in `k8s/monitor/prometheus.yaml` (`OmniBaselineCpuZHigh`, `OmniBaselineMemZHigh`, `OmniBaselineDiskZHigh`) fire in Prometheus; without Alertmanager they do not send Telegram. Grafana contact points apply to **Grafana Unified Alerting** (Grafana-managed rules or routes you configure).

To test end-to-end without duplicating PromQL in two systems: create a **temporary** Grafana alert rule in the UI (Prometheus datasource, same expression as an OmniBaseline rule) and select contact point `omni-telegram`. If you later add provisioned Grafana rules mirroring those PromQL expressions, consider reducing or disabling the `omni_baseline_alerts` group in Prometheus to avoid duplicate firing in UIs.
