"""TDD — INV_DIAG_MEASURED: kết luận không được nêu đại lượng chưa ai đo.

Ca tái hiện lấy nguyên văn từ sự cố thật 2026-08-02 (8 vòng lặp CPU trên VM
`cust-app`). Session `omni:diag:session:ra-689e6dc59ea4`:

  turn 1  hypothesis "CPU saturation on host cust-app"  confidence 0.75
          evidence_gaps ["No information about disk usage or memory pressure"]
          → xin chạy `df -h` (lệnh ĐĨA) để điều tra sự cố CPU
  turn 2  `df` trả 18% used → LLM suy "đĩa ổn nên chắc do bộ nhớ", VỨT giả thuyết
          đúng, đổi sang "Insufficient memory available on the host",
          confidence 0.75 → 0.95, diagnosis_complete=true

Ba lỗi cùng lúc, cả ba đều lọt qua `_apply_grounding_gate` cũ (nó chỉ soi
đường dẫn + phần trăm, mà "Insufficient memory" không có cái nào):

  (a) loại trừ một khả năng bị dùng làm bằng chứng cho khả năng khác
  (b) confidence TĂNG sau khi bằng chứng bị loại
  (c) kết luận về đại lượng CHƯA TỪNG ĐO — không lệnh nào chạm tới bộ nhớ

Thẻ thứ hai (`ra-d645c49ed6d1`) là lớp lỗi anh em: kết luận "aoip-agent.service
đang crash" trong khi `systemctl is-failed` trả rc=1 (= KHÔNG có unit nào hỏng).
"""
from __future__ import annotations

import pytest

from pkg.diagnostics.measurement_grounding import (
    apply_measurement_gate,
    confidence_inflation,
    contradicted_service_claims,
    quantities_claimed,
    quantities_in_alert,
    quantities_measured,
    unmeasured_quantities,
)

# ── Dữ liệu thật, chép từ Redis ────────────────────────────────────────────────

ALERT_CPU = "[cust-app] CPU 98.3%>80.0%"

DF_STDOUT = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "/dev/vdb1       178G   32G  146G  18% /\n"
    "tmpfs           7.9G     0  7.9G   0% /tmp\n"
)

DF_RESULT = {
    "cmd_id": "cmd-1dbc366ecf32",
    "command_str": "df -h",
    "stdout": DF_STDOUT,
    "stderr": "",
    "rc": 0,
}

FINAL_MEMORY_PIVOT = {
    "root_cause": "Insufficient memory available on the host (mem_percent=60.0, mem_used_mb=5999.7)",
    "affected_components": ["payment-api", "omni-remote-agent"],
    "blast_radius": "High CPU usage might affect other services running on the host.",
    "impact_summary": "High CPU usage due to insufficient memory available.",
    "remediation_steps": ["Increase the amount of RAM allocated to the VM"],
    "confidence": 0.95,
    "suggested_recovery": None,
}

TURNS_MEMORY_PIVOT = [
    {
        "turn": 1,
        "hypothesis": "CPU saturation on host cust-app due to high load average",
        "confidence": 0.75,
        "command_results": [DF_RESULT],
    },
    {
        "turn": 2,
        "hypothesis": "Insufficient memory available on the host",
        "confidence": 0.95,
        "command_results": [],
    },
]


# ── quantities_claimed ────────────────────────────────────────────────────────

class TestQuantitiesClaimed:
    def test_memory_claim_detected(self) -> None:
        assert "memory" in quantities_claimed(FINAL_MEMORY_PIVOT["root_cause"])

    def test_cpu_claim_detected(self) -> None:
        assert quantities_claimed("CPU saturation on host cust-app due to high load average") == {"cpu"}

    def test_disk_and_inode_are_distinct(self) -> None:
        assert quantities_claimed("inode exhaustion on /var") == {"inode"}
        assert "disk" in quantities_claimed("/var partition is full, no space left on device")

    def test_service_word_alone_is_not_a_state_claim(self) -> None:
        """"caused by the nginx service" nêu TÊN dịch vụ, không khẳng định trạng thái —
        bắt nó là bắt oan mọi kết luận có chữ 'service'."""
        assert quantities_claimed("High CPU usage caused by the nginx service") == {"cpu"}

    def test_service_failure_claim_detected(self) -> None:
        assert "service" in quantities_claimed("aoip-agent.service is crashing due to high CPU usage.")

    def test_empty_text_claims_nothing(self) -> None:
        assert quantities_claimed("") == set()


# ── quantities_in_alert / quantities_measured ─────────────────────────────────

class TestGroundingSources:
    def test_alert_hint_grounds_cpu(self) -> None:
        assert quantities_in_alert(ALERT_CPU) == {"cpu"}

    def test_df_measures_disk_not_memory(self) -> None:
        measured = quantities_measured(["df -h"])
        assert "disk" in measured
        assert "memory" not in measured

    def test_df_dash_i_measures_inodes(self) -> None:
        assert "inode" in quantities_measured(["df -i /var"])

    def test_free_measures_memory(self) -> None:
        assert "memory" in quantities_measured(["free -h"])

    def test_systemctl_measures_service_state(self) -> None:
        assert "service" in quantities_measured(["systemctl is-failed"])

    def test_unknown_command_measures_nothing(self) -> None:
        assert quantities_measured(["zzz --nope"]) == set()


# ── unmeasured_quantities — ca tái hiện chính ─────────────────────────────────

class TestUnmeasuredQuantities:
    def test_df_then_memory_conclusion_is_flagged(self) -> None:
        """CA GỐC: chạy `df -h` (đĩa) rồi kết luận về BỘ NHỚ."""
        out = unmeasured_quantities(
            FINAL_MEMORY_PIVOT["root_cause"], ALERT_CPU, ["df -h"]
        )
        assert out == ["memory"]

    def test_conclusion_matching_the_alert_quantity_passes(self) -> None:
        out = unmeasured_quantities(
            "CPU saturation on host cust-app (load_avg_1m=11.89)", ALERT_CPU, ["top -b -n 1"]
        )
        assert out == []

    def test_conclusion_backed_by_a_command_passes(self) -> None:
        out = unmeasured_quantities(
            "Insufficient memory available on the host", ALERT_CPU, ["free -h"]
        )
        assert out == []

    def test_no_grounding_source_at_all_does_not_guess(self) -> None:
        """Session degraded (agent offline, alert rỗng): không có gì để so — không đoán bừa."""
        assert unmeasured_quantities("Insufficient memory available", "", []) == []


# ── contradicted_service_claims — thẻ ra-d645c49ed6d1 ─────────────────────────

class TestContradictedServiceClaims:
    def test_is_failed_rc1_contradicts_crash_claim(self) -> None:
        """`systemctl is-failed` rc=1 nghĩa là KHÔNG có unit nào hỏng."""
        results = [
            {"command_str": "systemctl is-failed", "stdout": "running\n", "rc": 1},
            {"command_str": "journalctl -u aoip-agent.service", "stdout": "Jul 13 ...", "rc": 0},
        ]
        out = contradicted_service_claims(
            "aoip-agent.service is crashing due to high CPU usage.", results
        )
        assert out and "systemctl is-failed" in out[0]

    def test_is_failed_rc0_means_really_failed_no_contradiction(self) -> None:
        results = [{"command_str": "systemctl is-failed nginx.service", "stdout": "failed\n", "rc": 0}]
        assert contradicted_service_claims("nginx.service is failed", results) == []

    def test_is_active_active_contradicts_down_claim(self) -> None:
        results = [{"command_str": "systemctl is-active nginx.service", "stdout": "active\n", "rc": 0}]
        out = contradicted_service_claims("nginx.service is down and not running", results)
        assert out

    def test_no_service_claim_no_contradiction(self) -> None:
        results = [{"command_str": "systemctl is-failed", "stdout": "running\n", "rc": 1}]
        assert contradicted_service_claims("CPU saturation on host", results) == []


# ── confidence_inflation ──────────────────────────────────────────────────────

class TestConfidenceInflation:
    def test_pivot_with_confidence_rise_and_no_supporting_measurement(self) -> None:
        """0.75 → 0.95 khi đổi sang đại lượng mà lệnh vừa chạy KHÔNG đo."""
        out = confidence_inflation(TURNS_MEMORY_PIVOT)
        assert out is not None
        assert out["from_confidence"] == pytest.approx(0.75)
        assert out["to_confidence"] == pytest.approx(0.95)
        assert "memory" in out["unsupported_quantities"]

    def test_same_hypothesis_across_turns_is_not_inflation(self) -> None:
        turns = [
            {"turn": 1, "hypothesis": "CPU saturation", "confidence": 0.95, "command_results": []},
            {"turn": 2, "hypothesis": "CPU saturation", "confidence": 0.95, "command_results": []},
        ]
        assert confidence_inflation(turns) is None

    def test_pivot_backed_by_matching_command_is_not_inflation(self) -> None:
        turns = [
            {
                "turn": 1,
                "hypothesis": "CPU saturation",
                "confidence": 0.6,
                "command_results": [{"command_str": "free -h", "stdout": "Mem: ...", "rc": 0}],
            },
            {"turn": 2, "hypothesis": "memory exhaustion", "confidence": 0.9, "command_results": []},
        ]
        assert confidence_inflation(turns) is None


# ── apply_measurement_gate — hợp nhất, đây là thứ diagnosis_loop gọi ──────────

class TestApplyMeasurementGate:
    def test_memory_pivot_is_neutralized(self) -> None:
        gated = apply_measurement_gate(
            FINAL_MEMORY_PIVOT,
            alert_hint=ALERT_CPU,
            command_results=[DF_RESULT],
            turns=TURNS_MEMORY_PIVOT,
        )
        assert gated["unmeasured_quantities"] == ["memory"]
        assert gated["root_cause"].startswith("[UNMEASURED: memory]")
        assert gated["confidence"] <= 0.3
        assert gated["suggested_recovery"] is None

    def test_original_dict_not_mutated(self) -> None:
        before = dict(FINAL_MEMORY_PIVOT)
        apply_measurement_gate(
            FINAL_MEMORY_PIVOT,
            alert_hint=ALERT_CPU,
            command_results=[DF_RESULT],
            turns=TURNS_MEMORY_PIVOT,
        )
        assert FINAL_MEMORY_PIVOT == before

    def test_contradicted_service_claim_drops_auto_recovery(self) -> None:
        """Nguy hiểm nhất: suggested_recovery restart một unit đang KHOẺ."""
        final = {
            "root_cause": "aoip-agent.service is crashing due to high CPU usage.",
            "confidence": 0.95,
            "remediation_steps": [],
            "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "aoip-agent.service"},
        }
        results = [
            {"command_str": "systemctl is-failed", "stdout": "running\n", "rc": 1},
            {"command_str": "journalctl -u aoip-agent.service", "stdout": "Jul 13 ...", "rc": 0},
        ]
        gated = apply_measurement_gate(
            final, alert_hint="[cust-edge] CPU 99.4%>80.0%", command_results=results, turns=[]
        )
        assert gated["suggested_recovery"] is None
        assert gated["contradicted_claims"]
        assert gated["confidence"] <= 0.3

    def test_grounded_conclusion_passes_through_unchanged(self) -> None:
        final = {
            "root_cause": "CPU saturation on host cust-app due to high load average (load_avg_1m=11.89)",
            "confidence": 0.95,
            "remediation_steps": ["x"],
            "suggested_recovery": None,
        }
        gated = apply_measurement_gate(
            final,
            alert_hint=ALERT_CPU,
            command_results=[{"command_str": "top -b -n 1", "stdout": "…", "rc": 0}],
            turns=[],
        )
        assert gated == final
        assert "unmeasured_quantities" not in gated

    def test_failed_command_does_not_count_as_a_measurement(self) -> None:
        """`ps aux --sort=-%cpu` rc=1 (BSD syntax error) KHÔNG đo được gì."""
        final = {
            "root_cause": "Insufficient memory available on the host",
            "confidence": 0.9,
            "remediation_steps": [],
            "suggested_recovery": None,
        }
        gated = apply_measurement_gate(
            final,
            alert_hint=ALERT_CPU,
            command_results=[{"command_str": "ps aux --sort=-%cpu", "stdout": "", "rc": 1}],
            turns=[],
        )
        assert gated["unmeasured_quantities"] == ["memory"]
