"""Load E2E live-run configuration from JSON (cluster names, topics, fixture paths).

Python E2E scripts must not embed namespace/service/tenant/IP literals: set:

  E2E_LIVE_PROFILE_JSON — path (absolute or relative to repo root) to a profile file.

See ``scripts/fixtures/e2e_live_profile.example.json`` for the required shape.
"""

from __future__ import annotations

import json
import os
from typing import Any

REQUIRED_PROFILE_KEYS = (
    "gateway_receiver_name",
    "omni_namespace",
    "kafka_svc_name",
    "redis_ma_svc_name",
    "gateway_svc_name",
    "analyst_deploy_name",
    "siem_bridge_deploy_name",
    "kafka_topic_diagnostic_evidence",
    "kafka_topic_alerts",
    "kafka_topic_actions",
    "siem_redis_stream",
    "fg_redis_namespace_order",
    "fg_redis_pod_name",
    "fg_redis_auth_secret_name",
    "gateway_webhook_path",
    "baseline_snapshot_ttl_sec",
    "lane_pre_inject_sleep_sec",
    "log_markers_lane1",
    "log_markers_lane2",
    "log_markers_lane3",
    "log_markers_lane4",
    "trace_fallback_lane1",
    "trace_fallback_lane2",
    "siem_trace_prefix",
    "siem_incident_id_prefix",
    "siem_random_hex_len",
    "siem_trace_body_hex_len",
    "siem_category",
    "siem_severity",
    "siem_tenant_id",
    "siem_source",
    "siem_affected_ip",
    "siem_description",
    "siem_suggested_action",
    "siem_hitl_required",
    "siem_alert_rule",
    "siem_alert_hint",
    "siem_k8s_namespace",
    "lane1_deployment",
    "lane1_summary",
    "lane1_description",
    "lane2_deployment",
    "lane2_pod",
    "lane2_summary",
    "lane2_description",
    "lane3_symptom_group",
    "lane3_evidence_labels",
    "runbook_who_hints",
    "lane2_multi_signal_keywords",
    "lane1_log_anchors",
    "lane2_log_anchors",
    "lane3_log_anchors",
    "lane3_http_classify_expectations",
    "lane4_diag_substrings_required",
    "lane4_forecast_blob_substrings_required",
    "lane4_cluster_log_substrings",
    "lane4_require_incident_id_in_diag",
    "stream_tags_by_lane",
    "crat_phase1_ingress_timeout_sec",
    "crat_phase2_timeout_sec",
    "crat_phase3_timeout_sec",
    "crat_phase4_wait_sec",
    "kafka_consumer_auto_offset_reset",
    "paths",
)

REQUIRED_PATH_KEYS = (
    "lane1_gateway_alert_template",
    "lane2_gateway_alert_template",
    "lane3_evidence_batch_template",
    "baseline_snapshot",
    "siem_redis_xadd_row_template",
    "lane4_analyst_siem_evidence_batch_template",
)

PRIMARY_MARKER_SENTINEL = "$LANE1_PRIMARY_MARKER"


class E2EProfileError(RuntimeError):
    pass


def require_live_profile_env_path(repo_root: str) -> str:
    raw = (os.environ.get("E2E_LIVE_PROFILE_JSON") or "").strip()
    if not raw:
        raise E2EProfileError(
            "Set E2E_LIVE_PROFILE_JSON to your cluster profile JSON "
            "(copy from scripts/fixtures/e2e_live_profile.example.json)."
        )
    return raw if os.path.isabs(raw) else os.path.join(repo_root, raw)


def load_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_live_e2e_profile(repo_root: str) -> dict[str, Any]:
    path = require_live_profile_env_path(repo_root)
    prof: dict[str, Any] = load_json_file(path)
    if "comment" in prof:
        prof = {k: v for k, v in prof.items() if k != "comment"}
    missing = [k for k in REQUIRED_PROFILE_KEYS if k not in prof]
    if missing:
        raise E2EProfileError(f"Profile {path} missing keys: {missing}")
    p_paths = prof["paths"]
    if not isinstance(p_paths, dict):
        raise E2EProfileError("profile.paths must be an object")
    pm = [k for k in REQUIRED_PATH_KEYS if k not in p_paths]
    if pm:
        raise E2EProfileError(f"profile.paths missing: {pm}")
    resolved: dict[str, str] = {}
    for k, rel in p_paths.items():
        rp = rel if os.path.isabs(rel) else os.path.join(repo_root, rel)
        if not os.path.isfile(rp):
            raise E2EProfileError(f"Referenced file missing for paths.{k}: {rp}")
        resolved[k] = rp
    prof["_resolved_paths"] = resolved
    prof["_profile_path"] = path
    return prof


def resolved_path(profile: dict[str, Any], key: str) -> str:
    try:
        return profile["_resolved_paths"][key]
    except KeyError as e:
        raise E2EProfileError(f"Unknown resolved path key: {key}") from e


def substitute_placeholders(obj: Any, mapping: dict[str, str]) -> Any:
    """Recursively replace ``<<<KEY>>>`` in strings inside dict/list structures."""

    def one(s: str) -> str:
        out = s
        for k, v in mapping.items():
            out = out.replace(f"<<<{k}>>>", v)
        return out

    if isinstance(obj, str):
        return one(obj)
    if isinstance(obj, list):
        return [substitute_placeholders(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute_placeholders(v, mapping) for k, v in obj.items()}
    return obj


def resolve_lane1_anchors(anchors: dict[str, str], prof: dict[str, Any]) -> dict[str, str]:
    primary = prof["log_markers_lane1"][0]
    out: dict[str, str] = {}
    for k, v in anchors.items():
        out[k] = primary if v == PRIMARY_MARKER_SENTINEL else v
    return out
