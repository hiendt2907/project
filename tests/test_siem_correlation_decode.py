"""TDD — services.siem_correlation.decode: omni-siem-raw → Incident + envelope.

Parity contract: port 1-1 của brain-go ``internal/transport/kafka.go``
(``decodeKafkaMessage`` + ``incidentEnvelope``).
"""

from __future__ import annotations

import json

from services.siem_correlation.decode import decode_kafka_message, incident_envelope


def _raw(**over) -> dict:
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "acme",
        "severity": "high",
        "source": "finguard",
        "category": "auth_failure",
        "timestamp_unix": 1721600000,
        "schema_version": "1.0.0",
    }
    base.update(over)
    return base


class TestDecode:
    def test_decodes_full_message(self):
        inc = decode_kafka_message(json.dumps(_raw(
            rule_id="SSH_BRUTE_FORCE",
            source_ip="203.0.113.9",
            dest_ip="10.0.0.2",
            description="failed login user=alice",
            correlation_ids=["a", "b"],
            tags=["upstream_source:siem-bridge"],
        )).encode())
        assert inc is not None
        assert inc.incident_id == "11111111-1111-1111-1111-111111111111"
        assert inc.tenant_id == "acme"
        assert inc.severity == "high"
        assert inc.source == "finguard"
        assert inc.category == "auth_failure"
        assert inc.timestamp_unix == 1721600000
        assert inc.rule_id == "SSH_BRUTE_FORCE"
        assert inc.source_ip == "203.0.113.9"
        assert inc.dest_ip == "10.0.0.2"
        assert inc.raw_log == "failed login user=alice"
        assert inc.correlation_ids == ("a", "b")
        assert inc.tags == ("upstream_source:siem-bridge",)
        # entities populated on decode (ip×2 + user)
        types = {e.type for e in inc.entities}
        assert types == {"ip", "user"}

    def test_missing_id_or_tenant_returns_none(self):
        assert decode_kafka_message(json.dumps(_raw(id=""))) is None
        assert decode_kafka_message(json.dumps({k: v for k, v in _raw().items() if k != "tenant_id"})) is None

    def test_invalid_json_returns_none(self):
        assert decode_kafka_message(b"{not json") is None
        assert decode_kafka_message(b"[1,2,3]") is None

    def test_body_preference_description_then_message_then_raw_log(self):
        inc = decode_kafka_message(_raw(description="D", message="M", raw_log="R"))
        assert inc is not None and inc.raw_log == "D"
        inc = decode_kafka_message(_raw(message="M", raw_log="R"))
        assert inc is not None and inc.raw_log == "M"
        inc = decode_kafka_message(_raw(raw_log="R"))
        assert inc is not None and inc.raw_log == "R"

    def test_non_string_fields_treated_as_empty(self):
        inc = decode_kafka_message(_raw(severity=5, source_ip=123))
        assert inc is not None
        assert inc.severity == ""
        assert inc.source_ip == ""

    def test_timestamp_accepts_number_only(self):
        inc = decode_kafka_message(_raw(timestamp_unix="1721600000"))
        assert inc is not None
        assert inc.timestamp_unix == 0

    def test_correlation_ids_keep_only_strings(self):
        inc = decode_kafka_message(_raw(correlation_ids=["a", 1, None, "b"]))
        assert inc is not None
        assert inc.correlation_ids == ("a", "b")

    def test_accepts_dict_and_str_input(self):
        assert decode_kafka_message(_raw()) is not None
        assert decode_kafka_message(json.dumps(_raw())) is not None


class TestIncidentEnvelope:
    def test_required_subset_always_present(self):
        inc = decode_kafka_message(_raw())
        env = incident_envelope(inc)
        assert env == {
            "id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "acme",
            "severity": "high",
            "source": "finguard",
            "category": "auth_failure",
            "timestamp_unix": 1721600000,
            "schema_version": "1.0.0",
        }

    def test_optional_fields_only_when_non_empty(self):
        inc = decode_kafka_message(_raw(
            source_ip="203.0.113.9", dest_ip="10.0.0.2",
            correlation_ids=["x"], tags=["t1"],
        ))
        env = incident_envelope(inc)
        assert env["source_ip"] == "203.0.113.9"
        assert env["dest_ip"] == "10.0.0.2"
        assert env["correlation_ids"] == ["x"]
        assert env["tags"] == ["t1"]

    def test_raw_log_never_leaves_via_envelope(self):
        inc = decode_kafka_message(_raw(description="user=alice did something"))
        env = incident_envelope(inc)
        assert "raw_log" not in env and "description" not in env and "message" not in env
