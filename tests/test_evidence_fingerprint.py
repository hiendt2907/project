"""Tests for evidence_fingerprint.py — fingerprint stability and normalization."""

from __future__ import annotations

import pytest

from pkg.reasoning.evidence_fingerprint import (
    fingerprint_evidence,
    fingerprint_batch,
    normalize_content,
    pick_representative,
)


# ── normalize_content ─────────────────────────────────────────────────────

class TestNormalizeContent:
    def test_strips_ipv4(self):
        assert "IP" in normalize_content("connection refused 10.0.0.5:3306")

    def test_strips_ipv4_no_port(self):
        assert "IP" in normalize_content("host 192.168.1.1 unreachable")

    def test_strips_uuid(self):
        result = normalize_content("trace_id=a1b2c3d4-1234-5678-abcd-ef0123456789")
        assert "UUID" in result
        assert "a1b2c3d4" not in result

    def test_strips_pid(self):
        assert "PID" in normalize_content("oom kill pid=1234 process mysqld")

    def test_strips_pid_colon(self):
        assert "PID" in normalize_content("killed PID:5678 (mysqld)")

    def test_strips_iso_timestamp(self):
        result = normalize_content("2024-01-15T10:30:45.123Z error occurred")
        assert "TS" in result
        assert "2024" not in result

    def test_strips_metric_with_unit(self):
        result = normalize_content("memory usage 85MB threshold exceeded")
        assert "NUM" in result

    def test_strips_percent(self):
        result = normalize_content("cpu at 97% critical")
        assert "NUM" in result

    def test_strips_large_numbers(self):
        result = normalize_content("error count 12345 in last minute")
        assert "NUM" in result

    def test_preserves_keywords(self):
        result = normalize_content("oom kill: mysqld out of memory")
        assert "oom kill" in result
        assert "mysqld" in result
        assert "out of memory" in result

    def test_collapses_whitespace(self):
        result = normalize_content("error   happened   here")
        assert "  " not in result

    def test_lowercase(self):
        result = normalize_content("ERROR: Connection Refused")
        assert result == result.lower()


# ── fingerprint_evidence: same content, different variables → same fp ─────

class TestFingerprintStability:
    def test_different_pids_same_fingerprint(self):
        item1 = {"probe": "container_log_app", "alert_hint": "OOM kill pid=1234", "raw": ""}
        item2 = {"probe": "container_log_app", "alert_hint": "OOM kill pid=5678", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)

    def test_different_ips_same_fingerprint(self):
        item1 = {"probe": "network_check", "alert_hint": "connection refused 10.0.0.1:3306", "raw": ""}
        item2 = {"probe": "network_check", "alert_hint": "connection refused 172.16.0.5:3306", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)

    def test_different_timestamps_same_fingerprint(self):
        item1 = {"probe": "remote_log_errors", "alert_hint": "2024-01-15T10:30:45Z error in db", "raw": ""}
        item2 = {"probe": "remote_log_errors", "alert_hint": "2024-03-20T22:11:00Z error in db", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)

    def test_different_numbers_same_fingerprint(self):
        item1 = {"probe": "mysql_status", "alert_hint": "deadlock found after 5000ms wait", "raw": ""}
        item2 = {"probe": "mysql_status", "alert_hint": "deadlock found after 12000ms wait", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)

    def test_different_probe_different_fingerprint(self):
        item1 = {"probe": "container_log_app", "alert_hint": "OOM kill", "raw": ""}
        item2 = {"probe": "remote_system_metrics", "alert_hint": "OOM kill", "raw": ""}
        assert fingerprint_evidence(item1) != fingerprint_evidence(item2)

    def test_different_semantic_content_different_fingerprint(self):
        item1 = {"probe": "mysql_status", "alert_hint": "deadlock found", "raw": ""}
        item2 = {"probe": "mysql_status", "alert_hint": "max connections reached", "raw": ""}
        assert fingerprint_evidence(item1) != fingerprint_evidence(item2)

    def test_case_insensitive(self):
        item1 = {"probe": "remote_log_errors", "alert_hint": "OOM KILL: mysqld", "raw": ""}
        item2 = {"probe": "remote_log_errors", "alert_hint": "oom kill: mysqld", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)

    def test_fingerprint_format(self):
        fp = fingerprint_evidence({"probe": "test_probe", "alert_hint": "some error", "raw": ""})
        assert fp.startswith("test_probe:")
        parts = fp.split(":")
        assert len(parts) == 2
        assert len(parts[1]) == 12

    def test_empty_content_has_fingerprint(self):
        fp = fingerprint_evidence({"probe": "remote_system_metrics", "alert_hint": "", "raw": ""})
        assert fp.startswith("remote_system_metrics:")
        assert len(fp) > len("remote_system_metrics:")

    def test_missing_probe_uses_unknown(self):
        fp = fingerprint_evidence({"alert_hint": "some error", "raw": ""})
        assert fp.startswith("unknown:")

    def test_raw_content_included_in_fingerprint(self):
        item1 = {"probe": "container_log", "alert_hint": "error", "raw": ""}
        item2 = {"probe": "container_log", "alert_hint": "error", "raw": "out of memory"}
        # Different raw content → different fingerprint
        assert fingerprint_evidence(item1) != fingerprint_evidence(item2)

    def test_pod_name_stripped(self):
        item1 = {"probe": "container_log", "alert_hint": "pod/nginx-abc12 failed", "raw": ""}
        item2 = {"probe": "container_log", "alert_hint": "pod/nginx-xyz99 failed", "raw": ""}
        assert fingerprint_evidence(item1) == fingerprint_evidence(item2)


# ── fingerprint_batch ─────────────────────────────────────────────────────

class TestFingerprintBatch:
    def test_returns_list_same_length(self):
        items = [
            {"probe": "p1", "alert_hint": "error a", "raw": ""},
            {"probe": "p2", "alert_hint": "error b", "raw": ""},
        ]
        fps = fingerprint_batch(items)
        assert len(fps) == 2

    def test_empty_batch(self):
        assert fingerprint_batch([]) == []


# ── pick_representative ───────────────────────────────────────────────────

class TestPickRepresentative:
    def test_picks_richest_by_alert_hint(self):
        items = [
            {"probe": "p", "alert_hint": "short", "raw": ""},
            {"probe": "p", "alert_hint": "longer alert hint with more context", "raw": ""},
            {"probe": "p", "alert_hint": "medium hint", "raw": ""},
        ]
        rep = pick_representative(items)
        assert rep["alert_hint"] == "longer alert hint with more context"

    def test_picks_richest_by_raw(self):
        items = [
            {"probe": "p", "alert_hint": "", "raw": "short"},
            {"probe": "p", "alert_hint": "", "raw": "much longer raw content with details"},
        ]
        rep = pick_representative(items)
        assert rep["raw"] == "much longer raw content with details"

    def test_single_item(self):
        item = {"probe": "p", "alert_hint": "only item", "raw": ""}
        assert pick_representative([item]) == item

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            pick_representative([])
