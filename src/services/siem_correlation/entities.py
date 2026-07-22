"""Entity extraction + kill-chain stage mapping.

Port 1-1 của brain-go ``internal/extract/entities.go`` và
``killchain_stages.go``.

SECURITY INVARIANT (FinGuard, non-negotiable): extraction NEVER reads a raw
line of VM data. It reads only already-parsed Incident fields (source_ip,
dest_ip) and the normalized message string (raw_log), from which entities are
pulled with a strict ALLOWLIST of ``key=value`` token patterns. Values are
length-capped and character-restricted so a malicious log line cannot smuggle
arbitrary content through as an "entity".
"""

from __future__ import annotations

import re

from services.siem_correlation.models import (
    ENTITY_HOST,
    ENTITY_IP,
    ENTITY_POD,
    ENTITY_PROCESS,
    ENTITY_SESSION,
    ENTITY_USER,
    Entity,
    Incident,
    KillChainStage,
)

# Caps (parity: extract.maxEntityValueLen / maxEntitiesPerIncident).
_MAX_ENTITY_VALUE_LEN = 128
_MAX_ENTITIES_PER_INCIDENT = 16

# Allowlist: normalized-message key → canonical entity type. Synonyms collapse
# onto a single type so cross-vendor events still correlate on one dimension.
_ALLOWLIST: tuple[tuple[tuple[str, ...], str], ...] = (
    (("user", "username", "account", "principal"), ENTITY_USER),
    (("session", "session_id", "sid", "token_id"), ENTITY_SESSION),
    (("host", "hostname", "node", "vm"), ENTITY_HOST),
    (("pod", "pod_name"), ENTITY_POD),
    (("process", "proc", "exe", "image"), ENTITY_PROCESS),
)

# An entity value may contain only "safe identifier" characters — whitespace or
# shell/log punctuation terminate the match (parity: extract.valuePattern).
_VALUE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:@/\-]*"

_KV_MATCHERS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(r"(?i)\b" + re.escape(key) + r'\s*[=:]\s*"?(' + _VALUE_PATTERN + r')"?'), typ)
    for keys, typ in _ALLOWLIST
    for key in keys
)


def normalize_value(raw: str) -> str:
    """Trim, strip quotes, lowercase; reject empty/oversized values."""
    v = raw.strip().strip("\"'")
    if not v or len(v) > _MAX_ENTITY_VALUE_LEN:
        return ""
    return v.lower()


def extract_entities(inc: Incident) -> tuple[Entity, ...]:
    """De-duplicated correlation dimensions, stable order (type, value),
    capped fan-out. Reads only parsed fields + the normalized message."""
    seen: set[Entity] = set()

    def add(typ: str, raw: str) -> None:
        v = normalize_value(raw)
        if v:
            seen.add(Entity(type=typ, value=v))

    add(ENTITY_IP, inc.source_ip)
    add(ENTITY_IP, inc.dest_ip)

    msg = inc.raw_log
    if msg:
        for pattern, typ in _KV_MATCHERS:
            for value in pattern.findall(msg):
                add(typ, value)

    out = sorted(seen)
    return tuple(out[:_MAX_ENTITIES_PER_INCIDENT])


# --- Kill-chain stages (parity: killchain_stages.go) -----------------------

STAGE_UNKNOWN = KillChainStage("unknown", 0)

# STATIC, audited table — no LLM, no VM data — deterministic and reviewable.
_CATEGORY_STAGE: dict[str, KillChainStage] = {
    "port_scan": KillChainStage("reconnaissance", 1),
    "network_anomaly": KillChainStage("reconnaissance", 1),
    "recon": KillChainStage("reconnaissance", 1),
    "auth_failure": KillChainStage("initial_access", 2),
    "brute_force": KillChainStage("initial_access", 2),
    "credential_stuffing": KillChainStage("initial_access", 2),
    "new_process": KillChainStage("execution", 3),
    "malware": KillChainStage("execution", 3),
    "suspicious_process": KillChainStage("execution", 3),
    "privilege_escalation": KillChainStage("privilege_escalation", 4),
    "sudo_abuse": KillChainStage("privilege_escalation", 4),
    "lateral_movement": KillChainStage("lateral_movement", 5),
    "k8s_threat": KillChainStage("lateral_movement", 5),
    "data_exfil": KillChainStage("exfiltration", 6),
    "data_exfiltration": KillChainStage("exfiltration", 6),
    "ddos": KillChainStage("impact", 7),
    "ransomware": KillChainStage("impact", 7),
}

# Rules are more specific than categories — they win when present.
_RULE_STAGE: dict[str, KillChainStage] = {
    "SSH_BRUTE_FORCE": KillChainStage("initial_access", 2),
    "PORT_SCAN_DETECTED": KillChainStage("reconnaissance", 1),
    "REVERSE_SHELL": KillChainStage("execution", 3),
    "K8S_EXEC_INTO_POD": KillChainStage("lateral_movement", 5),
}


def stage_for(inc: Incident) -> KillChainStage:
    """Stage for an incident: exact rule match > category > unknown
    (case-insensitive, parity: extract.StageFor)."""
    rule = inc.rule_id.strip()
    if rule:
        stage = _RULE_STAGE.get(rule.upper())
        if stage is not None:
            return stage
    category = inc.category.strip()
    if category:
        stage = _CATEGORY_STAGE.get(category.lower())
        if stage is not None:
            return stage
    return STAGE_UNKNOWN
