"""Unit tests for alert QoS ingress storm control (plan step 1)."""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from workers.alert_qos import (
    AdmissionDecision,
    AlertPriority,
    admit_alert,
    classify_alert_priority,
)


def _alert(*, source="prometheus", severity="warning", alertname="X", namespace="multi-agent", pod=""):
    labels = {"alertname": alertname, "severity": severity, "namespace": namespace}
    if pod:
        labels["pod"] = pod
    return {"source": source, "data": json.dumps({"alerts": [{"labels": labels}]})}


class TestClassify:
    def test_critical_severity_is_critical(self):
        assert classify_alert_priority(_alert(severity="critical")) is AlertPriority.CRITICAL

    def test_high_severity_is_critical(self):
        assert classify_alert_priority(_alert(severity="high")) is AlertPriority.CRITICAL

    def test_warning_severity_is_normal(self):
        assert classify_alert_priority(_alert(severity="warning")) is AlertPriority.NORMAL

    def test_siem_source_always_critical(self):
        assert classify_alert_priority(_alert(source="siem", severity="info")) is AlertPriority.CRITICAL

    def test_no_identity_is_malformed(self):
        p = {"source": "prometheus", "data": json.dumps({"alerts": [{"labels": {"severity": "warning"}}]})}
        assert classify_alert_priority(p) is AlertPriority.MALFORMED

    def test_unparseable_data_is_malformed(self):
        assert classify_alert_priority({"source": "prometheus", "data": "not-json{"}) is AlertPriority.MALFORMED

    def test_pod_only_identity_not_malformed(self):
        p = {"source": "prometheus", "data": json.dumps(
            {"alerts": [{"labels": {"pod": "nginx-x", "severity": "warning"}}]})}
        assert classify_alert_priority(p) is AlertPriority.NORMAL

    def test_non_alert_source_is_normal(self):
        assert classify_alert_priority({"source": "telegram"}) is AlertPriority.NORMAL


class TestAdmit:
    async def test_critical_never_shed_even_over_cap(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        for i in range(50):
            d = await admit_alert(r, AlertPriority.CRITICAL, now=1000.0, member=f"c{i}",
                                  normal_cap=2, window_sec=60)
            assert d is AdmissionDecision.ADMIT

    async def test_normal_capped_within_window(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        results = [
            await admit_alert(r, AlertPriority.NORMAL, now=1000.0, member=f"n{i}",
                              normal_cap=3, window_sec=60)
            for i in range(5)
        ]
        admitted = sum(1 for d in results if d is AdmissionDecision.ADMIT)
        assert admitted == 3
        assert results[3] is AdmissionDecision.SHED

    async def test_window_slides_old_entries_drop(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        for i in range(3):
            await admit_alert(r, AlertPriority.NORMAL, now=1000.0, member=f"a{i}",
                             normal_cap=3, window_sec=60)
        # 120s later the old window has slid out → admits again.
        d = await admit_alert(r, AlertPriority.NORMAL, now=1120.0, member="late",
                             normal_cap=3, window_sec=60)
        assert d is AdmissionDecision.ADMIT

    async def test_cap_zero_disables_shedding(self):
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        d = await admit_alert(r, AlertPriority.NORMAL, now=1.0, member="x",
                             normal_cap=0, window_sec=60)
        assert d is AdmissionDecision.ADMIT

    async def test_fail_open_on_redis_error(self):
        class BoomRedis:
            async def eval(self, *a, **k):
                raise RuntimeError("redis down")
        d = await admit_alert(BoomRedis(), AlertPriority.NORMAL, now=1.0, member="x",
                             normal_cap=1, window_sec=60)
        assert d is AdmissionDecision.ADMIT
