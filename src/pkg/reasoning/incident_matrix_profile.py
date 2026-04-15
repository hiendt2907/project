"""Lookup workload_profile (e.g. api_web) and proof_lane from incident training matrix."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_MATRIX_CACHE: list[dict[str, Any]] | None = None

VALID_PROOF_LANES = frozenset({"resource", "state", "app_log"})


def _matrix_paths() -> list[Path]:
    raw = os.environ.get("MATRIX_PATHS") or str(_ROOT / "config" / "incident_training_matrix.yaml")
    parts: list[str] = []
    for sep in (":", ","):
        if sep in raw:
            parts = [p.strip() for p in raw.replace(sep, ",").split(",") if p.strip()]
            break
    if not parts:
        parts = [raw.strip()]
    out: list[Path] = []
    for p in parts:
        path = Path(p) if Path(p).is_absolute() else _ROOT / p
        out.append(path)
    return out


def merged_matrix_scenarios() -> list[dict[str, Any]]:
    global _MATRIX_CACHE
    if _MATRIX_CACHE is not None:
        return _MATRIX_CACHE
    rows: list[dict[str, Any]] = []
    for path in _matrix_paths():
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for row in data.get("scenarios") or []:
            if isinstance(row, dict) and str(row.get("id") or "").strip():
                rows.append(row)
    _MATRIX_CACHE = rows
    return rows


def invalidate_matrix_cache() -> None:
    """Test hook."""
    global _MATRIX_CACHE
    _MATRIX_CACHE = None


def alertname_from_batch(batch: list[dict[str, Any]]) -> str:
    """Best-effort alertname from canonical labels or alert_rule."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    an = str(labels.get("alertname") or "").strip()
                    if an:
                        return an
            except Exception:
                continue
        ar = str(b.get("alert_rule") or "").strip()
        if ar:
            return ar[:512]
    return ""


def workload_profile_for_alert(alertname: str) -> str | None:
    """
    Return workload_profile from matrix row when prometheus_alert matches alertname.
    """
    an = (alertname or "").strip()
    if not an:
        return None
    for row in merged_matrix_scenarios():
        pa = str(row.get("prometheus_alert") or "").strip()
        if pa and pa == an:
            wp = row.get("workload_profile")
            if isinstance(wp, str) and wp.strip():
                return wp.strip()
    return None


_RE_API_WEB_RAG = re.compile(
    r"(?i)\b(nginx|envoy|ingress|http/|https://|rest\s*api|api\s+gateway|status[\"']?\s*:\s*5\d\d)\b",
)


def rag_match_text_implies_api_web(rag_text: str | None) -> bool:
    """Conservative: RAG chunk text suggests HTTP/API surface (not mutate gate alone)."""
    t = (rag_text or "").strip()
    if len(t) < 12:
        return False
    return bool(_RE_API_WEB_RAG.search(t))


def is_api_web_workload(
    batch: list[dict[str, Any]],
    *,
    rag_match_text: str | None = None,
) -> bool:
    """
    True if Matrix marks api_web for this alertname, RAG text matches API/Web heuristics,
    OR the alertname itself signals an HTTP error-rate alert (app_log_heuristic).
    """
    an = alertname_from_batch(batch)
    wp = workload_profile_for_alert(an)
    if wp and wp.lower() == "api_web":
        return True
    if rag_match_text and rag_match_text_implies_api_web(rag_match_text):
        return True
    # Alertname-based: HTTP error-rate alerts are always api_web scope.
    if app_log_heuristic(batch):
        return True
    return False


def labels_from_batch(batch: list[dict[str, Any]]) -> dict[str, str]:
    """Merged labels from canonical_query_snippet JSON in evidence batch."""
    out: dict[str, str] = {}
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        lab = j.get("labels")
        if isinstance(lab, dict):
            for k, v in lab.items():
                if v is not None and str(v).strip():
                    out[str(k)] = str(v)
    return out


def row_matches_series_label_defaults(row: dict[str, Any], batch: list[dict[str, Any]]) -> bool:
    defaults = row.get("series_label_defaults")
    if not isinstance(defaults, dict) or not defaults:
        return True
    labels = labels_from_batch(batch)
    for k, v in defaults.items():
        if str(labels.get(k) or "").strip() != str(v).strip():
            return False
    return True


def rows_matching_prometheus_alert(alertname: str) -> list[dict[str, Any]]:
    an = (alertname or "").strip()
    if not an:
        return []
    return [r for r in merged_matrix_scenarios() if str(r.get("prometheus_alert") or "").strip() == an]


def pick_matrix_row_for_batch(
    batch: list[dict[str, Any]],
    *,
    rag_match_text: str | None = None,
) -> dict[str, Any] | None:
    """
    Select a single matrix row for this batch: series_label_defaults match first,
    then generic rows (disambiguate api_web when multiple).
    """
    an = alertname_from_batch(batch)
    rows = rows_matching_prometheus_alert(an)
    if not rows:
        return None
    for r in rows:
        d = r.get("series_label_defaults")
        if isinstance(d, dict) and d and row_matches_series_label_defaults(r, batch):
            return r
    generic = [
        r
        for r in rows
        if not (isinstance(r.get("series_label_defaults"), dict) and r["series_label_defaults"])
    ]
    if not generic:
        return None
    if len(generic) == 1:
        return generic[0]
    for r in generic:
        if str(r.get("workload_profile") or "").lower() == "api_web" and is_api_web_workload(
            batch, rag_match_text=rag_match_text
        ):
            return r
    return generic[0]


def proof_lane_from_annotation(batch: list[dict[str, Any]]) -> str | None:
    """Optional override via labels/annotations on the alert payload."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        for key in ("labels", "annotations"):
            d = j.get(key)
            if not isinstance(d, dict):
                continue
            for lk in ("omni_proof_lane", "omni.omni_proof_lane"):
                v = d.get(lk)
                if isinstance(v, str) and v.strip().lower() in VALID_PROOF_LANES:
                    return v.strip().lower()
    return None


_RE_STATE_LANE = re.compile(
    r"(createcontainerconfigerror|errimagepull|imagepullbackoff|crashloop|oomkilled|"
    r"failedmount|configmap|createcontainer|unschedul|pending|evicted)",
    re.I,
)


def state_lane_heuristic(batch: list[dict[str, Any]]) -> bool:
    """K8s deterministic failure signals — not metric-only noise."""
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_STATE_LANE.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    for k in ("reason", "alertname"):
                        if _RE_STATE_LANE.search(str(labels.get(k) or "")):
                            return True
            except Exception:
                pass
    return False


# Alertname / message patterns that indicate HTTP application error rate — route to app_log.
_RE_APP_LOG_LANE = re.compile(
    r"(?i)(HttpError|5xx|http[_\-]?error[_\-]?rate|nginx[_\-]?error|error[_\-]?rate|"
    r"log[_\-]?surge|sustained[_\s]+5\d\d|access[_\-]?log)",
)


def app_log_heuristic(batch: list[dict[str, Any]]) -> bool:
    """HTTP error-rate / log-surge signals that belong in the app_log lane."""
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_APP_LOG_LANE.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if snip.startswith("{"):
            try:
                j = json.loads(snip)
                labels = j.get("labels") if isinstance(j, dict) else None
                if isinstance(labels, dict):
                    for k in ("alertname",):
                        if _RE_APP_LOG_LANE.search(str(labels.get(k) or "")):
                            return True
            except Exception:
                pass
    return False


def resolve_proof_lane(
    batch: list[dict[str, Any]],
    *,
    rag_match_text: str | None = None,
    blind_lane_hint: str | None = None,
) -> tuple[str, str]:
    """
    Return (proof_lane, source): annotation > probe structured remediate > matrix row >
    blind hint > state heuristic > default resource.
    """
    pl = proof_lane_from_annotation(batch)
    if pl:
        return pl, "annotation"
    from pkg.reasoning.deterministic_mutate_from_evidence import (
        env_default_remediation_namespace,
        parse_probe_driven_mutate_tools_csv,
        probe_structured_remediation_ready,
    )

    # Before matrix/RAG defaults: probe FAILED + full mutate args → state lane (sigma bypass).
    if probe_structured_remediation_ready(
        batch,
        default_ns=env_default_remediation_namespace(),
        allowed_tools=parse_probe_driven_mutate_tools_csv(""),
    ):
        return "state", "probe_structured_remediate"
    row = pick_matrix_row_for_batch(batch, rag_match_text=rag_match_text)
    if row:
        lane = row.get("proof_lane")
        if isinstance(lane, str) and lane.strip().lower() in VALID_PROOF_LANES:
            resolved = lane.strip().lower()
            # State-lane heuristic signals deterministic K8s failures (CreateContainerConfigError,
            # CrashLoopBackOff, FailedMount, etc.).  When the matrix selected a resource-lane row
            # via a generic fallback (e.g., a training scenario that re-uses the same alertname),
            # these signals must override — they carry stronger evidence than a metric-only match.
            if resolved == "resource" and state_lane_heuristic(batch):
                return "state", "heuristic_override"
            return resolved, "matrix"
    bh = (blind_lane_hint or "").strip().lower()
    if bh in VALID_PROOF_LANES:
        return bh, "blind_hint"
    if state_lane_heuristic(batch):
        return "state", "heuristic"
    if app_log_heuristic(batch):
        return "app_log", "heuristic"
    return "resource", "default"


def expected_stage_for_batch(
    batch: list[dict[str, Any]],
    *,
    rag_match_text: str | None = None,
) -> str | None:
    row = pick_matrix_row_for_batch(batch, rag_match_text=rag_match_text)
    if not row:
        return None
    es = row.get("expected_stage")
    if isinstance(es, str) and es.strip():
        return es.strip()
    return None
