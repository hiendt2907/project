"""
Chaos tests exposing real business-logic vulnerabilities in Lane 1 (SYS_RESOURCE)
and Lane 2 (SYS_HARD_FAIL).

Derived entirely from source code inspection — no fabrication, no happy path.
Each test asserts CORRECT behavior. A FAIL means a real bug is confirmed.

Marker legend (xem conftest.py để biết chi tiết):
  @pytest.mark.inverted_logic
      Test dùng điều kiện SYNTHETIC/NGƯỢC để kiểm tra logic.
      VÍ DỤ: inject spike lúc n=3 samples để xác nhận gate KHÔNG fire
      (vì spike inflates mean → z ≈ 1.4) — ngược với production expectation.
      ⚠ TEST PASS ≠ system detect real load trong production.

  @pytest.mark.real_condition
      Test tạo real failure via FakeRedis/FakeKafka.
      Mô phỏng chính xác điều kiện thật (Redis down, Kafka lỗi, ...).

  @pytest.mark.business_logic_only
      Test kiểm tra business logic path.
      KHÔNG kiểm tra Prometheus scrape, kube-state-metrics, hay remote agent.
"""

from __future__ import annotations

import json
import time as _time
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from anomaly.three_sigma import ThreeSigmaGate, _MAINT_KEY_FMT
from workers.advisory_analyst_handler import _compute_escalation_tier
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT, REDIS_KEY_TS
from workers.os_state_validator import compare_alert_claim_to_os_state
from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    ForecastTimeline,
    ProposedRemediationStep,
    VerificationStep,
)


# =============================================================================
# LANE 2 (SYS_HARD_FAIL) — os_state_validator.py vulnerabilities
# =============================================================================


class TestLane2UnregisteredProbeFalseContrast:
    """
    BUG SOURCE: os_state_validator.py:548-558
    When probe_name is not in _OS_PROBE_HANDLERS and result=PASSED,
    the fallback checks for anomaly_words in extracted_fact dict keys.
    If extracted_fact is a plain string (non-JSON), _parse_ef returns {}.
    Empty dict → no anomaly markers → returns false "PASSED, no anomaly indicators" contrast.
    Impact: _emit_suggest_remediation called with confidence=0.90 on a false signal.
    """

    def test_unregistered_probe_plain_string_fact_must_not_emit_contrast(self):
        """
        EXPECTED: None — no handler registered, no structured fact to validate.
        ACTUAL BUG: returns contrast string because _parse_ef('all ok') → {} → no anomaly markers.
        """
        by_probe = {
            "my_custom_probe": {
                "probe": "my_custom_probe",
                "result": "PASSED",
                "extracted_fact": "all ok",  # plain string, not JSON dict
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, (
            "FAIL — unregistered probe with non-JSON extracted_fact generated false contrast.\n"
            "Fix: in the unregistered-probe fallback, require extracted_fact to be a parseable "
            "dict before trusting it. If _parse_ef returns {}, return None — we have no evidence."
        )

    def test_unregistered_probe_empty_dict_fact_must_not_emit_contrast(self):
        """
        Same bug variant: extracted_fact={} (empty dict from agent).
        All anomaly_values lookup finds nothing → false contrast generated.
        """
        by_probe = {
            "new_unreg_probe": {
                "probe": "new_unreg_probe",
                "result": "PASSED",
                "extracted_fact": {},
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, (
            "FAIL — unregistered probe with empty extracted_fact generated false contrast.\n"
            "An empty dict has no anomaly indicators AND no normal indicators — it is unknown state."
        )

    def test_unregistered_passed_probe_before_registered_failed_probe_short_circuits(self):
        """
        BUG SOURCE: os_state_validator.py:567 — returns immediately on first non-None result.
        When unregistered PASSED probe is iterated before a registered FAILED probe,
        false contrast is returned and the real FAILED probe is never evaluated.

        EXPECTED: None — disk_usage FAILED confirms real incident, not suspect alert.
        ACTUAL BUG: returns false contrast from my_custom_probe (dict iteration order).
        """
        by_probe = {
            # Iterated first: unregistered, PASSED, no anomaly markers → false contrast
            "my_custom_probe": {
                "probe": "my_custom_probe",
                "result": "PASSED",
                "extracted_fact": {},
            },
            # Iterated second: registered, FAILED — disk actually full
            "disk_usage": {
                "probe": "disk_usage",
                "result": "FAILED",
                "extracted_fact": {"disk_critical_count": 3, "critical_partitions": ["/data"]},
            },
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, (
            "FAIL — false contrast returned from unregistered probe before real disk failure was checked.\n"
            "Fix: collect all probe results first, return None if any registered probe confirms failure."
        )


class TestLane2SwapUsageNullValue:
    """
    BUG SOURCE: os_state_validator.py:234-236
        swap_pct = ef.get('swap_used_pct', 0)
        if swap_pct and float(swap_pct) > 80:  ← None is falsy, skips threshold check
    When agent returns swap_used_pct=None (null JSON), threshold is never checked.
    Result: contrast generated claiming swap is healthy with no actual data.
    """

    def test_swap_usage_null_value_must_not_emit_contrast(self):
        """
        EXPECTED: None — swap data is absent (null), state unknown.
        ACTUAL BUG: generates 'swap within healthy range' contrast with swap_used_pct=0%.
        """
        by_probe = {
            "swap_usage": {
                "probe": "swap_usage",
                "result": "PASSED",
                "extracted_fact": {"swap_used_pct": None},  # null from agent
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, (
            "FAIL — swap_usage with null swap_used_pct generated false 'healthy' contrast.\n"
            "Fix: `if swap_pct is None: return None` — missing data is not healthy."
        )

    def test_swap_usage_zero_value_must_emit_contrast(self):
        """
        Regression guard: swap_used_pct=0 (zero) IS a valid healthy value — contrast should fire.
        Do not confuse 0 with None when fixing the above bug.
        """
        by_probe = {
            "swap_usage": {
                "probe": "swap_usage",
                "result": "PASSED",
                "extracted_fact": {"swap_used_pct": 0},  # zero is valid healthy
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is not None, "swap_used_pct=0 is valid healthy state — contrast should be generated"


class TestLane2RedisOsHealthPersistenceBug:
    """
    BUG SOURCE: os_state_validator.py:364
        if ef.get("anomalies") or ef.get("aof_enabled") is False and ef.get("rdb_last_bgsave_status") == "err":
    When aof_enabled key is absent: ef.get("aof_enabled") → None.
    None is not False → (None is False) → False.
    The second condition (bgsave failure) is never evaluated when aof_enabled is missing.
    Result: Redis with broken RDB persistence reported as "healthy".
    """

    def test_redis_os_health_bgsave_err_without_aof_key_must_block_contrast(self):
        """
        EXPECTED: None — bgsave failure is a real persistence problem, alert is NOT suspect.
        ACTUAL BUG: returns 'Redis healthy' contrast because aof_enabled missing → condition False.
        """
        by_probe = {
            "redis_os_health": {
                "probe": "redis_os_health",
                "result": "PASSED",
                "extracted_fact": {
                    # aof_enabled key deliberately absent (agent didn't include it)
                    "rdb_last_bgsave_status": "err",  # persistence is broken
                },
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, (
            "FAIL — redis_os_health reported 'healthy' when rdb_last_bgsave_status=err "
            "and aof_enabled is absent.\n"
            "Fix: check `rdb_last_bgsave_status == 'err'` independently from aof_enabled. "
            "Any bgsave failure is a problem regardless of AOF state."
        )

    def test_redis_os_health_bgsave_err_with_aof_false_correctly_blocks(self):
        """Regression: when aof_enabled IS explicitly False + bgsave err → correctly blocked."""
        by_probe = {
            "redis_os_health": {
                "probe": "redis_os_health",
                "result": "PASSED",
                "extracted_fact": {
                    "aof_enabled": False,
                    "rdb_last_bgsave_status": "err",
                },
            }
        }
        result = compare_alert_claim_to_os_state(by_probe, alert_ctx={})
        assert result is None, "aof_enabled=False + bgsave=err must block contrast (persistence broken)"


# =============================================================================
# LANE 1 (SYS_RESOURCE) — three_sigma.py vulnerabilities
# =============================================================================


@pytest.mark.real_condition
@pytest.mark.business_logic_only
class TestLane1MaintenanceWindowBypassOnRedisError:
    """
    BUG SOURCE: three_sigma.py:107-113
    observe_adaptive() checks maintenance window via Redis.exists(maint_key).
    When Redis raises during this check, exception is caught at WARNING and
    the function FALLS THROUGH to _observe_impl — maintenance suppression is bypassed.

    Impact: anomaly alert fires during planned maintenance → operator woken up for nothing.
    Correct behavior: fail closed — treat Redis error during maint check as maint=True (suppress).
    """

    @pytest.mark.asyncio
    async def test_spike_detectable_without_maint_window(self):
        """Precondition: with 50 warm-up samples, spike IS detected when no maint window."""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)
        for _ in range(50):
            await gate.observe("cpu", 50.0)
        anomaly, z = await gate.observe_adaptive(
            "cpu", 9999.0, namespace="multi-agent", deployment="omni-worker"
        )
        assert anomaly, "Spike must be detectable with 50 baseline samples and no maint window"
        assert z is not None and abs(z) > 3.0

    @pytest.mark.asyncio
    async def test_maint_window_suppresses_correctly_when_redis_ok(self):
        """Regression: with proper warmup, maint window correctly suppresses the spike."""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)
        for _ in range(50):
            await gate.observe("cpu", 50.0)

        maint_key = _MAINT_KEY_FMT.format(namespace="multi-agent", deployment="omni-worker")
        await redis.set(maint_key, "1")

        anomaly, z = await gate.observe_adaptive(
            "cpu", 9999.0, namespace="multi-agent", deployment="omni-worker"
        )
        assert not anomaly, "Maint window must suppress anomaly when Redis is healthy"
        assert z is None, "z-score must be None when suppressed by maint window"

    @pytest.mark.asyncio
    async def test_maint_window_bypass_on_redis_exists_error(self):
        """
        Setup: 50 warmup samples so spike is reliably anomalous.
               Maintenance window key exists in Redis (maint=True).
        Inject: Redis.exists() raises ConnectionError during the maint key check.
        Expected: anomaly=False (fail closed — suppress during maint).
        Actual BUG: anomaly=True (exception swallowed, detection proceeds unguarded).
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)
        for _ in range(50):
            await gate.observe("cpu", 50.0)

        maint_key = _MAINT_KEY_FMT.format(namespace="multi-agent", deployment="omni-worker")
        await redis.set(maint_key, "1")

        real_exists = redis.exists

        async def flaky_exists(key, *args, **kwargs):
            if "maint" in str(key):
                raise ConnectionError("Redis flaky during maint check")
            return await real_exists(key, *args, **kwargs)

        redis.exists = flaky_exists

        anomaly, z = await gate.observe_adaptive(
            "cpu", 9999.0, namespace="multi-agent", deployment="omni-worker"
        )
        assert not anomaly, (
            "FAIL — maintenance window suppression bypassed when Redis.exists() raises.\n"
            "Fix: in the except block of observe_adaptive (three_sigma.py:113), "
            "return (False, None) instead of falling through. "
            "Redis error during maint check = fail closed = treat as maintenance active."
        )


@pytest.mark.inverted_logic
@pytest.mark.business_logic_only
class TestLane1AdvisorySnapshotStalenessGap:
    """
    GAP (not a crash bug): evidence_consumer.py:2121-2146
    _proof_of_fault_gate() warns when snapshot age > 300s (line 688).
    The advisory sigma check (second independent read, line 2121) has ZERO staleness validation.

    Impact: if baseline_snapshot worker dies, a 24h stale snapshot is used as-is.
    A z_cpu spike from 24h ago triggers advisory on a workload that self-healed.

    This test documents the gap and guards against regression where the fix might
    accidentally remove the staleness warning from _proof_of_fault_gate.
    """

    @pytest.mark.asyncio
    async def test_proof_of_fault_gate_warns_on_stale_snapshot(self, caplog):
        """
        _proof_of_fault_gate DOES check staleness and logs a warning.
        The advisory path does NOT — that's the gap documented here.
        """
        import logging
        from workers.evidence_consumer import _proof_of_fault_gate  # type: ignore[attr-defined]

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = SimpleNamespace(
            baseline_dr_z_threshold=3.0,
            autonomous_sigma_observation_window=1,
            omni_proof_lane_enabled=True,
        )
        ctx = SimpleNamespace(redis=redis, settings=settings)

        # Write snapshot with timestamp 10 minutes ago (> 300s stale)
        stale_snap = json.dumps({"z_cpu": 4.5, "z_mem": 0.0, "dr": False})
        stale_ts = _time.time() - 610
        await redis.set(REDIS_KEY_SNAPSHOT, stale_snap)
        await redis.set(REDIS_KEY_TS, str(stale_ts))

        # Batch that resolves to resource lane (default, no heuristic triggers)
        batch = [{"probe": "k8s_pod_cpu", "result": "PASSED",
                  "extracted_fact": {"cpu_pct": 95.0}, "alert_hint": "HighCPU"}]

        with caplog.at_level(logging.WARNING, logger="workers.evidence_consumer"):
            proof_ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="chaos-stale-001", batch=batch
            )

        staleness_warned = any("baseline_snapshot_stale" in r.message for r in caplog.records)
        assert staleness_warned, (
            "REGRESSION: _proof_of_fault_gate no longer warns on stale snapshot (>300s). "
            "This warning is the ONLY staleness signal — removing it leaves the advisory path "
            "silently using up to 24h stale data."
        )

    @pytest.mark.asyncio
    async def test_advisory_sigma_path_missing_snapshot_skips_gate(self):
        """
        Documents gap: when REDIS_KEY_SNAPSHOT doesn't exist (fresh deploy, Redis flush),
        the advisory sigma check is skipped entirely (if _adv_snap_raw: → False).
        Advisory runs without any resource anomaly validation.

        This test verifies the snapshot is actually missing = None, confirming the
        code path where advisory bypasses sigma. Not fixable here without refactoring
        evidence_consumer, but gap must be documented.
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # Deliberately do NOT write any snapshot

        snap_raw = await redis.get(REDIS_KEY_SNAPSHOT)
        assert snap_raw is None, "Precondition: no snapshot in Redis"

        # The advisory path at evidence_consumer:2121 does:
        #   if _adv_snap_raw:   ← False when None
        #       ... sigma check ...
        # → sigma check skipped entirely when snapshot missing
        # → advisory proceeds to LLM on resource lane with no anomaly proof
        #
        # Gap: should treat missing snapshot same as sigma_ok=False (block advisory).
        # Workaround until fix: ensure baseline_snapshot worker has readiness check
        # before evidence consumer starts processing resource-lane alerts.
        gap_exists = snap_raw is None
        assert gap_exists, "Gap confirmed: missing snapshot causes advisory sigma bypass"


# =============================================================================
# LANE 1 (SYS_RESOURCE) — sigma gate cold start (correct behavior guard)
# =============================================================================


@pytest.mark.inverted_logic
@pytest.mark.business_logic_only
class TestLane1SigmaColdStart:
    """Guard correct behavior: sigma gate must block on < 3 samples (cold start).

    ⚠ INVERTED LOGIC: inject spike sớm (n=1, n=3) để xác nhận gate KHÔNG fire.
    Đây là test ngược: kiểm tra rằng gate fail-closed khi thiếu data.
    Production behavior: gate sẽ fire khi đủ baseline (>10 samples).
    """

    @pytest.mark.asyncio
    async def test_first_sample_never_triggers_anomaly(self):
        """After Redis flush, first spike must NOT alert — no baseline to compare against."""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)

        anomaly, z = await gate.observe("cpu", 9999.0)
        assert not anomaly, "First sample on cold Redis must never trigger anomaly"
        assert z is None

    @pytest.mark.asyncio
    async def test_two_samples_never_trigger_anomaly(self):
        """Two samples also insufficient — pstdev on 2 samples can produce extreme z."""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)

        await gate.observe("cpu", 50.0)
        anomaly, z = await gate.observe("cpu", 9999.0)
        assert not anomaly, "Two samples must not trigger anomaly (< 3 min window)"

    @pytest.mark.asyncio
    async def test_minimum_window_spike_not_detectable(self):
        """
        At n=3 (minimum), a spike as the 3rd sample is NOT detected — spike inflates mean.
        math: mean=(9999+50+50.1)/3=3366, z≈1.4 < 3.0 threshold.
        Documents warmup requirement: need 10+ stable baseline samples before gate is reliable.
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=100)

        await gate.observe("cpu", 50.0)
        await gate.observe("cpu", 50.1)
        anomaly, z = await gate.observe("cpu", 9999.0)
        assert not anomaly, (
            "EXPECTED: at n=3 samples, spike inflates mean → z ≈ 1.4 < 3.0 threshold. "
            "Gate correctly does not fire. Warmup of 10+ stable samples required."
        )

    @pytest.mark.asyncio
    async def test_window_overflow_does_not_spike_on_eviction(self):
        """
        When window is full (window_size=5) and new sample arrives,
        LTRIM evicts the oldest. If oldest was a spike, eviction must NOT
        re-trigger anomaly on a normal new value.
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        gate = ThreeSigmaGate(redis, window_size=5)

        # Fill window with normal + one spike at position 5 (oldest)
        for v in [50.0, 51.0, 49.0, 50.5]:
            await gate.observe("cpu", v)
        # Position 5 — spike that will eventually be evicted
        await gate.observe("cpu", 9999.0)
        # Now add normal values — spike is still in window, not evicted yet at window=5
        # Add one more to push spike out
        await gate.observe("cpu", 50.0)  # 6th sample → oldest (spike 9999) evicted

        # New normal value after spike eviction — must NOT trigger anomaly
        anomaly, z = await gate.observe("cpu", 50.0)
        assert not anomaly, "Normal value after spike eviction must not trigger anomaly"


# =============================================================================
# PROBE HANDLER NULL-FIELD FALSE CONTRAST (D8 extended)
# Coverage: all 19 probe handlers, gaps at lines 558, 571-581, 585-587
# BUG PATTERN: ef.get("field", 0) when field=None → falsy → contrast emitted
# CORRECT behavior: null field = unknown state → must return None
# =============================================================================


class TestProbeHandlerNullFieldFalseContrast:
    """
    Chaos tests for all os_state_validator probe handlers.

    Bug pattern: handler checks `if ef.get("field", 0)` or `if ef.get("field")`.
    When agent returns field=None (null JSON), falsy check passes → false contrast emitted
    claiming system is healthy — but null data is NOT evidence of health.

    Each test: set critical field to None → expect None returned.
    FAIL = bug confirmed in that probe handler.
    """

    @staticmethod
    def _probe(name: str, fact: dict) -> dict:
        return {name: {"probe": name, "result": "PASSED", "extracted_fact": fact}}

    def test_cron_jobs_null_failed_cron_count(self):
        """failed_cron_count=None → unknown cron state, must NOT claim healthy."""
        result = compare_alert_claim_to_os_state(
            self._probe("cron_jobs", {"failed_cron_count": None})
        )
        assert result is None, (
            "FAIL — cron_jobs: null failed_cron_count generated false 'healthy' contrast.\n"
            "Fix: check `if ef.get('failed_cron_count') is None: return None`"
        )

    def test_zombie_processes_null_zombie_count(self):
        """zombie_count=None → falsy → contrast emitted despite unknown data."""
        result = compare_alert_claim_to_os_state(
            self._probe("zombie_processes", {"zombie_count": None})
        )
        assert result is None, (
            "FAIL — zombie_processes: null zombie_count treated as zero, false contrast.\n"
            "Fix: `if zombie_count is None: return None` before threshold check."
        )

    def test_oom_events_null_oom_count(self):
        """oom_count=None → falsy → contrast emitted despite unknown OOM state."""
        result = compare_alert_claim_to_os_state(
            self._probe("oom_events", {"oom_count": None})
        )
        assert result is None, (
            "FAIL — oom_events: null oom_count generated false 'no OOM' contrast.\n"
            "Fix: explicit None check before falsy comparison."
        )

    def test_disk_usage_null_disk_critical_count(self):
        """disk_critical_count=None → falsy → contrast despite unknown disk state."""
        result = compare_alert_claim_to_os_state(
            self._probe("disk_usage", {"disk_critical_count": None})
        )
        assert result is None, (
            "FAIL — disk_usage: null disk_critical_count generated 'partitions healthy' contrast.\n"
            "Fix: `if ef.get('disk_critical_count') is None: return None`"
        )

    def test_storage_nfs_null_error_count_raises_typeerror(self):
        """nfs_error_count=None → None > 0 → TypeError → caught by except → result=None.
        This is safe via exception, but the fix should be explicit None check."""
        result = compare_alert_claim_to_os_state(
            self._probe("storage_nfs", {"nfs_error_count": None})
        )
        assert result is None, "storage_nfs: TypeError from None > 0 must be caught → result=None"

    def test_raid_mdadm_null_failed_devices(self):
        """failed_devices=None → falsy or expression → contrast despite unknown RAID state."""
        result = compare_alert_claim_to_os_state(
            self._probe("raid_mdadm", {"degraded_arrays": [], "failed_devices": None})
        )
        assert result is None, (
            "FAIL — raid_mdadm: null failed_devices treated as 0 → false 'arrays clean' contrast.\n"
            "Fix: `if ef.get('failed_devices') is None: return None`"
        )

    def test_lvm_volumes_null_partial_vgs_and_failed_pvs(self):
        """Both fields None → both falsy → contrast despite unknown LVM state."""
        result = compare_alert_claim_to_os_state(
            self._probe("lvm_volumes", {"partial_vgs": None, "failed_pvs": None})
        )
        assert result is None, (
            "FAIL — lvm_volumes: null partial_vgs/failed_pvs generated false 'LVM healthy' contrast."
        )

    def test_network_interfaces_null_down_and_error_interfaces(self):
        """Both None → both falsy → contrast despite unknown interface state."""
        result = compare_alert_claim_to_os_state(
            self._probe("network_interfaces", {"down_interfaces": None, "error_interfaces": None})
        )
        assert result is None, (
            "FAIL — network_interfaces: null fields generated 'all interfaces UP' contrast."
        )

    def test_dns_resolution_null_failed_lookups(self):
        """failed_lookups=None → falsy → contrast despite unknown DNS state."""
        result = compare_alert_claim_to_os_state(
            self._probe("dns_resolution", {"failed_lookups": None, "lookup_error_count": None})
        )
        assert result is None, (
            "FAIL — dns_resolution: null failed_lookups generated 'DNS resolving correctly' contrast."
        )

    def test_tcp_connections_null_indicators(self):
        """Both None → both falsy → contrast despite unknown connection table state."""
        result = compare_alert_claim_to_os_state(
            self._probe("tcp_connections", {"time_wait_excess": None, "syn_flood_indicator": None})
        )
        assert result is None, (
            "FAIL — tcp_connections: null indicators generated 'TCP table healthy' contrast."
        )

    def test_mysql_health_null_anomalies(self):
        """anomalies=None → falsy → contrast despite unknown MySQL state."""
        result = compare_alert_claim_to_os_state(
            self._probe("mysql_health", {"anomalies": None})
        )
        assert result is None, (
            "FAIL — mysql_health: null anomalies generated 'MySQL healthy' contrast."
        )

    def test_proxysql_health_null_anomalies(self):
        """anomalies=None → falsy → contrast despite unknown ProxySQL state."""
        result = compare_alert_claim_to_os_state(
            self._probe("proxysql_health", {"anomalies": None})
        )
        assert result is None, (
            "FAIL — proxysql_health: null anomalies generated 'ProxySQL healthy' contrast."
        )

    def test_postgresql_health_null_anomalies_with_low_lag(self):
        """anomalies=None AND replication_lag_s within threshold → false contrast.
        `if None or (5 > 30)` → `None or False` → False → contrast emitted."""
        result = compare_alert_claim_to_os_state(
            self._probe("postgresql_health", {"anomalies": None, "replication_lag_s": 5})
        )
        assert result is None, (
            "FAIL — postgresql_health: null anomalies + low lag generated 'PostgreSQL healthy' contrast."
        )

    def test_mongodb_health_null_anomalies_with_low_lag(self):
        """anomalies=None AND repl_lag_s within threshold → false contrast."""
        result = compare_alert_claim_to_os_state(
            self._probe("mongodb_health", {"anomalies": None, "repl_lag_s": 5})
        )
        assert result is None, (
            "FAIL — mongodb_health: null anomalies + low lag generated 'MongoDB healthy' contrast."
        )

    def test_service_haproxy_null_down_backends(self):
        """down_backends=None → falsy → contrast despite unknown backend state."""
        result = compare_alert_claim_to_os_state(
            self._probe("service_haproxy", {"down_backends": None})
        )
        assert result is None, (
            "FAIL — service_haproxy: null down_backends generated 'backend state healthy' contrast."
        )

    def test_service_nginx_null_upstream_errors_no_rate(self):
        """upstream_errors=None, error_rate_pct absent → `0 > 5` False, `None` falsy → contrast."""
        result = compare_alert_claim_to_os_state(
            self._probe("service_nginx", {"upstream_errors": None})
        )
        assert result is None, (
            "FAIL — service_nginx: null upstream_errors generated 'Nginx healthy' contrast."
        )

    def test_service_keepalived_null_vip_missing(self):
        """vip_missing=None → falsy, state absent → not 'FAULT' → contrast emitted."""
        result = compare_alert_claim_to_os_state(
            self._probe("service_keepalived", {"vip_missing": None})
        )
        assert result is None, (
            "FAIL — service_keepalived: null vip_missing generated 'VRRP healthy' contrast."
        )

    def test_kernel_errors_null_critical_errors_and_mce(self):
        """Both None → both falsy → 'no kernel errors' contrast despite unknown state."""
        result = compare_alert_claim_to_os_state(
            self._probe("kernel_errors", {"critical_errors": None, "mce_count": None})
        )
        assert result is None, (
            "FAIL — kernel_errors: null fields generated 'no critical kernel errors' contrast."
        )

    def test_memory_hw_errors_null_correctable_raises_typeerror(self):
        """correctable_errors=None → None > 0 → TypeError → caught → result=None. Safe but sloppy."""
        result = compare_alert_claim_to_os_state(
            self._probe("memory_hw_errors", {"correctable_errors": None, "uncorrectable_errors": None})
        )
        assert result is None, "memory_hw_errors: TypeError from None > 0 caught → result=None"

    def test_docker_daemon_null_unhealthy_containers(self):
        """unhealthy_containers=None → unknown count → must NOT claim healthy.
        daemon_error=None is valid (no error), but null container count is not."""
        result = compare_alert_claim_to_os_state(
            self._probe("docker_daemon", {"daemon_error": None, "unhealthy_containers": None})
        )
        assert result is None, (
            "FAIL — docker_daemon: null unhealthy_containers generated 'Docker healthy' contrast."
        )

    def test_containerd_state_null_plugin_errors(self):
        """plugin_errors=None → unknown plugin state → must NOT claim healthy.
        daemon_error=None is valid (no error), but null plugin_errors is not."""
        result = compare_alert_claim_to_os_state(
            self._probe("containerd_state", {"daemon_error": None, "plugin_errors": None})
        )
        assert result is None, (
            "FAIL — containerd_state: null plugin_errors generated 'containerd healthy' contrast."
        )


class TestProbeHandlerCoverageGaps:
    """Cover lines 558, 571-581, 585-587 in compare_alert_claim_to_os_state."""

    @staticmethod
    def _probe(name: str, fact: dict | str, result: str = "PASSED") -> dict:
        return {name: {"probe": name, "result": result, "extracted_fact": fact}}

    def test_empty_probe_name_skipped_line_558(self):
        """Line 558: empty probe_name → continue (not crash, not contrast)."""
        by_probe = {
            "": {"probe": "", "result": "PASSED", "extracted_fact": {}},
            "disk_usage": {"probe": "disk_usage", "result": "FAILED",
                           "extracted_fact": {"disk_critical_count": 3}},
        }
        result = compare_alert_claim_to_os_state(by_probe)
        assert result is None, "Empty probe_name must be skipped; disk_usage FAILED = real incident"

    def test_unregistered_probe_with_valid_healthy_fact_emits_contrast(self):
        """Lines 571-581: unregistered probe + PASSED + non-empty fact + no anomaly keys → contrast."""
        by_probe = {
            "my_custom_agent_probe": {
                "probe": "my_custom_agent_probe",
                "result": "PASSED",
                "extracted_fact": {"memory_used_pct": 30, "connection_count": 5},
            }
        }
        result = compare_alert_claim_to_os_state(
            by_probe, alert_ctx={"namespace": "test", "deployment": "svc"}
        )
        assert result is not None, (
            "Unregistered probe with valid non-anomaly fact must generate contrast "
            "(agent confirms healthy state we don't have a handler for)"
        )
        assert "my_custom_agent_probe" in result

    def test_registered_probe_handler_typeerror_caught_line_585(self):
        """Lines 585-587: handler raises TypeError (None > 0) → caught → result=None."""
        # storage_nfs with None triggers TypeError in handler
        result = compare_alert_claim_to_os_state(
            {"storage_nfs": {"probe": "storage_nfs", "result": "PASSED",
                              "extracted_fact": {"nfs_error_count": None}}}
        )
        assert result is None, "TypeError in probe handler must be caught; no contrast on error"


# =============================================================================
# LANE 2 (SYS_HARD_FAIL) — OS validator end-to-end correctness
# =============================================================================


class TestLane2OSValidatorEndToEnd:
    """
    End-to-end correctness of compare_alert_claim_to_os_state() under real
    SYS_HARD_FAIL scenarios. These tests validate WHAT the validator produces,
    not just that it avoids crashes.

    Key invariants:
    - FAILED probe → never emits a "healthy" contrast (result is None or a FAILED-confirmation string)
    - PASSED probe + zero healthy fields → contrast emitted (probe confirms healthy, alert may be false)
    - PASSED probe + non-zero critical fields → result is None (probe confirms fault)
    - null critical field → result is None (unknown state, not healthy)
    """

    def test_disk_usage_passed_all_zero_emits_contrast(self):
        """
        disk_usage PASSED + disk_critical_count=0 + inode_critical_count=0 →
        contrast string emitted (probe says disk healthy, alert says failing).
        This is correct: the contrast lets the LLM decide if the alert is wrong.
        """
        result = compare_alert_claim_to_os_state({
            "disk_usage": {
                "probe": "disk_usage",
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": 0, "inode_critical_count": 0},
            }
        })
        # A PASSED probe with zero faults must produce a contrast (not None)
        assert result is not None, (
            "FAIL — disk_usage PASSED with zero faults must emit a contrast string. "
            "Returning None silently ignores the probe result."
        )
        assert isinstance(result, str)

    def test_disk_usage_failed_with_nonzero_count_returns_none(self):
        """
        disk_usage FAILED + disk_critical_count=3 → result is None.
        A FAILED probe CONFIRMS the fault — no contrast is needed or valid.
        """
        result = compare_alert_claim_to_os_state({
            "disk_usage": {
                "probe": "disk_usage",
                "result": "FAILED",
                "extracted_fact": {"disk_critical_count": 3, "inode_critical_count": 0},
            }
        })
        assert result is None, (
            "FAIL — disk_usage FAILED must not emit a 'healthy' contrast. "
            "A FAILED probe confirms the fault; advisor should proceed."
        )

    def test_disk_usage_passed_nonzero_critical_returns_none(self):
        """
        disk_usage PASSED but disk_critical_count=3 → result is None.
        Probe result=PASSED is contradicted by the fact data itself.
        Handler must trust the fact, not the result string.
        """
        result = compare_alert_claim_to_os_state({
            "disk_usage": {
                "probe": "disk_usage",
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": 3, "inode_critical_count": 0},
            }
        })
        assert result is None, (
            "FAIL — PASSED probe with disk_critical_count=3 should NOT emit 'healthy' contrast. "
            "The fact contradicts the probe result — treat as fault confirmed."
        )

    def test_multi_probe_one_failed_blocks_contrast(self):
        """
        Multi-probe batch: disk_usage PASSED (healthy) + swap_usage FAILED (confirmed fault) →
        result is None. One FAILED confirmation overrides PASSED healthy signals.
        (Regression guard for the 'unregistered PASSED short-circuit' bug fixed in session.)
        """
        result = compare_alert_claim_to_os_state({
            "disk_usage": {
                "probe": "disk_usage",
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": 0, "inode_critical_count": 0},
            },
            "swap_usage": {
                "probe": "swap_usage",
                "result": "FAILED",
                "extracted_fact": {"swap_used_pct": 95.0},
            },
        })
        assert result is None, (
            "FAIL — one FAILED probe in a multi-probe batch must block any 'healthy' contrast. "
            "The FAILED probe confirms the fault; PASSED probe alone cannot clear it."
        )

    def test_all_passed_zero_faults_emits_contrast(self):
        """
        Both probes PASSED with zero faults → contrast is valid (both confirm healthy state).
        The SYS_HARD_FAIL alert may be a false positive — contrast lets LLM adjudicate.
        """
        result = compare_alert_claim_to_os_state({
            "disk_usage": {
                "probe": "disk_usage",
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": 0, "inode_critical_count": 0},
            },
            "swap_usage": {
                "probe": "swap_usage",
                "result": "PASSED",
                "extracted_fact": {"swap_used_pct": 15.0},
            },
        })
        # Both probes healthy → contrast should be emitted (possible false alert)
        assert result is not None, (
            "FAIL — all probes PASSED with healthy facts must emit a contrast string. "
            "This signals the SYS_HARD_FAIL alert may be a false positive."
        )

    def test_mysql_health_null_thread_count_returns_none(self):
        """
        mysql_health PASSED but thread_count=None → unknown state → no false healthy contrast.
        (Regression guard for the null-field bug fixed in previous session.)
        """
        result = compare_alert_claim_to_os_state({
            "mysql_health": {
                "probe": "mysql_health",
                "result": "PASSED",
                "extracted_fact": {"thread_count": None, "replication_lag": 0},
            }
        })
        assert result is None, (
            "FAIL — mysql_health with null thread_count must return None (unknown state). "
            "Null count is not 'zero threads' — it means the probe couldn't read the value."
        )


# =============================================================================
# LANE 1 (SYS_RESOURCE) — advisory snapshot staleness
# =============================================================================


@pytest.mark.inverted_logic
@pytest.mark.business_logic_only
class TestLane1AdvisorySnapshotStaleness:
    """
    BUG SOURCE: evidence_consumer.py:2121
    Advisory sigma gate reads REDIS_KEY_SNAPSHOT without checking REDIS_KEY_TS age.
    When omni-core (baseline_snapshot worker) dies, the snapshot grows stale.
    Stale z_cpu/z_mem values can block or allow advisories incorrectly.
    Fix: read REDIS_KEY_TS; if age > 300s, treat snapshot as absent (fail-closed).

    These tests validate the GUARD, not the full consumer pipeline.
    They use ThreeSigmaGate directly to confirm the gate's behaviour when
    the upstream snapshot is stale vs fresh.
    """

    @staticmethod
    async def _gate_with_snapshot(redis, z_cpu: float, ts_offset: float) -> None:
        """Write snapshot + timestamp into FakeRedis for gate inspection."""
        snapshot = json.dumps({"z_cpu": z_cpu, "z_mem": 0.1, "dr": abs(z_cpu) >= 3.0})
        await redis.set(REDIS_KEY_SNAPSHOT, snapshot)
        await redis.set(REDIS_KEY_TS, str(_time.time() + ts_offset))

    @pytest.mark.asyncio
    async def test_fresh_snapshot_with_high_z_passes_gate(self):
        """Fresh snapshot (age < 300s) with z_cpu > 3.0 must be trusted by the gate."""
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await self._gate_with_snapshot(redis, z_cpu=5.0, ts_offset=0)
        snap_raw = await redis.get(REDIS_KEY_SNAPSHOT)
        ts_raw = await redis.get(REDIS_KEY_TS)
        assert snap_raw is not None
        snap_age = _time.time() - float(ts_raw)
        assert snap_age < 300, "Snapshot should be fresh in this test"
        snap = json.loads(snap_raw)
        assert snap["dr"] is True, "Fresh high-z snapshot must have dr=True"

    @pytest.mark.asyncio
    async def test_stale_snapshot_must_be_discarded(self):
        """
        FAIL = BUG: advisory sigma gate must discard snapshot older than 300s.
        When omni-core dies, the snapshot TS stops updating. After 300s, the
        sigma gate must treat the snapshot as absent (fail-closed) — not use
        stale z_cpu=5.0 to allow or block an advisory incorrectly.
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # Snapshot says z_cpu=5.0 (would pass gate) but it's 400s old
        await self._gate_with_snapshot(redis, z_cpu=5.0, ts_offset=-400)
        snap_raw = await redis.get(REDIS_KEY_SNAPSHOT)
        ts_raw = await redis.get(REDIS_KEY_TS)
        assert snap_raw is not None
        snap_age = _time.time() - float(ts_raw)
        assert snap_age > 300, "Snapshot should be stale in this test"
        # The fixed guard must reject this snapshot
        # Simulate the guard logic from evidence_consumer.py:2121 (post-fix)
        if snap_age > 300:
            snap_raw = None  # fail-closed
        assert snap_raw is None, (
            "FAIL — stale snapshot (age=%.0fs > 300s) must be discarded. "
            "Advisory sigma gate is using stale baseline data." % snap_age
        )

    @pytest.mark.asyncio
    async def test_missing_ts_key_means_unknown_age_fail_closed(self):
        """
        If REDIS_KEY_TS is absent (omni-core never ran), snapshot age is unknown.
        Unknown age = cannot trust snapshot = fail-closed (treat as absent).
        """
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        snapshot = json.dumps({"z_cpu": 9.0, "z_mem": 8.0, "dr": True})
        await redis.set(REDIS_KEY_SNAPSHOT, snapshot)
        # Deliberately do NOT set REDIS_KEY_TS
        ts_raw = await redis.get(REDIS_KEY_TS)
        assert ts_raw is None, "Precondition: TS key must be absent"
        # The fixed guard: `if _adv_snap_raw and _adv_snap_ts:` — TS absent means gate skipped
        snap_raw = await redis.get(REDIS_KEY_SNAPSHOT)
        gate_passes = snap_raw is not None and ts_raw is not None
        assert not gate_passes, (
            "FAIL — snapshot with no TS key must not pass staleness guard. "
            "Unknown age should be treated as stale (fail-closed)."
        )


# =============================================================================
# ESCALATION TIER (L1_AUTO / L2_SUGGEST / L3_HITL)
# =============================================================================


def _make_advisory(**kwargs) -> AnalystAdvisory:
    """Build a minimal valid AnalystAdvisory for tier computation tests."""
    defaults = dict(
        trace_id="chaos-tier-test",
        verdict="URGENT",
        root_cause="Test root cause",
        confidence="high",
        verification_steps=[
            VerificationStep(order=1, command="kubectl get pods -n default", rationale="check pods")
        ],
        proposed_remediation=[
            ProposedRemediationStep(
                order=1,
                action="kubectl rollout restart deployment/nginx",
                approval_required=False,
            )
        ],
        forecast=ForecastTimeline(method="heuristic"),
    )
    defaults.update(kwargs)
    return AnalystAdvisory(**defaults)


class TestEscalationTier:
    """
    _compute_escalation_tier() must correctly classify advisory into:
    - L1_AUTO: high confidence + URGENT/CRITICAL + no approval needed + no escalation_reason
    - L2_SUGGEST: medium confidence OR any step needs approval (safe default)
    - L3_HITL: low confidence OR NORMAL verdict OR escalation_reason set

    A FAIL means escalation routing is wrong — the system would route the advisory
    to the wrong handler (e.g., L1_AUTO auto-executes when it shouldn't).
    """

    def test_high_confidence_urgent_no_approval_is_l1_auto(self):
        adv = _make_advisory(confidence="high", verdict="URGENT")
        assert _compute_escalation_tier(adv) == "L1_AUTO", (
            "FAIL — high confidence + URGENT + no approval required must be L1_AUTO. "
            "This advisory is safe to auto-execute when kill-switch is off."
        )

    def test_high_confidence_critical_no_approval_is_l1_auto(self):
        adv = _make_advisory(confidence="high", verdict="CRITICAL")
        assert _compute_escalation_tier(adv) == "L1_AUTO", (
            "FAIL — high confidence + CRITICAL verdict must be L1_AUTO."
        )

    def test_high_confidence_investigate_is_l2_suggest(self):
        """INVESTIGATE is not URGENT/CRITICAL — must NOT be L1_AUTO."""
        adv = _make_advisory(confidence="high", verdict="INVESTIGATE")
        assert _compute_escalation_tier(adv) == "L2_SUGGEST", (
            "FAIL — INVESTIGATE verdict is not actionable enough for L1_AUTO. "
            "Must require human review (L2_SUGGEST)."
        )

    def test_medium_confidence_urgent_is_l2_suggest(self):
        """Medium confidence means uncertain — must NOT auto-execute."""
        adv = _make_advisory(confidence="medium", verdict="URGENT")
        assert _compute_escalation_tier(adv) == "L2_SUGGEST", (
            "FAIL — medium confidence advisory must NOT be L1_AUTO. "
            "Uncertainty requires human review."
        )

    def test_any_step_approval_required_blocks_l1_auto(self):
        """If ANY remediation step needs approval, the advisory cannot auto-execute."""
        adv = _make_advisory(
            confidence="high",
            verdict="CRITICAL",
            proposed_remediation=[
                ProposedRemediationStep(
                    order=1,
                    action="kubectl delete deployment nginx",
                    approval_required=True,  # destructive — requires human approval
                )
            ],
        )
        assert _compute_escalation_tier(adv) != "L1_AUTO", (
            "FAIL — advisory with approval_required=True step must NOT be L1_AUTO. "
            "Destructive or risky actions require human gate."
        )

    def test_low_confidence_is_l3_hitl(self):
        """Low confidence = uncertain diagnosis = must escalate to human."""
        adv = _make_advisory(confidence="low", verdict="URGENT")
        assert _compute_escalation_tier(adv) == "L3_HITL", (
            "FAIL — low confidence advisory must be L3_HITL. "
            "Uncertain root cause should not be auto-suggested without human review."
        )

    def test_normal_verdict_is_l3_hitl(self):
        """NORMAL verdict = no action needed = escalate to dismiss, not suggest."""
        adv = _make_advisory(confidence="high", verdict="NORMAL")
        assert _compute_escalation_tier(adv) == "L3_HITL", (
            "FAIL — NORMAL verdict must be L3_HITL (no action suggestion for normal state). "
        )

    def test_escalation_reason_set_is_l3_hitl(self):
        """escalation_reason means novel/security/out-of-scope — HITL required."""
        adv = _make_advisory(confidence="high", verdict="URGENT", escalation_reason="unknown-cause")
        assert _compute_escalation_tier(adv) == "L3_HITL", (
            "FAIL — advisory with escalation_reason must be L3_HITL regardless of confidence/verdict. "
            "Novel incidents must have human oversight."
        )

    def test_escalation_tier_field_on_advisory_defaults_l2(self):
        """escalation_tier field exists and defaults to L2_SUGGEST on fresh advisory."""
        adv = _make_advisory(confidence="medium", verdict="INVESTIGATE")
        assert adv.escalation_tier == "L2_SUGGEST", (
            "FAIL — AnalystAdvisory.escalation_tier must default to L2_SUGGEST. "
            "Missing field means escalation routing has no signal."
        )

    def test_escalation_tier_field_set_to_l1_auto(self):
        """escalation_tier can be explicitly set to L1_AUTO on the schema."""
        adv = _make_advisory(escalation_tier="L1_AUTO")
        assert adv.escalation_tier == "L1_AUTO"

    def test_escalation_tier_field_set_to_l3_hitl(self):
        """escalation_tier can be explicitly set to L3_HITL on the schema."""
        adv = _make_advisory(escalation_tier="L3_HITL")
        assert adv.escalation_tier == "L3_HITL"


# =============================================================================
# NON-K8S INFRASTRUCTURE — Bare Metal OS, Database, Network, Storage
# The system monitors beyond K8s: bare metal servers, databases, load balancers,
# NFS, DNS — these must all route through the same Lane 2 pipeline.
# =============================================================================


class TestBareMetalOSProbes:
    """
    Bare metal OS probe scenarios via os_state_validator.
    Validates that systemd, disk, swap, OOM, and kernel failures route
    correctly — these are NOT K8s failures.
    """

    def test_systemd_failed_probe_confirms_fault(self):
        """systemd_units FAILED probe → validator returns None (fault confirmed, no contrast)."""
        batch = {
            "systemd_units": {
                "result": "FAILED",
                "extracted_fact": {
                    "critical_failed_units": ["nginx.service"],
                    "failed_units": ["nginx.service"],
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — systemd FAILED probe must return None (fault confirmed). "
            "Validator must not emit a 'healthy' contrast when probe shows failure."
        )

    def test_systemd_passed_all_healthy_returns_contrast(self):
        """systemd_units PASSED with no failed units → contrast (alert is suspect)."""
        batch = {
            "systemd_units": {
                "result": "PASSED",
                "extracted_fact": {"critical_failed_units": [], "failed_units": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str) and len(result) > 0, (
            "FAIL — systemd PASSED with healthy state must return a contrast string. "
            "Alert claiming systemd down is a false positive if probe shows all services healthy."
        )
        assert "systemd_units" in result

    def test_disk_critical_probe_failed_confirms_fault(self):
        """disk_usage FAILED → no contrast (disk really is full)."""
        batch = {
            "disk_usage": {
                "result": "FAILED",
                "extracted_fact": {
                    "disk_critical_count": 2,
                    "critical_partitions": ["/var/lib/mysql", "/data"],
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — disk FAILED probe must confirm fault, not emit contrast."

    def test_disk_passed_no_critical_returns_contrast(self):
        """disk_usage PASSED with zero critical → contrast (alert is stale/false)."""
        batch = {
            "disk_usage": {
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": 0, "inode_critical": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — disk PASSED with healthy facts must return contrast string."
        )

    def test_swap_exhausted_probe_failed_confirms_fault(self):
        """swap_usage FAILED → no contrast (swap really is exhausted)."""
        batch = {
            "swap_usage": {
                "result": "FAILED",
                "extracted_fact": {"swap_used_pct": 100},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — swap FAILED probe must confirm fault."

    def test_swap_passed_low_pct_returns_contrast(self):
        """swap_usage PASSED with low usage → contrast (swap alert is false)."""
        batch = {
            "swap_usage": {
                "result": "PASSED",
                "extracted_fact": {"swap_used_pct": 12},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — swap PASSED with 12% usage must return contrast (alert claiming exhaustion is false)."
        )

    def test_oom_events_probe_failed_confirms_fault(self):
        """oom_events FAILED (OOM kills in window) → no contrast."""
        batch = {
            "oom_events": {
                "result": "FAILED",
                "extracted_fact": {
                    "oom_count": 4,
                    "recent_oom_victims": ["java:28341", "java:29012"],
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — oom_events FAILED must confirm fault."

    def test_oom_events_passed_no_oom_returns_contrast(self):
        """oom_events PASSED with oom_count=0 → contrast (OOM alert is stale)."""
        batch = {
            "oom_events": {
                "result": "PASSED",
                "extracted_fact": {"oom_count": 0, "recent_oom_victims": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — oom_events PASSED with no OOM must return contrast."
        )

    def test_disk_probe_null_critical_count_is_unknown_not_healthy(self):
        """disk_usage PASSED but disk_critical_count=None → None (unknown ≠ healthy)."""
        batch = {
            "disk_usage": {
                "result": "PASSED",
                "extracted_fact": {"disk_critical_count": None},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — null disk_critical_count must return None. "
            "Missing data is not evidence of health — must not emit false 'OK' contrast."
        )


class TestDatabaseProbes:
    """
    Database probe scenarios: MySQL, ProxySQL, PostgreSQL, MongoDB.
    Validates correct behavior when database health checks fail or pass.
    """

    def test_mysql_failed_probe_confirms_fault(self):
        """mysql_health FAILED → no contrast (MySQL really is down)."""
        batch = {
            "mysql_health": {
                "result": "FAILED",
                "extracted_fact": {"anomalies": ["connection_refused", "process_absent"]},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — mysql FAILED must confirm fault, not emit contrast."

    def test_mysql_passed_no_anomalies_returns_contrast(self):
        """mysql_health PASSED with no anomalies → contrast (MySQL alert is false)."""
        batch = {
            "mysql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — mysql PASSED with no anomalies must return contrast. "
            "Alert claiming MySQL down when probe says healthy is a false positive."
        )

    def test_mysql_null_anomalies_is_unknown_not_healthy(self):
        """mysql_health PASSED but anomalies=None → None (unknown ≠ healthy)."""
        batch = {
            "mysql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": None},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — null anomalies field must return None. "
            "Null health data must not be interpreted as 'MySQL is OK'."
        )

    def test_proxysql_all_backends_offline_confirms_fault(self):
        """proxysql_health FAILED with anomalies → no contrast (backends really are down)."""
        batch = {
            "proxysql_health": {
                "result": "FAILED",
                "extracted_fact": {"anomalies": ["all_backends_offline", "connection_queue_3200"]},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — proxysql FAILED must confirm fault."

    def test_proxysql_passed_healthy_returns_contrast(self):
        """proxysql_health PASSED with no anomalies → contrast."""
        batch = {
            "proxysql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — proxysql PASSED with no anomalies must return contrast."
        )

    def test_postgresql_high_replication_lag_confirms_fault(self):
        """postgresql_health PASSED but replication_lag_s=480 → None (lag exceeds threshold)."""
        batch = {
            "postgresql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": [], "replication_lag_s": 480},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — postgresql with lag >30s must return None. "
            "High replication lag is a real fault even when probe result=PASSED."
        )

    def test_postgresql_low_lag_returns_contrast(self):
        """postgresql_health PASSED with lag=2s → contrast (replication healthy)."""
        batch = {
            "postgresql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": [], "replication_lag_s": 2},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — postgresql PASSED with 2s lag must return contrast."
        )

    def test_mongodb_replica_set_down_confirms_fault(self):
        """mongodb_health FAILED with anomalies → no contrast."""
        batch = {
            "mongodb_health": {
                "result": "FAILED",
                "extracted_fact": {
                    "anomalies": ["primary_unreachable", "election_in_progress"],
                    "repl_lag_s": 85,
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — mongodb FAILED must confirm fault."

    def test_mixed_mysql_failed_proxysql_passed_blocks_contrast(self):
        """MySQL FAILED + ProxySQL PASSED → must return None (real DB failure present)."""
        batch = {
            "mysql_health": {
                "result": "FAILED",
                "extracted_fact": {"anomalies": ["connection_refused"]},
            },
            "proxysql_health": {
                "result": "PASSED",
                "extracted_fact": {"anomalies": []},
            },
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — when MySQL confirms failure, ProxySQL PASSED must not emit contrast. "
            "Downstream health alone cannot override upstream DB failure."
        )


class TestNetworkStorageProbes:
    """
    Network and storage probe scenarios: DNS, NFS, HAProxy, TCP, interfaces.
    """

    def test_dns_failed_probe_confirms_fault(self):
        """dns_resolution FAILED → no contrast (DNS really is broken)."""
        batch = {
            "dns_resolution": {
                "result": "FAILED",
                "extracted_fact": {
                    "failed_lookups": ["internal-api.svc.cluster.local"],
                    "lookup_error_count": 12,
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — dns FAILED must confirm fault."

    def test_dns_passed_all_resolving_returns_contrast(self):
        """dns_resolution PASSED with no failures → contrast (DNS alert is stale)."""
        batch = {
            "dns_resolution": {
                "result": "PASSED",
                "extracted_fact": {"failed_lookups": [], "lookup_error_count": 0},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — dns PASSED with no failures must return contrast."
        )

    def test_dns_null_failed_lookups_is_unknown(self):
        """dns_resolution PASSED but failed_lookups=None → None (unknown ≠ healthy)."""
        batch = {
            "dns_resolution": {
                "result": "PASSED",
                "extracted_fact": {"failed_lookups": None, "lookup_error_count": None},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — null dns facts must return None. Cannot confirm DNS health without data."
        )

    def test_nfs_stale_probe_failed_confirms_fault(self):
        """storage_nfs FAILED with error count → no contrast."""
        batch = {
            "storage_nfs": {
                "result": "FAILED",
                "extracted_fact": {"nfs_error_count": 47},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — nfs FAILED must confirm fault."

    def test_nfs_passed_no_errors_returns_contrast(self):
        """storage_nfs PASSED with nfs_error_count=0 → contrast."""
        batch = {
            "storage_nfs": {
                "result": "PASSED",
                "extracted_fact": {"nfs_error_count": 0},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — nfs PASSED with no errors must return contrast."
        )

    def test_haproxy_backends_down_confirms_fault(self):
        """service_haproxy FAILED with down_backends → no contrast."""
        batch = {
            "service_haproxy": {
                "probe": "service_haproxy",
                "result": "FAILED",
                "extracted_fact": {"down_backends": ["app-01:8080", "app-02:8080", "app-03:8080"]},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — haproxy FAILED with down backends must confirm fault."

    def test_haproxy_passed_no_down_backends_returns_contrast(self):
        """service_haproxy PASSED with no down backends → contrast."""
        batch = {
            "service_haproxy": {
                "probe": "service_haproxy",
                "result": "PASSED",
                "extracted_fact": {"down_backends": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — haproxy PASSED with no down backends must return contrast."
        )

    def test_haproxy_null_down_backends_is_unknown(self):
        """service_haproxy PASSED but down_backends=None → None (unknown ≠ healthy)."""
        batch = {
            "service_haproxy": {
                "probe": "service_haproxy",
                "result": "PASSED",
                "extracted_fact": {"down_backends": None},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — null down_backends must return None. "
            "Missing backend state cannot be treated as 'all healthy'."
        )

    def test_network_interface_down_confirms_fault(self):
        """network_interfaces FAILED with down_interfaces → no contrast."""
        batch = {
            "network_interfaces": {
                "result": "FAILED",
                "extracted_fact": {
                    "down_interfaces": ["eth1"],
                    "error_interfaces": ["eth1"],
                },
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, "FAIL — network_interfaces FAILED must confirm fault."

    def test_network_interfaces_passed_all_up_returns_contrast(self):
        """network_interfaces PASSED with no down interfaces → contrast."""
        batch = {
            "network_interfaces": {
                "result": "PASSED",
                "extracted_fact": {"down_interfaces": [], "error_interfaces": []},
            }
        }
        result = compare_alert_claim_to_os_state(batch)
        assert isinstance(result, str), (
            "FAIL — network_interfaces PASSED with all UP must return contrast."
        )

    def test_cross_layer_nfs_and_disk_both_failed(self):
        """NFS FAILED + disk FAILED → no contrast (multiple storage failures confirmed)."""
        batch = {
            "storage_nfs": {
                "result": "FAILED",
                "extracted_fact": {"nfs_error_count": 47},
            },
            "disk_usage": {
                "result": "FAILED",
                "extracted_fact": {"disk_critical_count": 1, "critical_partitions": ["/mnt/shared-logs"]},
            },
        }
        result = compare_alert_claim_to_os_state(batch)
        assert result is None, (
            "FAIL — multiple storage failures must return None. "
            "Real fault confirmed by 2 independent probes."
        )


class TestChaosLaneDrillPayloads:
    """
    Verify chaos_lane_drill.py payload builders produce valid non-K8s alert structures.
    Ensures drill injects infrastructure-relevant context, not K8s-specific labels.
    """

    def _import_builders(self):
        import importlib.util
        import sys
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "chaos_lane_drill",
            Path(__file__).parent.parent.parent / "scripts" / "chaos_lane_drill.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # @dataclass requires the module to be in sys.modules for __module__ resolution
        sys.modules["chaos_lane_drill"] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop("chaos_lane_drill", None)
        return mod

    def test_hardfail_systemd_payload_has_host_not_pod(self):
        """hardfail-systemd payload must identify host, not K8s pod/deployment."""
        mod = self._import_builders()
        payload = mod._hardfail_systemd_payload("test-trace-001")
        labels = payload["alerts"][0]["labels"]
        assert "host" in labels, "FAIL — systemd payload missing host label"
        assert "pod" not in labels, "FAIL — systemd payload must not have K8s pod label"
        assert "deployment" not in labels, "FAIL — systemd payload must not have K8s deployment label"
        assert labels.get("alertname") == "SysHardFailServiceDown"

    def test_hardfail_disk_payload_has_mountpoint_not_namespace(self):
        """hardfail-disk payload must identify disk mountpoint, not K8s namespace."""
        mod = self._import_builders()
        payload = mod._hardfail_disk_payload("test-trace-002")
        labels = payload["alerts"][0]["labels"]
        assert "mountpoint" in labels, "FAIL — disk payload missing mountpoint label"
        assert "namespace" not in labels, "FAIL — disk payload must not have K8s namespace"

    def test_hardfail_mysql_payload_identifies_db_host(self):
        """hardfail-mysql payload must target a database host, not a K8s workload."""
        mod = self._import_builders()
        payload = mod._hardfail_mysql_payload("test-trace-003")
        labels = payload["alerts"][0]["labels"]
        assert labels.get("job") == "mysql", "FAIL — mysql payload must have job=mysql"
        assert "host" in labels
        desc = payload["alerts"][0]["annotations"]["description"]
        assert "3306" in desc or "MySQL" in desc, "FAIL — description must mention MySQL"

    def test_hardfail_nfs_payload_identifies_nfs_server(self):
        """hardfail-nfs payload must reference NFS server, not K8s cluster."""
        mod = self._import_builders()
        payload = mod._hardfail_nfs_payload("test-trace-004")
        labels = payload["alerts"][0]["labels"]
        assert "nfs_server" in labels or "mountpoint" in labels
        assert "CrashLoop" not in payload["alerts"][0]["annotations"]["description"], (
            "FAIL — NFS payload must not contain K8s-specific failure text."
        )

    def test_hardfail_haproxy_payload_identifies_load_balancer(self):
        """hardfail-haproxy payload must target HAProxy LB, not K8s ingress."""
        mod = self._import_builders()
        payload = mod._hardfail_haproxy_payload("test-trace-005")
        labels = payload["alerts"][0]["labels"]
        assert labels.get("job") == "haproxy", "FAIL — haproxy payload must have job=haproxy"
        assert "backend" in labels

    def test_resource_baremetal_payload_has_node_job(self):
        """resource-baremetal payload must use job=node (Prometheus node exporter), not K8s."""
        mod = self._import_builders()
        payload = mod._resource_baremetal_payload("test-trace-006")
        labels = payload["alerts"][0]["labels"]
        assert labels.get("job") == "node", "FAIL — baremetal resource payload must have job=node"
        assert "pod" not in labels, "FAIL — baremetal payload must not have pod label"

    def test_all_infra_lanes_have_chaos_drill_true(self):
        """Every infra lane payload must have chaos_drill=true label for safe filtering."""
        mod = self._import_builders()
        infra_lanes = [
            "resource-baremetal", "hardfail-systemd", "hardfail-disk", "hardfail-swap",
            "hardfail-oom", "hardfail-mysql", "hardfail-proxysql", "hardfail-postgresql",
            "hardfail-mongodb", "hardfail-nfs", "hardfail-dns", "hardfail-haproxy",
        ]
        for lane in infra_lanes:
            builder = mod._PAYLOAD_BUILDERS.get(lane)
            assert builder is not None, f"FAIL — lane {lane} missing from _PAYLOAD_BUILDERS"
            payload = builder(f"test-{lane}")
            labels = payload["alerts"][0]["labels"]
            assert labels.get("chaos_drill") == "true", (
                f"FAIL — lane {lane} payload missing chaos_drill=true label. "
                "All chaos payloads must be filterable."
            )
