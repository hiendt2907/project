import json
import os

def create_board(title, uid, panels):
    chaos_annotation = {
        "name": "Chaos Engine Injections",
        "datasource": {"type": "loki", "uid": "loki"},
        "enable": True,
        "hide": False,
        "iconColor": "rgba(255, 96, 96, 1)",
        "expr": '{namespace="multi-agent", pod=~".*omni-chaos.*"} |= "--- [KỊCH BẢN"',
        "target": {"limit": 100, "matchAny": False, "tags": [], "type": "tags"}
    }
    return {
        "annotations": {"list": [chaos_annotation]},
        "editable": True,
        "graphTooltip": 1,
        "refresh": "5s",
        "schemaVersion": 39,
        "tags": ["omni", "multi-agent", "chaos-ready"],
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": 1,
        "time": {"from": "now-15m", "to": "now"},
        "panels": panels
    }

def stat_panel(idx, title, expr, x, y, w=4, h=4, unit="short", color_mode="background", thresholds=None, ds="prometheus", decimals=0):
    if thresholds is None:
        thresholds = [{"color": "green", "value": None}]
    return {
        "type": "stat",
        "id": idx,
        "title": title,
        "datasource": {"type": ds, "uid": ds},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": color_mode,
            "graphMode": "none",
            "textMode": "value_and_name"
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": thresholds},
                "color": {"mode": "thresholds"}
            },
            "overrides": []
        },
        "targets": [{"datasource": {"type": ds, "uid": ds}, "expr": expr, "refId": "A", "instant": True} if ds == "prometheus" else {"datasource": {"type": "loki", "uid": "loki"}, "expr": expr, "refId": "A", "queryType": "instant"}]
    }

def ts_panel(idx, title, targets, x, y, w=12, h=8, unit="short", ds="prometheus", desc=""):
    return {
        "type": "timeseries",
        "id": idx,
        "title": title,
        "description": desc,
        "datasource": {"type": ds, "uid": ds},
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi"}
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "custom": {"lineWidth": 2, "fillOpacity": 15}
            },
            "overrides": []
        },
        "targets": [{"datasource": {"type": ds, "uid": ds}, "expr": t[0], "legendFormat": t[1], "refId": chr(65+i)} for i, t in enumerate(targets)]
    }

# --- DASHBOARD 1: WORKER & PIPELINE ---
panels_1 = [
    {"type": "row", "id": 1, "title": "1. Gateway & Message Pipeline", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}},
    stat_panel(11, "Gateway Requests (1m)", 'sum(rate(omni_gateway_requests_total[1m]))', 0, 1, 4, 4, unit="ops"),
    stat_panel(12, "Worker Processing Rate", 'sum(rate(omni_worker_messages_processed_total[1m]))', 4, 1, 4, 4, unit="ops", decimals=2),
    stat_panel(13, "Delayed Queue Backlog", 'omni_worker_lag_size', 8, 1, 4, 4, thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 100}, {"color": "red", "value": 1000}]),
    stat_panel(14, "Circuit Breaker Active", 'omni_circuit_breaker_active', 12, 1, 4, 4, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat_panel(15, "DLQ (Dead Letters in 5m)", 'sum(count_over_time({namespace="multi-agent", pod_name=~"omni-worker.*"} |= "events:dlq" [5m]))', 16, 1, 4, 4, ds="loki", thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    ts_panel(20, "Gateway & Worker Rates", [
        ('sum(rate(omni_gateway_requests_total[1m]))', 'Gateway Ingest (ops/s)'),
        ('sum(rate(omni_worker_messages_processed_total[1m]))', 'Worker Process (ops/s)')
    ], 0, 5, 12, 8, unit="ops"),
    ts_panel(21, "Processing Latency Profiles", [
        ('histogram_quantile(0.50, sum(rate(omni_worker_latency_seconds_bucket[5m])) by (le))', 'P50 (Median)'),
        ('histogram_quantile(0.95, sum(rate(omni_worker_latency_seconds_bucket[5m])) by (le))', 'P95'),
        ('histogram_quantile(0.99, sum(rate(omni_worker_latency_seconds_bucket[5m])) by (le))', 'P99')
    ], 12, 5, 12, 8, unit="s"),
    {"type": "row", "id": 3, "title": "2. Live Output Streams & Chaos Engine", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 13}},
    {
        "type": "logs", "id": 30, "title": "🔴 MISSION CONTROL: Chaos Engine Logs", "datasource": {"type": "loki", "uid": "loki"},
        "gridPos": {"h": 12, "w": 8, "x": 0, "y": 14},
        "options": {"showTime": True, "sortOrder": "Descending", "enableLogDetails": False},
        "targets": [{"datasource": {"type": "loki", "uid": "loki"}, "expr": '{namespace="multi-agent", pod_name=~"omni-chaos.*"}'}]
    },
    {
        "type": "logs", "id": 31, "title": "Worker Logs", "datasource": {"type": "loki", "uid": "loki"},
        "gridPos": {"h": 12, "w": 16, "x": 8, "y": 14},
        "options": {"showTime": True, "sortOrder": "Descending", "enableLogDetails": True},
        "targets": [{"datasource": {"type": "loki", "uid": "loki"}, "expr": '{namespace="multi-agent", pod_name=~"omni-worker.*"}'}]
    }
]

# --- DASHBOARD 2: AI & LLM (OLLAMA) ---
panels_2 = [
    {"type": "row", "id": 1, "title": "Local LLM Performance & AI Agents", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}},
    stat_panel(10, "Ollama Health Probe", 'omni_ollama_up', 0, 1, 4, 4, thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat_panel(11, "Agent Slots (Semaphore)", 'omni_ollama_semaphore_in_use', 4, 1, 4, 4, thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2}]),
    stat_panel(12, "LLM Timeouts & Rate Limits", 'sum(count_over_time({namespace="multi-agent", pod_name=~"omni-worker.*"} |~ "(?i)timeout|deadline|429|rate limit|context canceled|ollama.*(error|429)" [5m]))', 8, 1, 6, 4, ds="loki", thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    ts_panel(20, "Agent Task Concurrency (by Lane)", [('omni_ollama_semaphore_in_use', '{{lane}}')], 0, 5, 12, 8),
    ts_panel(21, "AI Faults Over Time", [('sum(count_over_time({namespace="multi-agent", pod_name=~"omni-worker.*"} |~ "(?i)timeout|deadline|429|rate limit|context canceled|ollama.*(error|429)" [1m]))', 'Errors / per min')], 12, 5, 12, 8, ds="loki"),
    {"type": "row", "id": 3, "title": "AI Diagnostics Traces", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 13}},
    {
        "type": "logs", "id": 30, "title": "LLM Inference Logs (Host/Docker bridging)", "datasource": {"type": "loki", "uid": "loki"},
        "gridPos": {"h": 12, "w": 24, "x": 0, "y": 14},
        "options": {"showTime": True, "sortOrder": "Descending"},
        "targets": [{"datasource": {"type": "loki", "uid": "loki"}, "expr": '{namespace="multi-agent"} |~ "(?i)ollama|llm|inference"'}]
    }
]

# --- DASHBOARD 3: INFRASTRUCTURE & DB ---
panels_3 = [
    {"type": "row", "id": 1, "title": "Datastore Resilience (Redis & Postgres/PGVector)", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}},
    stat_panel(10, "Redis Mesh Nodes", 'count(redis_connected_clients{app="redis-exporter"}) or vector(0)', 0, 1, 4, 4, thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 6}]),
    stat_panel(11, "Redis Cluster Memory", 'sum(redis_memory_used_bytes{app="redis-exporter"}) or vector(0)', 4, 1, 4, 4, unit="bytes", thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 500000000}]),
    stat_panel(12, "Redis Aggregate Ops/sec", 'sum(rate(redis_commands_processed_total{app="redis-exporter"}[1m])) or vector(0)', 8, 1, 4, 4, unit="ops", decimals=2),
    stat_panel(13, "PGPool deployment replicas", 'max(kube_deployment_status_replicas_available{namespace="multi-agent",deployment="pgpool-gateway"}) or vector(0)', 12, 1, 4, 4),
    stat_panel(14, "Postgres pods Ready", 'sum(kube_pod_status_ready{namespace="multi-agent",condition="true",pod=~"omni-postgres-.*"}) or vector(0)', 16, 1, 4, 4),
    stat_panel(15, "PGVector write fail rate", 'sum(rate(omni_learning_upserts_total{outcome="fail"}[5m])) or vector(0)', 20, 1, 4, 4, unit="ops", thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.01}]),
    ts_panel(20, "Redis Compute Cost Trend", [
        ('sum(rate(redis_commands_processed_total{app="redis-exporter"}[1m]))', 'Ops/s'),
        ('sum(redis_connected_clients{app="redis-exporter"})', 'Clients Connected')
    ], 0, 5, 12, 8),
    ts_panel(21, "PGVector learning write trend", [
        ('sum(increase(omni_learning_upserts_total{outcome="success"}[5m]))', 'action_experience write success (5m)'),
        ('sum(increase(omni_learning_upserts_total{outcome="fail"}[5m]))', 'action_experience write fail (5m)')
    ], 12, 5, 12, 8, unit="short"),
    {"type": "row", "id": 3, "title": "K8s Platform Anomaly Watch", "gridPos": {"h": 1, "w": 24, "x": 0, "y": 13}},
    stat_panel(30, "System CPU Z-Score", 'abs(omni:node_cpu:z)', 0, 14, 6, 4, decimals=2, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2}, {"color": "red", "value": 3}]),
    stat_panel(31, "System Mem Z-Score", 'abs(omni:mem:z)', 6, 14, 6, 4, decimals=2, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2}, {"color": "red", "value": 3}]),
    stat_panel(32, "System Disk Z-Score", 'abs(omni:node_disk:z)', 12, 14, 6, 4, decimals=2, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2}, {"color": "red", "value": 3}]),
    stat_panel(33, "System IOPS Z-Score", 'abs(omni:node_iops:z)', 18, 14, 6, 4, decimals=2, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2}, {"color": "red", "value": 3}]),
    stat_panel(34, "Kill Switch", 'omni_proactive_kill_switch', 0, 18, 6, 4, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
    stat_panel(35, "SRE Anomalies", 'increase(omni_anomaly_events_total[5m])', 6, 18, 6, 4),
    ts_panel(40, "Statistical Control Chart (3-Sigma Deviations)", [
        ('omni:node_cpu:z', 'CPU Z-Score'),
        ('omni:mem:z', 'MEM Z-Score'),
        ('omni:node_disk:z', 'Disk Z-Score'),
        ('omni:node_iops:z', 'IOPS Z-Score'),
        ('vector(3)', 'Upper Bound Warning'), ('vector(-3)', 'Lower Bound Warning')
    ], 12, 18, 12, 8)
]

db1 = create_board("🔄 [Omni] 1. Flow & Resiliency", "omni-flow-v1", panels_1)
db2 = create_board("🧠 [Omni] 2. Generative AI Subsystem", "omni-ai-v1", panels_2)
db3 = create_board("🏗️ [Omni] 3. Persistence & Infrastructure", "omni-infra-v1", panels_3)

os.makedirs("/Users/hiendang/project/k8s/dashboards/multi", exist_ok=True)
with open("/Users/hiendang/project/k8s/dashboards/multi/1-flow.json", "w") as f: json.dump(db1, f)
with open("/Users/hiendang/project/k8s/dashboards/multi/2-ai.json", "w") as f: json.dump(db2, f)
with open("/Users/hiendang/project/k8s/dashboards/multi/3-infra.json", "w") as f: json.dump(db3, f)

print("Generated 3 dashboards successfully!")
