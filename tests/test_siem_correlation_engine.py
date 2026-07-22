"""TDD — services.siem_correlation.{confidence,chain}: scoring + chain build.

Parity contract: port 1-1 của brain-go ``internal/correlate/confidence.go`` +
``chain.go`` (buildChain/explainChain/isMonotonic/round3) và
``internal/publisher/chain_publisher.go`` (ValidateChain + chainEnvelope shape).
"""

from __future__ import annotations

import pytest

from services.siem_correlation.chain import (
    build_chain,
    explain_chain,
    is_monotonic,
    validate_chain,
)
from services.siem_correlation.confidence import (
    entity_score,
    round3,
    score_chain,
    sequence_score,
    volume_score,
)
from services.siem_correlation.models import Entity, IncidentMeta, KillChainStage


def _meta(id_: str, cat: str, stage: tuple[str, int], ts: int, sev: str = "medium", ip: str = "10.0.0.5") -> IncidentMeta:
    return IncidentMeta(
        id=id_, category=cat, severity=sev, source_ip=ip,
        stage=KillChainStage(name=stage[0], order=stage[1]), ts=ts,
    )


_WEIGHTS = {"w_entity": 0.4, "w_sequence": 0.35, "w_volume": 0.25}


class TestSignals:
    def test_entity_score_saturates_at_3_types(self):
        assert entity_score([]) == 0.0
        assert entity_score([Entity("ip", "a")]) == pytest.approx(1 / 3)
        assert entity_score([Entity("ip", "a"), Entity("ip", "b"), Entity("user", "u")]) == pytest.approx(2 / 3)
        assert entity_score([Entity("ip", "a"), Entity("user", "u"), Entity("host", "h")]) == 1.0
        assert entity_score([Entity("ip", "a"), Entity("user", "u"), Entity("host", "h"), Entity("pod", "p")]) == 1.0

    def test_sequence_score_monotonic_progression(self):
        members = [
            _meta("a", "port_scan", ("reconnaissance", 1), 100),
            _meta("b", "auth_failure", ("initial_access", 2), 110),
            _meta("c", "malware", ("execution", 3), 120),
        ]
        assert sequence_score(members) == 1.0

    def test_sequence_score_orders_by_timestamp_not_input_order(self):
        members = [
            _meta("c", "malware", ("execution", 3), 120),
            _meta("a", "port_scan", ("reconnaissance", 1), 100),
            _meta("b", "auth_failure", ("initial_access", 2), 110),
        ]
        assert sequence_score(members) == 1.0

    def test_sequence_score_ignores_unknown_stage_and_needs_2(self):
        assert sequence_score([_meta("a", "x", ("unknown", 0), 100)]) == 0.0
        assert sequence_score([
            _meta("a", "x", ("unknown", 0), 100),
            _meta("b", "port_scan", ("reconnaissance", 1), 110),
        ]) == 0.0

    def test_sequence_score_partial(self):
        members = [
            _meta("a", "port_scan", ("reconnaissance", 1), 100),
            _meta("b", "port_scan", ("reconnaissance", 1), 110),
            _meta("c", "auth_failure", ("initial_access", 2), 120),
        ]
        # transitions=2, advances=1
        assert sequence_score(members) == 0.5

    def test_volume_score_saturates_at_2x_threshold(self):
        assert volume_score(3, 3) == 0.5
        assert volume_score(6, 3) == 1.0
        assert volume_score(9, 3) == 1.0
        assert volume_score(1, 0) == 0.5  # threshold<=0 coerced to 1, cap=2

    def test_score_chain_weighted_and_rounded(self):
        members = [
            _meta("a", "port_scan", ("reconnaissance", 1), 100),
            _meta("b", "auth_failure", ("initial_access", 2), 110),
            _meta("c", "malware", ("execution", 3), 120),
        ]
        entities = [Entity("ip", "10.0.0.5")]
        sig = score_chain(members, entities, threshold=3, **_WEIGHTS)
        assert sig == {"entity": 0.333, "sequence": 1.0, "volume": 0.5, "confidence": 0.608}

    def test_score_chain_normalizes_weight_sum(self):
        members = [
            _meta("a", "port_scan", ("reconnaissance", 1), 100),
            _meta("b", "auth_failure", ("initial_access", 2), 110),
            _meta("c", "malware", ("execution", 3), 120),
        ]
        entities = [Entity("ip", "10.0.0.5")]
        doubled = score_chain(members, entities, threshold=3, w_entity=0.8, w_sequence=0.7, w_volume=0.5)
        assert doubled["confidence"] == 0.608

    def test_round3_half_up(self):
        assert round3(0.6085) == 0.609  # 608.5+0.5=609.0
        assert round3(1 / 3) == 0.333


class TestIsMonotonic:
    def test_cases(self):
        assert is_monotonic([]) is False
        assert is_monotonic([1]) is False
        assert is_monotonic([1, 2, 3]) is True
        assert is_monotonic([1, 1, 2]) is True  # non-decreasing + last>first
        assert is_monotonic([1, 1, 1]) is False  # last == first
        assert is_monotonic([2, 1, 3]) is False


class TestBuildChain:
    def _members(self):
        return [
            _meta("b", "auth_failure", ("initial_access", 2), 110, sev="high"),
            _meta("a", "port_scan", ("reconnaissance", 1), 100, sev="low"),
            _meta("c", "malware", ("execution", 3), 120, sev="medium"),
        ]

    def test_envelope_contract_shape(self):
        entities = [Entity("ip", "10.0.0.5"), Entity("user", "alice")]
        signals = {"entity": 0.667, "sequence": 1.0, "volume": 0.5, "confidence": 0.725}
        chain = build_chain("acme", self._members(), entities, signals, window_seconds=600, now=1000)
        assert set(chain.keys()) == {
            "chain_id", "tenant_id", "attack_category", "kill_chain_stage",
            "kill_chain_ordered", "confidence", "signals", "common_dimensions",
            "member_events", "why_correlated", "window_seconds", "timestamp_unix",
            "schema_version",
        }
        assert chain["tenant_id"] == "acme"
        assert chain["attack_category"] == "execution"
        assert chain["kill_chain_stage"] == "execution"
        assert chain["kill_chain_ordered"] is True
        assert chain["confidence"] == 0.725
        assert chain["signals"] == signals
        assert chain["common_dimensions"] == [
            {"type": "ip", "value": "10.0.0.5"},
            {"type": "user", "value": "alice"},
        ]
        assert chain["window_seconds"] == 600
        assert chain["timestamp_unix"] == 1000
        assert chain["schema_version"] == "1.0.0"
        assert chain["chain_id"]

    def test_member_events_ordered_by_ts_with_exact_keys(self):
        chain = build_chain("acme", self._members(), [Entity("ip", "10.0.0.5")],
                            {"entity": 0.333, "sequence": 1.0, "volume": 0.5, "confidence": 0.608},
                            window_seconds=600, now=1000)
        events = chain["member_events"]
        assert [m["incident_id"] for m in events] == ["a", "b", "c"]
        assert set(events[0].keys()) == {
            "incident_id", "category", "severity", "source_ip",
            "kill_chain_stage", "kill_chain_order", "timestamp_unix",
        }
        assert events[0]["kill_chain_stage"] == "reconnaissance"
        assert events[0]["kill_chain_order"] == 1
        assert events[0]["timestamp_unix"] == 100

    def test_all_unknown_stages_yield_unknown_category(self):
        members = [
            _meta("a", "weird", ("unknown", 0), 100),
            _meta("b", "weird", ("unknown", 0), 110),
        ]
        chain = build_chain("acme", members, [Entity("ip", "1.2.3.4")],
                            {"entity": 0.333, "sequence": 0.0, "volume": 0.5, "confidence": 0.3},
                            window_seconds=600, now=1000)
        # Parity with Go: first member always ties (0 >= 0) → name "unknown",
        # NOT the "correlated_activity" fallback (only reachable with 0 members).
        assert chain["attack_category"] == "unknown"
        assert chain["kill_chain_ordered"] is False

    def test_explain_chain_format(self):
        dims = [{"type": "ip", "value": "10.0.0.5"}]
        refs = [
            {"kill_chain_stage": "reconnaissance"},
            {"kill_chain_stage": "unknown"},
            {"kill_chain_stage": "execution"},
        ]
        signals = {"entity": 0.333, "sequence": 1.0, "volume": 0.5, "confidence": 0.608}
        msg = explain_chain(dims, refs, signals)
        assert msg == (
            "3 events share [ip=10.0.0.5]; kill-chain: reconnaissance → execution; "
            "confidence=0.61 (entity=0.33 seq=1.00 vol=0.50)"
        )

    def test_explain_chain_truncated_at_2000(self):
        dims = [{"type": "user", "value": "u" * 120} for _ in range(40)]
        msg = explain_chain(dims, [], {"entity": 0, "sequence": 0, "volume": 0, "confidence": 0})
        assert len(msg) <= 2000
        assert msg.endswith("...[TRUNCATED]")


class TestValidateChain:
    def _chain(self, **over):
        base = build_chain(
            "acme",
            [_meta("a", "port_scan", ("reconnaissance", 1), 100),
             _meta("b", "auth_failure", ("initial_access", 2), 110)],
            [Entity("ip", "10.0.0.5")],
            {"entity": 0.333, "sequence": 1.0, "volume": 0.333, "confidence": 0.57},
            window_seconds=600, now=1000,
        )
        base.update(over)
        return base

    def test_valid_chain_passes(self):
        validate_chain(self._chain())

    def test_rejects_missing_required(self):
        with pytest.raises(ValueError):
            validate_chain(self._chain(chain_id=" "))
        with pytest.raises(ValueError):
            validate_chain(self._chain(tenant_id=""))
        with pytest.raises(ValueError):
            validate_chain(self._chain(schema_version=""))

    def test_rejects_thin_chain(self):
        with pytest.raises(ValueError):
            validate_chain(self._chain(member_events=[{"incident_id": "a"}]))
        with pytest.raises(ValueError):
            validate_chain(self._chain(common_dimensions=[]))

    def test_rejects_confidence_out_of_range(self):
        with pytest.raises(ValueError):
            validate_chain(self._chain(confidence=1.2))
        with pytest.raises(ValueError):
            validate_chain(self._chain(confidence=-0.1))
