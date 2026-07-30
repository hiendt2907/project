"""Đường ĐỌC chuyển từ lane trục A sang domain (Phase 2 của kế hoạch bỏ lane).

Bối cảnh: `plans/lane-to-domain-and-omni-decides-2026-07-30.md` §0 + Phase 2.

Điều các test này bảo vệ, theo thứ tự quan trọng:
 1. Envelope có `domain` thì domain THẮNG mọi suy đoán từ lane.
 2. Envelope KHÔNG có `domain` (agent bản cũ, dữ liệu lịch sử) vẫn đọc được qua
    `lane_to_domain` — additive, không cắt.
 3. Cả 9 domain canonical đều có cửa vào chẩn đoán. Trước khi bỏ lane, năm domain
    (network/storage/database/hardware/service) không có cửa nào.

Hàng rào ba trục "lane" nằm ở `tests/test_domain_lane_boundary.py` — file này KHÔNG
lặp lại nó.
"""
from __future__ import annotations

from typing import Any

import pytest

from pkg.domain import taxonomy
from pkg.reasoning.domain_signals import DOMAIN_DATABASE, DOMAIN_OS, detect_domain
from pkg.reasoning.evidence_cluster import resolve_item_domain
from pkg.reasoning.schema import coerce_evidence_dict


# ── domain_signals: nguồn tự khai thắng suy đoán ─────────────────────────────


class TestDetectDomainHint:
    def test_domain_hint_wins_over_lane_default(self) -> None:
        assert detect_domain("unknown_probe", "", "", "SYS_RESOURCE",
                             domain_hint="database") == DOMAIN_DATABASE

    def test_domain_hint_wins_over_probe_prefix(self) -> None:
        """Collector biết nó đang đo gì; tiền tố probe chỉ là suy đoán."""
        assert detect_domain("disk_usage", "", "", "", domain_hint="hardware") == taxonomy.HARDWARE

    def test_domain_hint_accepts_legacy_vocabulary(self) -> None:
        """Agent bản cũ gửi 'os_system'/'k8s' — taxonomy có alias, phải hiểu được."""
        assert detect_domain("unknown_probe", "", "", "", domain_hint="os_system") == DOMAIN_OS
        assert detect_domain("unknown_probe", "", "", "", domain_hint="k8s") == taxonomy.KUBERNETES

    def test_unrecognised_hint_falls_through_not_raises(self) -> None:
        """Nhãn rác không được làm mất cả cascade — nó chỉ bị bỏ qua."""
        assert detect_domain("mysql_status", "", "", "", domain_hint="nonsense") == DOMAIN_DATABASE

    def test_no_hint_keeps_previous_behaviour(self) -> None:
        assert detect_domain("unknown_probe", "", "", "SIEM_SECURITY") == taxonomy.SECURITY


# ── evidence_cluster: thứ tự hint > item.domain > lane ───────────────────────


class TestResolveItemDomain:
    def test_hint_first(self) -> None:
        item = {"domain": "storage", "lane": "SYS_RESOURCE"}
        assert resolve_item_domain(item, taxonomy.DATABASE) == taxonomy.DATABASE

    def test_item_domain_beats_lane(self) -> None:
        item = {"domain": "storage", "lane": "SYS_RESOURCE"}
        assert resolve_item_domain(item) == taxonomy.STORAGE

    def test_lane_only_is_the_fallback(self) -> None:
        assert resolve_item_domain({"lane": "APP_HTTP"}) == taxonomy.APPLICATION

    def test_sys_hard_fail_without_domain_stays_unknown(self) -> None:
        """Lane gánh 4 domain — không đoán. Đây là lý do collector phải khai domain."""
        assert resolve_item_domain({"lane": "SYS_HARD_FAIL"}) == taxonomy.UNKNOWN

    def test_empty_item_is_unknown_not_crash(self) -> None:
        assert resolve_item_domain({}) == taxonomy.UNKNOWN


# ── schema: `domain` phải đi qua được cửa hẹp coerce_evidence_dict ───────────


def test_coerce_evidence_dict_carries_domain() -> None:
    """Thiếu bước này thì domain do gateway ghi bị rơi im lặng ở biên reasoning."""
    out = coerce_evidence_dict({"trace_id": "t1", "domain": "database", "lane": "SYS_HARD_FAIL"})
    assert out["domain"] == "database"
    assert out["lane"] == "SYS_HARD_FAIL"  # additive: không cắt lane


def test_coerce_evidence_dict_without_domain_still_works() -> None:
    out = coerce_evidence_dict({"trace_id": "t1", "lane": "SYS_RESOURCE"})
    assert "domain" not in out
    assert taxonomy.lane_to_domain(out["lane"]) == taxonomy.OS_HOST


# ── autonomy policy: rule khai domain vẫn khớp sự cố mang lane, và ngược lại ─


class TestPolicyScopeBridge:
    def test_legacy_lane_rule_matches_domain_incident(self) -> None:
        """Rule HITL cho security critical đã nằm trong Redis dưới dạng lane cũ.

        Nếu nó thôi khớp khi caller gửi `security`, hệ thống mất cổng HITL mà không
        báo lỗi — đúng loại hỏng mà cả kế hoạch này phải tránh.
        """
        from pkg.autonomy.policy import AutonomyLevel, AutonomyPolicyStore, find_matching_rule

        rules = AutonomyPolicyStore.DEFAULT_POLICY
        matched = find_matching_rule(rules, taxonomy.SECURITY, "critical", "block_ip")
        assert matched is not None
        assert matched.level == AutonomyLevel.HITL

    def test_domain_rule_matches_legacy_lane_incident(self) -> None:
        from pkg.autonomy.policy import AutonomyLevel, PolicyRule, find_matching_rule

        rule = PolicyRule(lane=taxonomy.SECURITY, severity="critical", action_type="*",
                          level=AutonomyLevel.HITL)
        assert find_matching_rule([rule], "SIEM_SECURITY", "critical", "block_ip") is rule

    def test_two_unrecognised_scopes_do_not_match(self) -> None:
        """Hai nhãn rác cùng ra `unknown` — không được vì thế mà khớp nhau."""
        from pkg.autonomy.policy import AutonomyLevel, PolicyRule, find_matching_rule

        rule = PolicyRule(lane="GARBAGE_A", severity="*", action_type="*",
                          level=AutonomyLevel.HITL)
        assert find_matching_rule([rule], "GARBAGE_B", "critical", "*") is None

    def test_proof_lane_does_not_match_a_domain_rule(self) -> None:
        """`resource` là proof_lane (trục B). Nó không được khớp domain `os_host`."""
        from pkg.autonomy.policy import AutonomyLevel, PolicyRule, find_matching_rule

        rule = PolicyRule(lane=taxonomy.OS_HOST, severity="*", action_type="*",
                          level=AutonomyLevel.HITL)
        assert find_matching_rule([rule], "resource", "critical", "*") is None


# ── playbook trigger: khai domain hay lane đều khớp được ────────────────────


class TestPlaybookTriggerScope:
    def test_domains_expand_to_legacy_lanes(self) -> None:
        from workers.schemas.playbook import TriggerMatch

        trig = TriggerMatch(domains=["os_host"])
        assert "OS_HOST" in trig.lanes
        assert "SYS_RESOURCE" in trig.lanes

    def test_legacy_lanes_expand_to_domain(self) -> None:
        from workers.schemas.playbook import TriggerMatch

        trig = TriggerMatch(lanes=["SIEM_SECURITY"])
        assert "SIEM_SECURITY" in trig.lanes
        assert "SECURITY" in trig.lanes

    def test_sys_hard_fail_is_not_invented_from_a_domain(self) -> None:
        """`SYS_HARD_FAIL` → `unknown`, nên không domain nào được tự nhận nó."""
        from workers.schemas.playbook import TriggerMatch

        assert "SYS_HARD_FAIL" not in TriggerMatch(domains=["storage"]).lanes

    def test_unknown_token_is_kept_not_dropped(self) -> None:
        from workers.schemas.playbook import TriggerMatch

        assert "CUSTOM_SCOPE" in TriggerMatch(lanes=["custom_scope"]).lanes

    def test_empty_trigger_stays_empty(self) -> None:
        from workers.schemas.playbook import TriggerMatch

        assert TriggerMatch().lanes == []


# ── probe selection theo domain + catalogue 9 domain ────────────────────────


class TestDomainProbeSelection:
    def test_every_canonical_domain_has_a_door(self) -> None:
        """Lợi ích thật của việc bỏ lane: 9/9 domain có bộ chẩn đoán.

        4 lane cũ chỉ mở cửa cho os/app/siem — network, storage, database, hardware,
        service không có cách nào gọi đúng bộ chẩn đoán của mình.
        """
        from workers.diagnostic_probe_registry import domain_coverage

        empty = [d for d, dd in domain_coverage().items() if dd.is_empty]
        assert empty == [], f"domain khong co cua vao chan doan: {empty}"

    def test_five_previously_doorless_domains_have_commands(self) -> None:
        from workers.diagnostic_probe_registry import select_diagnostics

        for dom in (taxonomy.NETWORK, taxonomy.STORAGE, taxonomy.DATABASE,
                    taxonomy.HARDWARE, taxonomy.SERVICE):
            assert select_diagnostics(domain=dom).commands, f"{dom} khong co lenh chan doan"

    def test_probe_domains_plus_self_probes_covers_registry_exactly(self) -> None:
        """Mỗi probe phải nằm ở ĐÚNG MỘT nhóm: domain của khách, hoặc tự kiểm."""
        from workers.diagnostic_probe_registry import (
            PROBE_DOMAINS,
            PROBE_REGISTRY,
            SELF_PROBES,
        )

        assert set(PROBE_DOMAINS) | SELF_PROBES == set(PROBE_REGISTRY)
        assert not (set(PROBE_DOMAINS) & SELF_PROBES)

    def test_omni_self_infra_probes_are_not_customer_domains(self) -> None:
        """Probe kiểm Redis/Kafka của CHÍNH Omni không được nằm trên trục domain khách.

        Quyết định của chủ hệ thống 2026-07-30. Trước đó chúng gắn `service`, làm ma
        trận năng lực `service` trông rộng hơn thực tế: người đọc báo cáo thấy "3 probe
        service" rồi tưởng đó là năng lực chẩn đoán hệ thống của họ, trong khi cả ba chỉ
        trả lời "daemon của Omni còn sống không".

        Đây là hàng rào chống việc gắn lại — fail-closed lúc import đòi mọi probe phải
        được khai báo, nên đường dễ nhất là nhét vào `PROBE_DOMAINS`, và đó là sai.
        """
        from workers.diagnostic_probe_registry import (
            PROBE_DOMAINS,
            SELF_PROBES,
            select_diagnostics,
        )

        assert SELF_PROBES == {"redis_ping", "kafka_alerts_topic", "redis_stream_len_inbound"}
        for probe in SELF_PROBES:
            assert probe not in PROBE_DOMAINS

        # Không probe tự kiểm nào lọt vào bộ chẩn đoán của bất kỳ domain khách nào.
        for dom in taxonomy.CANONICAL_DOMAINS:
            leaked = SELF_PROBES & set(select_diagnostics(domain=dom).probes)
            assert not leaked, f"probe tu kiem lot vao domain {dom}: {sorted(leaked)}"

    def test_service_domain_still_has_a_way_in_after_removal(self) -> None:
        """Bỏ 3 probe tự kiểm KHÔNG được làm `service` mất cửa vào.

        `service` nay có 0 probe in-cluster nhưng vẫn còn lệnh trong catalogue — đó là
        cửa vào thật. Nếu test này đỏ thì việc bỏ đã đi quá xa: một domain rỗng nghĩa là
        sự cố thuộc domain đó không gọi được bộ chẩn đoán nào.
        """
        from workers.diagnostic_probe_registry import select_diagnostics

        dd = select_diagnostics(domain=taxonomy.SERVICE)
        assert dd.probes == ()
        assert dd.commands
        assert not dd.is_empty

    def test_select_by_lane_is_the_fallback(self) -> None:
        from workers.diagnostic_probe_registry import select_diagnostics

        assert select_diagnostics(lane="SIEM_SECURITY").domain == taxonomy.SECURITY
        assert "rbac_drift" in select_diagnostics(lane="SIEM_SECURITY").probes

    def test_domain_wins_over_lane(self) -> None:
        from workers.diagnostic_probe_registry import select_diagnostics

        got = select_diagnostics(domain="storage", lane="SIEM_SECURITY")
        assert got.domain == taxonomy.STORAGE

    def test_unknown_scope_returns_empty_not_a_guess(self) -> None:
        """Chạy sai bộ probe rồi kết luận 'không thấy gì' là bằng chứng giả."""
        from workers.diagnostic_probe_registry import select_diagnostics

        got = select_diagnostics(lane="SYS_HARD_FAIL")
        assert got.domain == taxonomy.UNKNOWN
        assert got.is_empty

    def test_catalogue_commands_are_read_only_scope(self) -> None:
        """Catalogue KHÔNG cấp quyền mutate — chọn theo domain không được nới điều đó."""
        from pkg.diagnostics.command_catalog import WRITE_VERBS, load_catalog

        cat = load_catalog()
        for spec in cat.specs.values():
            assert not (spec.subcommands & WRITE_VERBS)


# ── os_state_validator: probe cùng domain được kiểm trước ───────────────────


class TestOsValidatorDomainOrder:
    def test_probe_domains_registered_for_all_handlers(self) -> None:
        from workers.os_state_validator import _OS_PROBE_DOMAINS, _OS_PROBE_HANDLERS

        assert set(_OS_PROBE_DOMAINS) == set(_OS_PROBE_HANDLERS)

    @pytest.mark.parametrize(
        ("domain", "expected"),
        [
            (taxonomy.DATABASE, "mysql_health"),
            (taxonomy.STORAGE, "disk_usage"),
            (taxonomy.NETWORK, "dns_resolution"),
            (taxonomy.HARDWARE, "memory_hw_errors"),
            (taxonomy.SERVICE, "systemd_units"),
        ],
    )
    def test_os_probes_for_domain(self, domain: str, expected: str) -> None:
        from workers.os_state_validator import os_probes_for_domain

        assert expected in os_probes_for_domain(domain)

    def test_same_domain_probe_checked_first(self) -> None:
        """Sự cố `storage` phải nghe `disk_usage` trước `systemd_units`."""
        from workers.os_state_validator import _probe_order

        by_probe: dict[str, dict[str, Any]] = {
            "systemd_units": {}, "disk_usage": {}, "mysql_health": {},
        }
        assert _probe_order(by_probe, taxonomy.STORAGE)[0] == "disk_usage"

    def test_order_does_not_drop_other_domains(self) -> None:
        """Chỉ xếp lại thứ tự, KHÔNG lọc — bằng chứng khác lĩnh vực vẫn có giá trị."""
        from workers.os_state_validator import _probe_order

        by_probe: dict[str, dict[str, Any]] = {"systemd_units": {}, "disk_usage": {}}
        assert set(_probe_order(by_probe, taxonomy.STORAGE)) == set(by_probe)

    def test_unknown_domain_keeps_original_order(self) -> None:
        from workers.os_state_validator import _probe_order

        by_probe: dict[str, dict[str, Any]] = {"systemd_units": {}, "disk_usage": {}}
        assert _probe_order(by_probe, taxonomy.UNKNOWN) == ["systemd_units", "disk_usage"]

    def test_contrast_prefers_same_domain_probe(self) -> None:
        from workers.os_state_validator import compare_alert_claim_to_os_state

        by_probe = {
            "systemd_units": {"result": "PASSED", "extracted_fact": {"failed_units": []}},
            "disk_usage": {"result": "PASSED", "extracted_fact": {"disk_critical_count": 0}},
        }
        out = compare_alert_claim_to_os_state(by_probe, {}, domain=taxonomy.STORAGE)
        assert out is not None and "disk_usage" in out

    def test_no_domain_keeps_legacy_behaviour(self) -> None:
        from workers.os_state_validator import compare_alert_claim_to_os_state

        by_probe = {
            "systemd_units": {"result": "PASSED", "extracted_fact": {"failed_units": []}},
            "disk_usage": {"result": "PASSED", "extracted_fact": {"disk_critical_count": 0}},
        }
        out = compare_alert_claim_to_os_state(by_probe, {})
        assert out is not None and "systemd_units" in out


# ── os_diagnostic_loop: thay probe cùng domain khi RAG gợi ý probe không có ──


class TestSameDomainSubstitute:
    def test_substitutes_within_domain(self) -> None:
        from workers.os_diagnostic_loop import _same_domain_substitute

        got = _same_domain_substitute("storage_disk", {"disk_usage": {}}, taxonomy.STORAGE, set())
        assert got == "disk_usage"

    def test_never_crosses_domain(self) -> None:
        """Probe khác lĩnh vực trả lời một câu hỏi khác — không phải thay thế."""
        from workers.os_diagnostic_loop import _same_domain_substitute

        assert _same_domain_substitute("storage_disk", {"mysql_health": {}},
                                       taxonomy.STORAGE, set()) is None

    def test_no_reuse_of_already_used_probe(self) -> None:
        from workers.os_diagnostic_loop import _same_domain_substitute

        assert _same_domain_substitute("storage_disk", {"disk_usage": {}},
                                       taxonomy.STORAGE, {"disk_usage"}) is None

    def test_unknown_domain_disables_substitution(self) -> None:
        """Không biết lĩnh vực thì giữ hành vi cũ (dừng), không đoán."""
        from workers.os_diagnostic_loop import _same_domain_substitute

        assert _same_domain_substitute("x", {"disk_usage": {}}, taxonomy.UNKNOWN, set()) is None


class TestSelfDeclaredDomainWinsOnRealPaths:
    """Domain collector TỰ KHAI phải thắng suy đoán — ở CHÍNH các call site thật.

    Hồi quy 2026-07-30: `detect_domain()` có tham số `domain_hint` với đúng ngữ nghĩa
    "nguồn tự khai thắng mọi suy đoán", nhưng **cả hai** call site thật đều không
    truyền nó. Hệ quả đo được trên VM: `remote_log_errors` (do
    `collectors/logs.py` khai `application`) bị cascade nội dung suy thành
    `kubernetes` ⇒ sự cố ứng dụng của khách bị gán sai lĩnh vực và gọi sai bộ chẩn
    đoán. Test chỉ kiểm `detect_domain` sẽ KHÔNG bắt được lỗi này — nó nằm ở call site.
    """

    def test_remote_agent_pipeline_passes_domain_hint(self) -> None:
        import inspect

        from workers import remote_agent_pipeline as rap

        src = inspect.getsource(rap)
        assert "domain_hint=ev_doc.get(\"domain\")" in src, (
            "remote_agent_pipeline khong truyen domain tu khai vao detect_domain"
        )

    def test_agent_webhook_passes_domain_hint(self) -> None:
        import inspect

        from gateway.routes import agent_webhook

        src = inspect.getsource(agent_webhook)
        assert "domain_hint=" in src, (
            "agent_webhook khong truyen domain tu khai vao detect_domain"
        )

    def test_declared_application_survives_kubernetes_looking_content(self) -> None:
        """Đúng hình dạng dữ liệu đã gây lỗi: nội dung log nhắc container/k8s."""
        from pkg.reasoning.domain_signals import detect_domain

        noisy_raw = "container restarted; kubelet reported pod eviction; docker daemon"
        without_hint = detect_domain("remote_log_errors", "loi ung dung", noisy_raw, "APP_HTTP")
        with_hint = detect_domain(
            "remote_log_errors", "loi ung dung", noisy_raw, "APP_HTTP",
            domain_hint=taxonomy.APPLICATION,
        )
        assert with_hint == taxonomy.APPLICATION
        # Không khẳng định `without_hint` là kubernetes (cascade có thể đổi) — điều
        # cần bảo vệ là: có hint thì hint thắng, bất kể cascade suy ra gì.
        assert with_hint != without_hint or without_hint == taxonomy.APPLICATION
