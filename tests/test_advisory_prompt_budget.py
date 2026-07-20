"""TDD — prompt budget gate + section theo-lane động (P0a).

Phát hiện 2026-07-15: prompt advisory 38k chars nhưng production clip head-only ở
``35% × (num_ctx − num_predict) × 4`` = 10.035 chars (num_ctx 8192, num_predict
1024) → model chỉ thấy 26%; mọi rule phía sau (VERDICT SELECTION, EVIDENCE
RELEVANCE, META ALERTS, EXAMPLES...) chưa bao giờ đến model. Gate này bảo đảm
prompt LUÔN vừa vùng nhìn thấy — rule không bao giờ rơi vào vùng chết âm thầm nữa.
"""

from __future__ import annotations

from workers.advisory_mode_system_prompt import (
    build_advisory_system_prompt,
    production_prompt_clip_chars,
)

# Evidence tổng hợp "nặng nhất" thực tế: KB + SIEM + DB + storage + services + HTTP surge
_KITCHEN_SINK_EVIDENCE = (
    "=== REDIS SECOND-BRAIN CONTEXT (multi-turn RAG · 2 turns · top_score=0.706) ===\n"
    "[KB id=kb-seed-001 col=vendor_knowledge score=0.706] leak vs spike\n"
    "[ALERT_CONTEXT] siem_category=malware severity=critical\n"
    "[EVIDENCE] probe: mysql_health status: FAILED\n"
    "probe: proxysql_stats · probe: disk_usage /var 91% · nfs stale · inode\n"
    "probe: service_haproxy backend DOWN · systemd unit failed\n"
    "HTTP 503 surge 429 rate=42% symptom_group=http_surge\n"
)


def test_default_prompt_fits_production_clip():
    clip = production_prompt_clip_chars()
    p = build_advisory_system_prompt(None)
    assert len(p) <= clip, (
        f"Core prompt {len(p)} chars > production clip {clip} — "
        "rule cuối prompt sẽ rơi vào vùng model không nhìn thấy"
    )


def test_kitchen_sink_prompt_fits_production_clip():
    clip = production_prompt_clip_chars()
    p = build_advisory_system_prompt(None, evidence_text=_KITCHEN_SINK_EVIDENCE)
    assert len(p) <= clip, (
        f"Prompt với đủ mọi section động {len(p)} chars > clip {clip}"
    )


def test_production_clip_matches_handler_formula():
    # num_ctx 8192, num_predict 1024 → (8192-1024)*4*0.35 = 10035 (khớp log system_len)
    assert production_prompt_clip_chars(num_ctx=8192, num_predict=1024) == 10035


def test_kb_section_only_when_evidence_has_kb():
    with_kb = build_advisory_system_prompt(None, evidence_text="[KB id=x col=y score=0.7] z")
    without = build_advisory_system_prompt(None, evidence_text="probe: node_cpu ok")
    assert "kb_assessment" in with_kb
    assert "KB RECONCILIATION" in with_kb
    assert "KB RECONCILIATION" not in without


def test_siem_section_only_when_evidence_has_siem():
    with_siem = build_advisory_system_prompt(None, evidence_text="siem_category=ddos attack")
    without = build_advisory_system_prompt(None, evidence_text="disk 91% full")
    assert "kill_chain" in with_siem or "Kill Chain" in with_siem
    assert "Kill Chain" not in without and "kill_chain" not in without


def test_db_storage_services_sections_are_conditional():
    db = build_advisory_system_prompt(None, evidence_text="probe: mysql_health lag")
    storage = build_advisory_system_prompt(None, evidence_text="nfs mount stale inode")
    services = build_advisory_system_prompt(None, evidence_text="probe: service_haproxy")
    plain = build_advisory_system_prompt(None, evidence_text="pod restart count")
    assert "SHOW REPLICA STATUS" in db and "SHOW REPLICA STATUS" not in plain
    assert "nfs_health" in storage and "nfs_health" not in plain
    assert "haproxy_stats" in services and "haproxy_stats" not in plain


def test_core_always_carries_critical_guards():
    # Các guard sống còn phải nằm trong CORE (mọi biến thể prompt) — không phụ thuộc lane
    for ev in ("", "siem_category=ddos", "[KB id=a col=b score=0.5] c"):
        p = build_advisory_system_prompt(None, evidence_text=ev)
        assert "ANTI-PARROTING" in p
        assert "EVIDENCE RELEVANCE" in p
        assert "SELF-MONITORING" in p
        assert "OmniBaseline" in p
        assert "VERDICT SELECTION" in p
        assert "REMEDIATION DISCIPLINE" in p
        assert "SCOPE-AWARE ENTRY" in p


def test_no_giant_verbatim_examples_remain():
    # 3 example JSON lớn cuối prompt cũ là nguồn parrot chính — không được quay lại
    p = build_advisory_system_prompt(None)
    assert '"trace_id": "abc...' not in p
    assert '"trace_id": "xyz...' not in p
    assert "Pod memory usage (850 MB)" not in p
